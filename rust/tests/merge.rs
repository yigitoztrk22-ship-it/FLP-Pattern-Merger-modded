//! Behavioural tests for the merge engine.
//!
//! Each test builds a minimal FLP in memory, merges it, and inspects the notes
//! of the resulting merged pattern. The interesting cases are all timing ones:
//! cropped left edges, tails past the right edge, stretch ratios and the
//! per-arrangement section layout.

use flp_note_merger::error::MergeError;
use flp_note_merger::flp::*;
use flp_note_merger::{merge_in_memory, MergeOptions};

const PATTERN_BASE: u16 = 20481;

// ------------------------------- fixtures --------------------------------

#[derive(Clone, Copy)]
struct PlRec {
    position: u32,
    length: u32,
    /// `None` builds an Audio/Automation Clip, which the merger must ignore.
    pattern: Option<u16>,
    start: i32,
    end: i32,
    muted: bool,
}

impl PlRec {
    fn plain(position: u32, length: u32, pattern: u16) -> Self {
        PlRec {
            position,
            length,
            pattern: Some(pattern),
            start: 0,
            end: length as i32,
            muted: false,
        }
    }
    fn window(mut self, start: i32, end: i32) -> Self {
        self.start = start;
        self.end = end;
        self
    }
    fn muted(mut self) -> Self {
        self.muted = true;
        self
    }
}

struct FlpBuilder {
    ppq: u16,
    events: Vec<u8>,
    suffix: Vec<u8>,
}

impl FlpBuilder {
    fn new(ppq: u16) -> Self {
        let mut builder = FlpBuilder {
            ppq,
            events: Vec::new(),
            suffix: Vec::new(),
        };
        builder.blob(EV_FL_VERSION, b"21.0.3.3517\0");
        builder
    }

    fn byte(&mut self, id: u8, value: u8) -> &mut Self {
        self.events.extend_from_slice(&[id, value]);
        self
    }
    fn word(&mut self, id: u8, value: u16) -> &mut Self {
        self.events.push(id);
        self.events.extend_from_slice(&value.to_le_bytes());
        self
    }
    fn dword(&mut self, id: u8, value: u32) -> &mut Self {
        self.events.push(id);
        self.events.extend_from_slice(&value.to_le_bytes());
        self
    }
    fn blob(&mut self, id: u8, payload: &[u8]) -> &mut Self {
        self.events.push(id);
        push_varint(&mut self.events, payload.len());
        self.events.extend_from_slice(payload);
        self
    }

    fn play_truncated(&mut self, on: bool) -> &mut Self {
        self.byte(EV_PLAY_TRUNCATED, on as u8)
    }

    fn pattern(&mut self, id: u16) -> &mut Self {
        self.word(EV_NEW_PATTERN, id);
        self.dword(EV_PATTERN_LENGTH, 0);
        self.blob(EV_PATTERN_CONTROLLERS, &[1, 2, 3, 4]);
        self
    }

    /// `(position, length)` pairs. Bytes 12..24 carry a tag that must survive.
    fn notes(&mut self, notes: &[(u32, u32)]) -> &mut Self {
        let mut payload = Vec::with_capacity(notes.len() * NOTE_SIZE);
        for (index, &(position, length)) in notes.iter().enumerate() {
            let mut record = [0u8; NOTE_SIZE];
            record[..4].copy_from_slice(&position.to_le_bytes());
            record[4..6].copy_from_slice(&0xBEEFu16.to_le_bytes());
            record[6..8].copy_from_slice(&7u16.to_le_bytes()); // rack channel
            record[8..12].copy_from_slice(&length.to_le_bytes());
            record[12] = 60 + index as u8; // key
            record[13..24].copy_from_slice(&[index as u8; 11]);
            payload.extend_from_slice(&record);
        }
        self.blob(EV_PATTERN_NOTES, &payload)
    }

    fn arrangement(&mut self, id: u16) -> &mut Self {
        self.word(EV_NEW_ARRANGEMENT, id)
    }

