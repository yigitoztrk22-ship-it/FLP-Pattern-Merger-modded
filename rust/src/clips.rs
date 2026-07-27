//! Playlist walk: collect the Pattern Clips and lay out arrangement sections.

use crate::error::{MergeError, Result};
use crate::flp::*;
use crate::scan::ScanResult;

/// One Pattern Clip to flatten. Audio and Automation Clips never reach here.
#[derive(Clone, Copy, Debug)]
pub struct Clip {
    /// Index into `ScanResult::arrangements`.
    pub arr_index: u32,
    pub position: u32,
    pub length: u32,
    pub pattern_id: u16,
    pub start_offset: i32,
    pub end_offset: i32,
}

impl Clip {
    /// Visible source range of the referenced pattern, as FL interprets it.
    /// Widened to u64 so `start + length` can never wrap.
    #[inline]
    pub fn source_range(&self) -> (u64, u64) {
        let start = if self.start_offset < 0 {
            0u64
        } else {
            self.start_offset as u64
        };
        let end = if (self.end_offset as i64) > start as i64 {
            self.end_offset as u64
        } else {
            start + self.length as u64
        };
        (start, end)
    }
}

pub struct ClipIndex {
    pub clips: Vec<Clip>,
    pub total_pattern_clips: u64,
    pub included_pattern_clips: u64,
}

fn choose_playlist_record_size(scan: &ScanResult, payload_length: usize) -> Result<usize> {
    if payload_length == 0 {
        return Ok(if scan.version_major >= 21 {
            PLAYLIST_NEW_SIZE
        } else {
            PLAYLIST_OLD_SIZE
        });
    }
    if scan.version_major >= 21 && payload_length % PLAYLIST_NEW_SIZE == 0 {
        return Ok(PLAYLIST_NEW_SIZE);
    }
    if payload_length % PLAYLIST_OLD_SIZE == 0 {
        return Ok(PLAYLIST_OLD_SIZE);
    }
    if payload_length % PLAYLIST_NEW_SIZE == 0 {
        return Ok(PLAYLIST_NEW_SIZE);
    }
    Err(MergeError::format(format!(
        "Unsupported Playlist payload size ({payload_length} bytes) for FL Studio {}.",
        scan.version_text
    )))
}

/// Index every Pattern Clip and assign each arrangement its own section of the
/// merged pattern. Mutates arrangement bookkeeping on `scan`.
pub fn extract_pattern_clips(
    data: &[u8],
    scan: &mut ScanResult,
    include_muted_clips: bool,
) -> Result<ClipIndex> {
    let mut clips: Vec<Clip> = Vec::new();
    let mut total_pattern_clips: u64 = 0;
    let mut included_pattern_clips: u64 = 0;

    for arr_index in 0..scan.arrangements.len() {
        let Some(reference) = scan.arrangements[arr_index].playlist else {
            continue;
        };
        if reference.data_length == 0 {
            continue;
        }
        let record_size = choose_playlist_record_size(scan, reference.data_length)?;
        let arr = &mut scan.arrangements[arr_index];
        arr.record_size = record_size;
        let records = reference.data_length / record_size;
        clips.reserve(records);

        let payload = &data[reference.data_offset..reference.data_offset + reference.data_length];
        for index in 0..records {
            let raw = &payload[index * record_size..(index + 1) * record_size];
            let position = read_u32(raw, 0);
            let pattern_base = read_u16(raw, 4);
            let item_index = read_u16(raw, 6);
            let length = read_u32(raw, 8);
            if item_index <= pattern_base {
                continue; // Audio Clip or Automation Clip.
            }

            total_pattern_clips += 1;
            let pattern_id = item_index - pattern_base;
            let flags = read_u16(raw, 18);
            let muted = flags & MUTED_CLIP_FLAG != 0;
            let start_offset = read_i32(raw, 24);
            let end_offset = read_i32(raw, 28);

            // Prefer a normal, non-stretched source record as the skeleton for
            // the replacement clip. This avoids carrying clip-specific
            // stretch/variant state from an unusual first item.
            let plain_template = (start_offset < 0 && end_offset < 0)
                || (start_offset >= 0
                    && end_offset >= start_offset
                    && (end_offset as i64 - start_offset as i64) == length as i64);
            if arr.template.is_none() || (plain_template && !arr.template_is_plain) {
                arr.template = Some(raw.to_vec());
                arr.template_is_plain = plain_template;
            }
            arr.pattern_clip_count += 1;
            arr.timeline_length = arr.timeline_length.max(position as u64 + length as u64);

            if muted && !include_muted_clips {
                continue;
            }
            if length == 0 {
                continue;
            }

            clips.push(Clip {
                arr_index: arr_index as u32,
                position,
                length,
                pattern_id,
                start_offset,
                end_offset,
            });
            arr.included_clip_count += 1;
            included_pattern_clips += 1;
        }
    }

    if included_pattern_clips == 0 {
        let reason = if include_muted_clips {
            ""
        } else {
            " after muted clips were skipped"
        };
        return Err(MergeError::format(format!(
            "No usable Pattern Clips were found in any Playlist{reason}."
        )));
    }

    assign_section_offsets(scan)?;
    Ok(ClipIndex {
        clips,
        total_pattern_clips,
        included_pattern_clips,
    })
}

/// Put every arrangement in a non-overlapping section of the one target pattern.
fn assign_section_offsets(scan: &mut ScanResult) -> Result<()> {
    let mut cursor: u64 = 0;
    let bar = (scan.ppq as u64 * 4).max(1);
    for arr in &mut scan.arrangements {
        if cursor % bar != 0 {
            cursor += bar - (cursor % bar);
        }
        arr.section_offset = cursor;
        cursor += arr.timeline_length;
        if arr.timeline_length != 0 {
            cursor += bar; // one safety bar between arrangements
        }
        if cursor > INT32_MAX {
            return Err(MergeError::format(
                "The combined arrangements exceed FL Studio's signed Pattern Clip offset range.",
            ));
        }
    }
    Ok(())
}
