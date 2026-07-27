//! Flatten Pattern Clips into absolute merged note records.
//!
//! Two properties make this fast:
//!
//! * **Shape caching.** Every clip that shares `(pattern, source range, clip
//!   length)` produces byte-identical records apart from one additive position
//!   offset. Each distinct shape is transformed once into a *relative* block;
//!   each clip then re-emits that block with its own base added. Projects that
//!   reuse one pattern thousands of times — the case the README calls out as
//!   pathological — collapse to a single transform plus memcpy-speed rebasing.
//! * **Exact output sizing.** Record counts are known before any bytes are
//!   written, so the merged payload is allocated once and every clip fills a
//!   disjoint sub-slice in parallel. No temporary files and no reallocation.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hash, Hasher};

use rayon::prelude::*;

use crate::clips::Clip;
use crate::error::{MergeError, Result};
use crate::flp::*;
use crate::scan::{EventRef, ScanResult};

/// Below this many notes the rayon fork/join costs more than it saves.
const PARALLEL_NOTE_THRESHOLD: usize = 24_000;

/// Transformed records for one clip *shape*, with positions still relative to
/// the clip's own origin.
struct RelBlock {
    bytes: Vec<u8>,
    count: usize,
    max_rel: u64,
}

pub struct MergedNotes {
    pub bytes: Vec<u8>,
    pub count: usize,
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct BlockKey {
    pattern_id: u16,
    src_start: u64,
    src_end: u64,
    clip_length: u32,
}

impl Hash for BlockKey {
    #[inline]
    fn hash<H: Hasher>(&self, state: &mut H) {
        state.write_u64(self.src_start);
        state.write_u64(self.src_end);
        state.write_u64((self.pattern_id as u64) << 32 | self.clip_length as u64);
    }
}

/// Small non-cryptographic hasher (fxhash). Clip keys are internal, never
/// attacker-chosen, and there can be hundreds of thousands of them.
#[derive(Default)]
struct FxHasher {
    hash: u64,
}

const FX_SEED: u64 = 0x51_7c_c1_b7_27_22_0a_95;

impl FxHasher {
    #[inline]
    fn add(&mut self, value: u64) {
        self.hash = (self.hash.rotate_left(5) ^ value).wrapping_mul(FX_SEED);
    }
}

impl Hasher for FxHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            self.add(byte as u64);
        }
    }
    #[inline]
    fn write_u64(&mut self, value: u64) {
        self.add(value);
    }
    #[inline]
    fn write_u32(&mut self, value: u32) {
        self.add(value as u64);
    }
    #[inline]
    fn finish(&self) -> u64 {
        self.hash
    }
}

type FxBuild = BuildHasherDefault<FxHasher>;

/// Map a source-tick delta onto the clip's stretched timeline.
///
/// Integer half-up rounding keeps the first and last ticks exact and avoids
/// cumulative floating-point error. The 128-bit product cannot wrap, so the
/// arbitrary-precision fallback the Python build needed is unnecessary.
#[inline(always)]
fn map_delta<const IDENTITY: bool>(delta: u64, clip_length: u64, span: u64, half: u64) -> u64 {
    if IDENTITY {
        // clip_length == span: (d*s + s/2)/s == d exactly. Skip the divide.
        delta
    } else {
        (((delta as u128) * (clip_length as u128) + half as u128) / (span as u128)) as u64
    }
}

fn fill_block<const IDENTITY: bool>(
    data: &[u8],
    events: &[EventRef],
    clip_length: u64,
    src_start: u64,
    src_end: u64,
    play_truncated: bool,
    out: &mut Vec<u8>,
) -> Result<u64> {
    let span = src_end - src_start;
    let half = span / 2;
    let mut max_rel: u64 = 0;

    for event in events {
        let raw = &data[event.data_offset..event.data_offset + event.data_length];
        for record in raw.chunks_exact(NOTE_SIZE) {
            let position = read_u32(record, NOTE_POSITION) as u64;
            let length = read_u32(record, NOTE_LENGTH) as u64;

            let mapped_start;
            let new_length;
            if length == 0 {
                if position < src_start || position >= src_end {
                    continue;
                }
                mapped_start = map_delta::<IDENTITY>(position - src_start, clip_length, span, half);
                new_length = 0;
            } else {
                let end = position + length;
                // A clip controls which note-on events are visible, but a note
                // starting inside the clip keeps its complete tail; the mapped
                // end is deliberately not clamped to the clip's right edge.
                if position >= src_end || end <= src_start {
                    continue;
                }
                let visible_start = if position < src_start {
                    // FL's "Play truncated notes in clips" restores the portion
                    // after a sliced/cropped left edge.
                    if !play_truncated {
                        continue;
                    }
                    src_start
                } else {
                    position
                };
                mapped_start =
                    map_delta::<IDENTITY>(visible_start - src_start, clip_length, span, half);
                let mapped_end = map_delta::<IDENTITY>(end - src_start, clip_length, span, half);
                new_length = (mapped_end - mapped_start).max(1);
            }

            if mapped_start > UINT32_MAX {
                return Err(MergeError::format(
                    "A merged note position exceeds FL Studio's 32-bit range.",
                ));
            }
            if new_length > UINT32_MAX {
                return Err(MergeError::format(
                    "A merged note length exceeds FL Studio's 32-bit range.",
                ));
            }
            if mapped_start > max_rel {
                max_rel = mapped_start;
            }

            let at = out.len();
            out.extend_from_slice(record);
            let slot = &mut out[at..at + NOTE_SIZE];
            write_u32(slot, NOTE_POSITION, mapped_start as u32);
            write_u32(slot, NOTE_LENGTH, new_length as u32);
        }
    }
    Ok(max_rel)
}