    fn playlist(&mut self, records: &[PlRec], record_size: usize) -> &mut Self {
        let mut payload = vec![0u8; records.len() * record_size];
        for (index, record) in records.iter().enumerate() {
            let slot = &mut payload[index * record_size..(index + 1) * record_size];
            write_u32(slot, 0, record.position);
            write_u16(slot, 4, PATTERN_BASE);
            let item_index = match record.pattern {
                Some(pattern) => PATTERN_BASE + pattern,
                None => PATTERN_BASE - 1,
            };
            write_u16(slot, 6, item_index);
            write_u32(slot, 8, record.length);
            write_u16(slot, 18, if record.muted { MUTED_CLIP_FLAG } else { 0 });
            write_i32(slot, 24, record.start);
            write_i32(slot, 28, record.end);
        }
        self.blob(EV_PLAYLIST, &payload)
    }

    fn suffix(&mut self, bytes: &[u8]) -> &mut Self {
        self.suffix.extend_from_slice(bytes);
        self
    }

    fn build(&self) -> Vec<u8> {
        let header = {
            let mut header = Vec::new();
            header.extend_from_slice(&0u16.to_le_bytes()); // format
            header.extend_from_slice(&16u16.to_le_bytes()); // channels
            header.extend_from_slice(&self.ppq.to_le_bytes());
            header
        };
        let mut out = Vec::new();
        out.extend_from_slice(b"FLhd");
        out.extend_from_slice(&(header.len() as u32).to_le_bytes());
        out.extend_from_slice(&header);
        out.extend_from_slice(b"FLdt");
        out.extend_from_slice(&(self.events.len() as u32).to_le_bytes());
        out.extend_from_slice(&self.events);
        out.extend_from_slice(&self.suffix);
        out
    }
}

// ------------------------------- inspection -------------------------------

/// Every event in an FLP, as `(id, payload)`.
fn events_of(project: &[u8]) -> Vec<(u8, Vec<u8>)> {
    let header_length = read_u32(project, 4) as usize;
    let data_length_pos = 8 + header_length + 4;
    let data_offset = data_length_pos + 4;
    let data_end = data_offset + read_u32(project, data_length_pos) as usize;
    let mut out = Vec::new();
    let mut cursor = data_offset;
    while cursor < data_end {
        let event = next_event(project, cursor, data_end).expect("valid event");
        out.push((
            event.id,
            project[event.data_offset..event.data_offset + event.data_length].to_vec(),
        ));
        cursor = event.next;
    }
    out
}

/// The merged pattern's `(position, length)` pairs.
fn merged_notes(project: &[u8]) -> Vec<(u32, u32)> {
    let payloads: Vec<Vec<u8>> = events_of(project)
        .into_iter()
        .filter(|(id, payload)| *id == EV_PATTERN_NOTES && !payload.is_empty())
        .map(|(_, payload)| payload)
        .collect();
    assert_eq!(payloads.len(), 1, "exactly one pattern keeps its notes");
    payloads[0]
        .chunks_exact(NOTE_SIZE)
        .map(|record| {
            (
                read_u32(record, NOTE_POSITION),
                read_u32(record, NOTE_LENGTH),
            )
        })
        .collect()
}

fn merged_records(project: &[u8]) -> Vec<Vec<u8>> {
    let payloads: Vec<Vec<u8>> = events_of(project)
        .into_iter()
        .filter(|(id, payload)| *id == EV_PATTERN_NOTES && !payload.is_empty())
        .map(|(_, payload)| payload)
        .collect();
    payloads[0]
        .chunks_exact(NOTE_SIZE)
        .map(|record| record.to_vec())
        .collect()
}

fn merge(project: &[u8]) -> Vec<u8> {
    merge_in_memory(project, &MergeOptions::default()).expect("merge succeeds")
}

// --------------------------------- tests ----------------------------------

#[test]
fn varint_round_trips() {
    for value in [
        0usize,
        1,
        127,
        128,
        300,
        16383,
        16384,
        1 << 21,
        usize::from(u16::MAX),
    ] {
        let mut encoded = Vec::new();
        push_varint(&mut encoded, value);
        let mut cursor = 0;
        let decoded = read_varint(&encoded, &mut cursor, encoded.len()).expect("decodes");
        assert_eq!(decoded, value, "round trip {value}");
        assert_eq!(cursor, encoded.len(), "consumed every byte for {value}");
    }
}

