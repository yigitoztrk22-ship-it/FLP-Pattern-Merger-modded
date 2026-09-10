//! FLP container primitives: event ids, record sizes, and variable-integer codecs.

use crate::error::{MergeError, Result};

// FLP event IDs used by this tool.
pub const EV_PLAY_TRUNCATED: u8 = 30;
pub const EV_NEW_PATTERN: u8 = 65;
pub const EV_NEW_ARRANGEMENT: u8 = 99;
pub const EV_CURRENT_ARRANGEMENT: u8 = 100;
pub const EV_PATTERN_LENGTH: u8 = 164;
pub const EV_PATTERN_NAME: u8 = 193;
pub const EV_FL_VERSION: u8 = 199;
pub const EV_PATTERN_CONTROLLERS: u8 = 223;
pub const EV_PATTERN_NOTES: u8 = 224;
pub const EV_PLAYLIST: u8 = 233;

pub const NOTE_SIZE: usize = 24;
pub const NOTE_KEY: usize = 12;
pub const NOTE_VELOCITY: usize = 21;
pub const PLAYLIST_OLD_SIZE: usize = 32;
pub const PLAYLIST_NEW_SIZE: usize = 60;
pub const UINT32_MAX: u64 = 0xFFFF_FFFF;
pub const INT32_MAX: u64 = 0x7FFF_FFFF;
pub const PPQ_MIN: u16 = 24;
pub const MUTED_CLIP_FLAG: u16 = 0x2000;

/// Byte offsets patched inside a 24-byte FL note record.
pub const NOTE_POSITION: usize = 0;
pub const NOTE_LENGTH: usize = 8;

#[inline(always)]
pub fn read_u16(data: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([data[at], data[at + 1]])
}

#[inline(always)]
pub fn read_u32(data: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([data[at], data[at + 1], data[at + 2], data[at + 3]])
}

#[inline(always)]
pub fn read_i32(data: &[u8], at: usize) -> i32 {
    read_u32(data, at) as i32
}

#[inline(always)]
pub fn write_u16(data: &mut [u8], at: usize, value: u16) {
    data[at..at + 2].copy_from_slice(&value.to_le_bytes());
}

#[inline(always)]
pub fn write_u32(data: &mut [u8], at: usize, value: u32) {
    data[at..at + 4].copy_from_slice(&value.to_le_bytes());
}

#[inline(always)]
pub fn write_i32(data: &mut [u8], at: usize, value: i32) {
    data[at..at + 4].copy_from_slice(&value.to_le_bytes());
}

/// Decode an FLP variable-length integer starting at `pos`, advancing the cursor.
#[inline]
pub fn read_varint(data: &[u8], pos: &mut usize, boundary: usize) -> Result<usize> {
    let mut value: usize = 0;
    let mut shift: u32 = 0;
    for _ in 0..10 {
        if *pos >= boundary {
            return Err(MergeError::format("Truncated FLP variable-length event."));
        }
        let byte = data[*pos];
        *pos += 1;
        value |= ((byte & 0x7F) as usize) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
        shift += 7;
    }
    Err(MergeError::format(
        "FLP event length uses an invalid variable integer.",
    ))
}

/// Append an FLP variable-length integer.
#[inline]
pub fn push_varint(out: &mut Vec<u8>, mut value: usize) {
    loop {
        let byte = (value & 0x7F) as u8;
        value >>= 7;
        if value != 0 {
            out.push(byte | 0x80);
        } else {
            out.push(byte);
            return;
        }
    }
}

/// One decoded event header.
pub struct EventHeader {
    pub id: u8,
    pub data_offset: usize,
    pub data_length: usize,
    pub next: usize,
}

/// Decode the event beginning at `event_start`. `boundary` is the end of FLdt.
#[inline]
pub fn next_event(data: &[u8], event_start: usize, boundary: usize) -> Result<EventHeader> {
    if event_start >= boundary {
        return Err(MergeError::format("Unexpected end of FLP event stream."));
    }
    let id = data[event_start];
    let mut pos = event_start + 1;
    let data_length = if id < 64 {
        1
    } else if id < 128 {
        2
    } else if id < 192 {
        4
    } else {
        read_varint(data, &mut pos, boundary)?
    };
    let data_offset = pos;
    let next = data_offset
        .checked_add(data_length)
        .ok_or_else(|| MergeError::format("FLP event length overflows the address space."))?;
    if next > boundary {
        return Err(MergeError::format(format!(
            "Event {id} at offset {event_start} extends outside FLdt."
        )));
    }
    Ok(EventHeader {
        id,
        data_offset,
        data_length,
        next,
    })
}

/// Read the leading ASCII C string of an FL version blob.
pub fn decode_c_string(data: &[u8]) -> String {
    let end = data.iter().position(|&b| b == 0).unwrap_or(data.len());
    data[..end].iter().map(|&b| b as char).collect()
}

/// `21.0.3.3517` -> 21. Returns 0 when the text is not a version.
pub fn parse_version_major(text: &str) -> u32 {
    text.split('.')
        .next()
        .and_then(|head| head.trim().parse::<u32>().ok())
        .unwrap_or(0)
}