fn build_relative_block(
    data: &[u8],
    events: &[EventRef],
    key: &BlockKey,
    play_truncated: bool,
) -> Result<RelBlock> {
    let clip_length = key.clip_length as u64;
    let span = key.src_end - key.src_start;
    if span == 0 || clip_length == 0 {
        return Ok(RelBlock {
            bytes: Vec::new(),
            count: 0,
            max_rel: 0,
        });
    }

    let upper_bound: usize = events.iter().map(|event| event.data_length).sum();
    let mut bytes = Vec::with_capacity(upper_bound);
    let max_rel = if clip_length == span {
        fill_block::<true>(
            data,
            events,
            clip_length,
            key.src_start,
            key.src_end,
            play_truncated,
            &mut bytes,
        )?
    } else {
        fill_block::<false>(
            data,
            events,
            clip_length,
            key.src_start,
            key.src_end,
            play_truncated,
            &mut bytes,
        )?
    };

    let count = bytes.len() / NOTE_SIZE;
    Ok(RelBlock {
        bytes,
        count,
        max_rel,
    })
}

/// Expand every clip into one contiguous merged-note payload.
///
/// Output order matches a straight sequential walk of the clip index, so the
/// result is byte-identical to the reference implementation's turbo output.
pub fn expand_clips(
    data: &[u8],
    scan: &ScanResult,
    clips: &[Clip],
    parallel: bool,
) -> Result<MergedNotes> {
    // 1. Deduplicate clips down to distinct transform shapes.
    let mut shape_of_clip: Vec<u32> = Vec::with_capacity(clips.len());
    let mut keys: Vec<BlockKey> = Vec::new();
    let mut seen: HashMap<BlockKey, u32, FxBuild> = HashMap::default();
    for clip in clips {
        let (src_start, src_end) = clip.source_range();
        let key = BlockKey {
            pattern_id: clip.pattern_id,
            src_start,
            src_end,
            clip_length: clip.length,
        };
        let next = keys.len() as u32;
        let slot = *seen.entry(key).or_insert(next);
        if slot == next {
            keys.push(key);
        }
        shape_of_clip.push(slot);
    }

    // 2. Transform each distinct shape exactly once.
    let play_truncated = scan.play_truncated_notes;
    let estimated: usize = keys
        .iter()
        .map(|key| {
            scan.notes_of(key.pattern_id)
                .iter()
                .map(|event| event.data_length / NOTE_SIZE)
                .sum::<usize>()
        })
        .sum();
    let blocks: Vec<RelBlock> = if parallel && estimated >= PARALLEL_NOTE_THRESHOLD {
        keys.par_iter()
            .map(|key| {
                build_relative_block(data, scan.notes_of(key.pattern_id), key, play_truncated)
            })
            .collect::<Result<Vec<_>>>()?
    } else {
        keys.iter()
            .map(|key| {
                build_relative_block(data, scan.notes_of(key.pattern_id), key, play_truncated)
            })
            .collect::<Result<Vec<_>>>()?
    };

    // 3. Exact output layout: each clip owns a disjoint span of the payload.
    let mut offsets: Vec<usize> = Vec::with_capacity(clips.len() + 1);
    let mut total = 0usize;
    for &shape in &shape_of_clip {
        offsets.push(total);
        total += blocks[shape as usize].count * NOTE_SIZE;
    }
    offsets.push(total);
    if total / NOTE_SIZE > u32::MAX as usize {
        return Err(MergeError::format(
            "Merged note data exceeds the 4 GiB FLdt chunk limit. Split the project first.",
        ));
    }

    let mut bytes = vec![0u8; total];
    let mut spans: Vec<&mut [u8]> = Vec::with_capacity(clips.len());
    {
        let mut rest = &mut bytes[..];
        for index in 0..clips.len() {
            let size = offsets[index + 1] - offsets[index];
            let (head, tail) = rest.split_at_mut(size);
            spans.push(head);
            rest = tail;
        }
    }

    let emit = |index: usize, span: &mut [u8]| -> Result<()> {
        let clip = &clips[index];
        let block = &blocks[shape_of_clip[index] as usize];
        if block.count == 0 {
            return Ok(());
        }
        let arr = &scan.arrangements[clip.arr_index as usize];
        let base = arr.section_offset + clip.position as u64;
        span.copy_from_slice(&block.bytes);
        if base == 0 {
            return Ok(());
        }
        // One bounds check for the whole block instead of one per note.
        if base + block.max_rel > UINT32_MAX {
            return Err(MergeError::format(
                "A merged note position exceeds FL Studio's 32-bit range.",
            ));
        }
        for record in span.chunks_exact_mut(NOTE_SIZE) {
            let relative = read_u32(record, NOTE_POSITION) as u64;
            write_u32(record, NOTE_POSITION, (base + relative) as u32);
        }
        Ok(())
    };

    if parallel && total / NOTE_SIZE >= PARALLEL_NOTE_THRESHOLD {
        spans
            .par_iter_mut()
            .enumerate()
            .try_for_each(|(index, span)| emit(index, span))?;
    } else {
        for (index, span) in spans.iter_mut().enumerate() {
            emit(index, span)?;
        }
    }

    Ok(MergedNotes {
        count: total / NOTE_SIZE,
        bytes,
    })
}