#[test]
fn plain_clip_shifts_notes_by_clip_position() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48), (96, 96), (192, 24)])
        .arrangement(0)
        .playlist(&[PlRec::plain(384, 384, 1)], 60);

    assert_eq!(
        merged_notes(&merge(&builder.build())),
        [(384, 48), (480, 96), (576, 24)]
    );
}

#[test]
fn tails_may_extend_past_the_right_edge() {
    // The 1.1 fix: a note starting inside the clip keeps its whole duration
    // even when it runs past the clip's visible end.
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 96 * 64)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 96 * 4, 1)], 60);

    assert_eq!(merged_notes(&merge(&builder.build())), [(0, 96 * 64)]);
}

#[test]
fn cropped_left_edge_follows_play_truncated_setting() {
    let make = |play_truncated: bool| {
        let mut builder = FlpBuilder::new(96);
        builder
            .play_truncated(play_truncated)
            .pattern(1)
            // Starts at 0, still sounding when the clip's window opens at 96.
            .notes(&[(0, 384), (192, 48)])
            .arrangement(0)
            .playlist(&[PlRec::plain(1000, 384, 1).window(96, 96 + 384)], 60);
        merged_notes(&merge(&builder.build()))
    };

    // Restored: only the clipped-off left part is removed, the rest plays.
    assert_eq!(make(true), [(1000, 288), (1096, 48)]);
    // Disabled: the truncated note is dropped entirely.
    assert_eq!(make(false), [(1096, 48)]);
}

#[test]
fn notes_outside_the_window_are_dropped() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48), (96, 48), (480, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1).window(96, 288)], 60);

    // Only the note starting inside [96, 288) survives.
    assert_eq!(merged_notes(&merge(&builder.build())), [(0, 48)]);
}

#[test]
fn zero_length_notes_keep_zero_length() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 0), (96, 0), (500, 0)])
        .arrangement(0)
        .playlist(&[PlRec::plain(10, 192, 1)], 60);

    // The step at 500 lies outside the 192-tick window.
    assert_eq!(merged_notes(&merge(&builder.build())), [(10, 0), (106, 0)]);
}

#[test]
fn stretched_clips_scale_positions_and_durations() {
    let mut builder = FlpBuilder::new(96);
    // Source window is 384 ticks shown in a 192-tick clip: half speed.
    builder
        .pattern(1)
        .notes(&[(0, 96), (192, 96), (288, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1).window(0, 384)], 60);

    assert_eq!(
        merged_notes(&merge(&builder.build())),
        [(0, 48), (96, 48), (144, 24)]
    );
}

#[test]
fn expanded_clips_scale_up() {
    let mut builder = FlpBuilder::new(96);
    // Source window is 96 ticks shown in a 384-tick clip: 4x slower.
    builder
        .pattern(1)
        .notes(&[(0, 24), (48, 24)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 384, 1).window(0, 96)], 60);

    assert_eq!(merged_notes(&merge(&builder.build())), [(0, 96), (192, 96)]);
}

#[test]
fn every_other_note_byte_is_preserved() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48), (96, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(240, 192, 1)], 60);

    let records = merged_records(&merge(&builder.build()));
    assert_eq!(records.len(), 2);
    for (index, record) in records.iter().enumerate() {
        assert_eq!(read_u16(record, 4), 0xBEEF, "flags preserved");
        assert_eq!(read_u16(record, 6), 7, "rack channel preserved");
        assert_eq!(record[12], 60 + index as u8, "key preserved");
        assert_eq!(&record[13..24], &[index as u8; 11], "tail bytes preserved");
    }
}

#[test]
fn reused_pattern_expands_to_every_placement() {
    // Exercises the shape cache: one pattern, three placements.
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48), (96, 48)])
        .arrangement(0)
        .playlist(
            &[
                PlRec::plain(0, 192, 1),
                PlRec::plain(192, 192, 1),
                PlRec::plain(1000, 192, 1),
            ],
            60,
        );

    assert_eq!(
        merged_notes(&merge(&builder.build())),
        [
            (0, 48),
            (96, 48),
            (192, 48),
            (288, 48),
            (1000, 48),
            (1096, 48)
        ]
    );
}

