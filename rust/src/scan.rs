//! Single-pass structural scan of the FLdt event stream.
//!
//! The source file is mapped once and walked as a byte slice, so the scan costs
//! one pass over the *event headers* rather than one syscall per event. Large
//! plugin/sample blobs are skipped by their length field and never touched.

use crate::error::{MergeError, Result};
use crate::flp::*;

/// Location of one event payload inside the mapped file.
#[derive(Clone, Copy, Debug)]
pub struct EventRef {
    pub event_start: usize,
    pub data_offset: usize,
    pub data_length: usize,
}

#[derive(Clone, Debug)]
pub struct ArrangementInfo {
    pub iid: u16,
    pub playlist: Option<EventRef>,
    pub record_size: usize,
    /// Skeleton Playlist record reused for the replacement Pattern Clip.
    pub template: Option<Vec<u8>>,
    pub template_is_plain: bool,
    pub pattern_clip_count: u64,
    pub included_clip_count: u64,
    pub timeline_length: u64,
    pub section_offset: u64,
}

impl ArrangementInfo {
    fn new(iid: u16) -> Self {
        ArrangementInfo {
            iid,
            playlist: None,
            record_size: PLAYLIST_OLD_SIZE,
            template: None,
            template_is_plain: false,
            pattern_clip_count: 0,
            included_clip_count: 0,
            timeline_length: 0,
            section_offset: 0,
        }
    }
}

pub struct ScanResult {
    pub file_size: usize,
    pub header_length: usize,
    pub ppq: u16,
    pub data_length_pos: usize,
    pub data_offset: usize,
    pub data_length: usize,
    pub data_end: usize,
    pub suffix_offset: usize,
    pub version_text: String,
    pub version_major: u32,
    pub current_arrangement: u16,
    pub play_truncated_notes: bool,

    /// Arrangements in discovery order.
    pub arrangements: Vec<ArrangementInfo>,
    /// Arrangement id -> index into `arrangements` (`u32::MAX` = absent).
    arr_index: Vec<u32>,

    /// CSR index of note events per pattern: `pat_start[id]..pat_start[id + 1]`.
    pat_start: Vec<u32>,
    pat_notes: Vec<EventRef>,

    pub target_pattern_id: u16,
    pub target_note_event_start: usize,
    pub source_note_count: u64,
    /// Number of distinct patterns that carry a note payload.
    pub patterns_with_notes: usize,
}

impl ScanResult {
    /// Note events belonging to `pattern_id`, in file order.
    #[inline]
    pub fn notes_of(&self, pattern_id: u16) -> &[EventRef] {
        let id = pattern_id as usize;
        if id + 1 >= self.pat_start.len() {
            return &[];
        }
        let start = self.pat_start[id] as usize;
        let end = self.pat_start[id + 1] as usize;
        &self.pat_notes[start..end]
    }

    #[inline]
    pub fn arrangement_index(&self, iid: u16) -> Option<usize> {
        let slot = *self.arr_index.get(iid as usize)?;
        if slot == u32::MAX {
            None
        } else {
            Some(slot as usize)
        }
    }
}

