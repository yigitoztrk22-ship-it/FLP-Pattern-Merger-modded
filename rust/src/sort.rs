//! Optional global ordering for `--sorted` compatibility mode.
//!
//! FL Studio reads note records by their stored positions, so turbo mode skips
//! this entirely. When a globally ordered payload is requested, a stable LSD
//! radix sort over the 32-bit position field replaces the reference build's
//! external merge sort — no temporary files, four linear passes.

use crate::expand::MergedNotes;
use crate::flp::{read_u32, NOTE_POSITION, NOTE_SIZE};

pub fn sort_by_position(notes: &mut MergedNotes) {
    let count = notes.count;
    if count < 2 {
        return;
    }

    let keys: Vec<u32> = notes
        .bytes
        .chunks_exact(NOTE_SIZE)
        .map(|record| read_u32(record, NOTE_POSITION))
        .collect();

    let mut source: Vec<u32> = (0..count as u32).collect();
    let mut target: Vec<u32> = vec![0; count];

    for shift in [0u32, 8, 16, 24] {
        let mut counts = [0u32; 256];
        for &index in &source {
            counts[((keys[index as usize] >> shift) & 0xFF) as usize] += 1;
        }
        // A single populated bucket means this digit cannot reorder anything.
        if counts.iter().any(|&n| n as usize == count) {
            continue;
        }
        let mut running = 0u32;
        for slot in counts.iter_mut() {
            let current = *slot;
            *slot = running;
            running += current;
        }
        for &index in &source {
            let digit = ((keys[index as usize] >> shift) & 0xFF) as usize;
            target[counts[digit] as usize] = index;
            counts[digit] += 1;
        }
        std::mem::swap(&mut source, &mut target);
    }

    let mut ordered = vec![0u8; notes.bytes.len()];
    for (slot, &index) in source.iter().enumerate() {
        let from = index as usize * NOTE_SIZE;
        ordered[slot * NOTE_SIZE..(slot + 1) * NOTE_SIZE]
            .copy_from_slice(&notes.bytes[from..from + NOTE_SIZE]);
    }
    notes.bytes = ordered;
}