#[test]
fn audio_and_automation_clips_are_ignored() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .arrangement(0)
        .playlist(
            &[
                PlRec {
                    position: 0,
                    length: 192,
                    pattern: None,
                    start: -1,
                    end: -1,
                    muted: false,
                },
                PlRec::plain(192, 192, 1),
            ],
            60,
        );

    assert_eq!(merged_notes(&merge(&builder.build())), [(192, 48)]);
}

#[test]
fn muted_clips_follow_the_option() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .arrangement(0)
        .playlist(
            &[PlRec::plain(0, 192, 1), PlRec::plain(192, 192, 1).muted()],
            60,
        );
    let project = builder.build();

    let included = merge_in_memory(&project, &MergeOptions::default()).expect("merge");
    assert_eq!(merged_notes(&included), [(0, 48), (192, 48)]);

    let options = MergeOptions {
        include_muted_clips: false,
        ..MergeOptions::default()
    };
    let skipped = merge_in_memory(&project, &options).expect("merge");
    assert_eq!(merged_notes(&skipped), [(0, 48)]);
}

#[test]
fn arrangements_land_in_separate_sections() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1)], 60)
        .arrangement(1)
        .playlist(&[PlRec::plain(0, 192, 1)], 60);

    // Arrangement 0 occupies [0, 192); arrangement 1 starts after that plus one
    // safety bar (384 ticks), rounded up to the next bar boundary.
    assert_eq!(merged_notes(&merge(&builder.build())), [(0, 48), (768, 48)]);
}

#[test]
fn redundant_payloads_are_cleared_and_the_suffix_survives() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .pattern(2)
        .notes(&[(0, 48), (96, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1), PlRec::plain(192, 192, 2)], 60)
        .suffix(b"trailing-bytes");

    let output = merge(&builder.build());
    let events = events_of(&output);

    let note_payloads: Vec<usize> = events
        .iter()
        .filter(|(id, _)| *id == EV_PATTERN_NOTES)
        .map(|(_, payload)| payload.len())
        .collect();
    assert_eq!(note_payloads.len(), 2, "both note events are still present");
    assert_eq!(note_payloads[1], 0, "the redundant payload is emptied");

    assert!(
        events
            .iter()
            .filter(|(id, _)| *id == EV_PATTERN_CONTROLLERS)
            .all(|(_, payload)| payload.is_empty()),
        "pattern automation is cleared"
    );

    // The target pattern's length is reset so FL derives it from the notes.
    let lengths: Vec<Vec<u8>> = events
        .iter()
        .filter(|(id, _)| *id == EV_PATTERN_LENGTH)
        .map(|(_, payload)| payload.clone())
        .collect();
    assert_eq!(lengths[0], vec![0, 0, 0, 0]);

    assert!(output.ends_with(b"trailing-bytes"), "suffix preserved");
}

#[test]
fn each_playlist_becomes_one_pattern_clip() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1), PlRec::plain(192, 192, 1)], 60);

    let output = merge(&builder.build());
    let playlists: Vec<Vec<u8>> = events_of(&output)
        .into_iter()
        .filter(|(id, _)| *id == EV_PLAYLIST)
        .map(|(_, payload)| payload)
        .collect();

    assert_eq!(playlists.len(), 1);
    assert_eq!(playlists[0].len(), 60, "exactly one 60-byte record");
    assert_eq!(
        read_u32(&playlists[0], 0),
        0,
        "clip starts at the timeline top"
    );
    assert_eq!(
        read_u32(&playlists[0], 8),
        384,
        "covers the whole arrangement"
    );
    assert_eq!(
        read_u16(&playlists[0], 6),
        PATTERN_BASE + 1,
        "targets pattern 1"
    );
    assert_eq!(
        read_u16(&playlists[0], 18) & MUTED_CLIP_FLAG,
        0,
        "not muted"
    );
}