/// Walk FLhd/FLdt once and record everything the merge needs.
pub fn scan_flp(data: &[u8]) -> Result<ScanResult> {
    let file_size = data.len();
    if file_size < 12 || &data[0..4] != b"FLhd" {
        return Err(MergeError::format(
            "Not an FL Studio project: FLhd header is missing.",
        ));
    }
    let header_length = read_u32(data, 4) as usize;
    if !(6..=1024 * 1024).contains(&header_length) {
        return Err(MergeError::format(format!(
            "Unsupported FLP header size: {header_length} bytes."
        )));
    }
    if 8 + header_length + 8 > file_size {
        return Err(MergeError::format("Unexpected end of file."));
    }
    let ppq = read_u16(data, 8 + 4);
    if !(PPQ_MIN..=u16::MAX).contains(&ppq) {
        return Err(MergeError::format(format!(
            "The FLP PPQ/timebase must be between {PPQ_MIN} and {}; found {ppq}.",
            u16::MAX
        )));
    }

    let chunk_at = 8 + header_length;
    if &data[chunk_at..chunk_at + 4] != b"FLdt" {
        return Err(MergeError::format(
            "Not an FL Studio project: FLdt data chunk is missing.",
        ));
    }
    let data_length_pos = chunk_at + 4;
    let data_length = read_u32(data, data_length_pos) as usize;
    let data_offset = data_length_pos + 4;
    let data_end = data_offset + data_length;
    if data_end > file_size {
        return Err(MergeError::format(
            "The FLdt chunk extends past the end of the file.",
        ));
    }

    let mut version_text = String::from("unknown");
    let mut version_major = 0u32;
    let mut current_arrangement_event = 0u16;
    let mut play_truncated_notes = true;

    let mut arrangements: Vec<ArrangementInfo> = Vec::new();
    let mut arr_index: Vec<u32> = Vec::new();

    // (pattern id, event) in file order; regrouped into CSR after the walk.
    let mut note_refs: Vec<(u16, EventRef)> = Vec::new();
    let mut max_pattern_id: usize = 0;

    let mut current_pattern: Option<u16> = None;
    let mut current_arrangement: Option<usize> = None;
    let mut target: Option<(u16, usize)> = None;
    let mut source_note_count: u64 = 0;

    let mut cursor = data_offset;
    while cursor < data_end {
        let event = next_event(data, cursor, data_end)?;
        let payload = event.data_offset;

        match event.id {
            EV_NEW_PATTERN => {
                let id = read_u16(data, payload);
                current_pattern = Some(id);
                max_pattern_id = max_pattern_id.max(id as usize);
            }
            EV_NEW_ARRANGEMENT => {
                let id = read_u16(data, payload);
                let slot = id as usize;
                if slot >= arr_index.len() {
                    arr_index.resize(slot + 1, u32::MAX);
                }
                if arr_index[slot] == u32::MAX {
                    arr_index[slot] = arrangements.len() as u32;
                    arrangements.push(ArrangementInfo::new(id));
                }
                current_arrangement = Some(arr_index[slot] as usize);
            }
            EV_CURRENT_ARRANGEMENT => {
                current_arrangement_event = read_u16(data, payload);
            }
            EV_PLAY_TRUNCATED => {
                play_truncated_notes = data[payload] != 0;
            }
            EV_FL_VERSION => {
                let take = event.data_length.min(256);
                version_text = decode_c_string(&data[payload..payload + take]);
                version_major = parse_version_major(&version_text);
            }
            EV_PATTERN_NAME => {
                // Pattern names are display-only and never affect the output.
            }
            EV_PATTERN_NOTES => {
                let Some(pattern_id) = current_pattern else {
                    return Err(MergeError::format(
                        "Found a note event before any pattern identifier.",
                    ));
                };
                if event.data_length % NOTE_SIZE != 0 {
                    return Err(MergeError::format(format!(
                        "Pattern {pattern_id} has a malformed note payload ({} bytes).",
                        event.data_length
                    )));
                }
                note_refs.push((
                    pattern_id,
                    EventRef {
                        event_start: cursor,
                        data_offset: payload,
                        data_length: event.data_length,
                    },
                ));
                max_pattern_id = max_pattern_id.max(pattern_id as usize);
                source_note_count += (event.data_length / NOTE_SIZE) as u64;
                if target.is_none() && event.data_length != 0 {
                    target = Some((pattern_id, cursor));
                }
            }
            EV_PLAYLIST => {
                let index = match current_arrangement {
                    Some(index) => index,
                    None => {
                        // Very old projects may omit an explicit arrangement marker.
                        if arr_index.is_empty() {
                            arr_index.resize(1, u32::MAX);
                        }
                        if arr_index[0] == u32::MAX {
                            arr_index[0] = arrangements.len() as u32;
                            arrangements.push(ArrangementInfo::new(0));
                        }
                        let index = arr_index[0] as usize;
                        current_arrangement = Some(index);
                        index
                    }
                };
                arrangements[index].playlist = Some(EventRef {
                    event_start: cursor,
                    data_offset: payload,
                    data_length: event.data_length,
                });
            }
            _ => {}
        }

        cursor = event.next;
    }

    if cursor != data_end {
        return Err(MergeError::format(
            "FLdt event stream did not end on an event boundary.",
        ));
    }

    if arrangements.is_empty() {
        // A pattern-only project still gets a synthetic empty arrangement entry.
        arr_index.resize(1, u32::MAX);
        arr_index[0] = 0;
        arrangements.push(ArrangementInfo::new(0));
    }

    let Some((target_pattern_id, target_note_event_start)) = target else {
        return Err(MergeError::format(
            "No piano-roll/step-sequencer notes were found. A project with at least one note is required.",
        ));
    };

    // Counting sort of the note events into a per-pattern CSR index. Stable, so
    // events keep file order inside each pattern.
    let table = max_pattern_id + 2;
    let mut pat_start = vec![0u32; table];
    for (pattern_id, _) in &note_refs {
        pat_start[*pattern_id as usize + 1] += 1;
    }
    for i in 1..table {
        pat_start[i] += pat_start[i - 1];
    }
    let mut fill = pat_start.clone();
    let mut pat_notes = vec![
        EventRef {
            event_start: 0,
            data_offset: 0,
            data_length: 0
        };
        note_refs.len()
    ];
    for (pattern_id, event) in &note_refs {
        let slot = &mut fill[*pattern_id as usize];
        pat_notes[*slot as usize] = *event;
        *slot += 1;
    }
    let patterns_with_notes = (0..table - 1)
        .filter(|&id| pat_start[id + 1] > pat_start[id])
        .count();

    Ok(ScanResult {
        file_size,
        header_length,
        ppq,
        data_length_pos,
        data_offset,
        data_length,
        data_end,
        suffix_offset: data_end,
        version_text,
        version_major,
        current_arrangement: current_arrangement_event,
        play_truncated_notes,
        arrangements,
        arr_index,
        pat_start,
        pat_notes,
        target_pattern_id,
        target_note_event_start,
        source_note_count,
        patterns_with_notes,
    })
}