#[test]
fn sorted_mode_orders_by_position_and_keeps_every_note() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48), (96, 48)])
        .arrangement(0)
        .playlist(
            &[
                PlRec::plain(1000, 192, 1),
                PlRec::plain(0, 192, 1),
                PlRec::plain(500, 192, 1),
            ],
            60,
        );
    let project = builder.build();

    let turbo = merged_notes(&merge(&project));
    let options = MergeOptions {
        turbo_mode: false,
        ..MergeOptions::default()
    };
    let mut sorted = merged_notes(&merge_in_memory(&project, &options).expect("merge"));

    assert!(
        sorted.windows(2).all(|pair| pair[0].0 <= pair[1].0),
        "positions ascend: {sorted:?}"
    );
    let mut expected = turbo;
    expected.sort();
    sorted.sort();
    assert_eq!(sorted, expected, "same notes, different order");
}

#[test]
fn single_threaded_matches_parallel() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48), (96, 96), (192, 0)])
        .arrangement(0);
    let records: Vec<PlRec> = (0..500)
        .map(|index| PlRec::plain(index * 192, 192, 1).window(0, 192 + (index % 3) as i32 * 96))
        .collect();
    builder.playlist(&records, 60);
    let project = builder.build();

    let parallel = merge_in_memory(&project, &MergeOptions::default()).expect("merge");
    let serial = merge_in_memory(
        &project,
        &MergeOptions {
            parallel: false,
            ..MergeOptions::default()
        },
    )
    .expect("merge");
    assert_eq!(parallel, serial, "thread count must not change the output");
}

#[test]
fn rejects_files_that_are_not_projects() {
    let error = merge_in_memory(b"not an flp at all", &MergeOptions::default()).unwrap_err();
    assert!(matches!(error, MergeError::Format(_)), "got {error:?}");
    assert!(error.to_string().contains("FLhd"));
}

#[test]
fn rejects_a_project_without_notes() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1)], 60);

    let error = merge_in_memory(&builder.build(), &MergeOptions::default()).unwrap_err();
    assert!(error.to_string().contains("at least one note"), "{error}");
}

#[test]
fn rejects_a_project_without_pattern_clips() {
    let mut builder = FlpBuilder::new(96);
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .arrangement(0)
        .playlist(
            &[PlRec {
                position: 0,
                length: 192,
                pattern: None,
                start: -1,
                end: -1,
                muted: false,
            }],
            60,
        );

    let error = merge_in_memory(&builder.build(), &MergeOptions::default()).unwrap_err();
    assert!(
        error.to_string().contains("No usable Pattern Clips"),
        "{error}"
    );
}

#[test]
fn rejects_an_unsupported_timebase() {
    let mut builder = FlpBuilder::new(8); // below the 24 PPQ floor
    builder
        .pattern(1)
        .notes(&[(0, 48)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1)], 60);

    let error = merge_in_memory(&builder.build(), &MergeOptions::default()).unwrap_err();
    assert!(error.to_string().contains("PPQ/timebase"), "{error}");
}

#[test]
fn handles_old_32_byte_playlist_records() {
    let mut builder = FlpBuilder::new(24);
    builder.events.clear();
    builder.blob(EV_FL_VERSION, b"12.5.1.165\0");
    builder
        .pattern(1)
        .notes(&[(0, 12), (24, 12)])
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 48, 1)], 32);

    assert_eq!(merged_notes(&merge(&builder.build())), [(0, 12), (24, 12)]);
}

#[test]
fn handles_high_timebases() {
    let mut builder = FlpBuilder::new(960);
    builder
        .pattern(1)
        .notes(&[(0, 480), (960, 480)])
        .arrangement(0)
        .playlist(&[PlRec::plain(3840, 1920, 1)], 60);

    assert_eq!(
        merged_notes(&merge(&builder.build())),
        [(3840, 480), (4800, 480)]
    );
}

#[test]
fn multiple_note_events_in_one_pattern_are_all_merged() {
    let mut builder = FlpBuilder::new(96);
    builder.pattern(1).notes(&[(0, 48)]).notes(&[(96, 48)]);
    builder
        .arrangement(0)
        .playlist(&[PlRec::plain(0, 192, 1)], 60);

    assert_eq!(merged_notes(&merge(&builder.build())), [(0, 48), (96, 48)]);
}
