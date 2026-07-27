#!/usr/bin/env python3
"""Generate synthetic but structurally valid .flp fixtures.

The files exercise the parts of the container the merger actually touches:
pattern/arrangement markers, note payloads, Playlist records (old 32-byte and
new 60-byte), muted clips, audio/automation clips, stretched clips, cropped
left edges, zero-length step-sequencer notes and long tails — plus opaque
blob events and a trailing suffix that must survive the rewrite untouched.

Used by tools/compare_impls.py to prove the Rust port is byte-identical to the
Python reference implementation.
"""

from __future__ import annotations

import argparse
import random
import struct
from pathlib import Path

EV_PLAY_TRUNCATED = 30
EV_NEW_PATTERN = 65
EV_NEW_ARRANGEMENT = 99
EV_CURRENT_ARRANGEMENT = 100
EV_PATTERN_LENGTH = 164
EV_PATTERN_NAME = 193
EV_FL_VERSION = 199
EV_PATTERN_CONTROLLERS = 223
EV_PATTERN_NOTES = 224
EV_PLAYLIST = 233

NOTE_SIZE = 24
PATTERN_BASE = 20481
MUTED_CLIP_FLAG = 0x2000


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def ev_byte(event_id: int, value: int) -> bytes:
    return bytes((event_id, value & 0xFF))


def ev_word(event_id: int, value: int) -> bytes:
    return bytes((event_id,)) + struct.pack("<H", value)


def ev_dword(event_id: int, value: int) -> bytes:
    return bytes((event_id,)) + struct.pack("<I", value)


def ev_blob(event_id: int, payload: bytes) -> bytes:
    return bytes((event_id,)) + varint(len(payload)) + payload


def make_note(rng: random.Random, position: int, length: int) -> bytes:
    """A 24-byte FL note record. Only bytes 0..4 and 8..12 are ever rewritten;
    every other byte must survive the merge unchanged."""
    record = bytearray(rng.randbytes(NOTE_SIZE))
    struct.pack_into("<I", record, 0, position)
    struct.pack_into("<I", record, 8, length)
    return bytes(record)


def make_playlist_record(
    rng: random.Random,
    size: int,
    position: int,
    length: int,
    item_index: int,
    start_offset: int,
    end_offset: int,
    muted: bool,
    group: int = 0,
) -> bytes:
    record = bytearray(rng.randbytes(size))
    struct.pack_into("<I", record, 0, position)
    struct.pack_into("<H", record, 4, PATTERN_BASE)
    struct.pack_into("<H", record, 6, item_index)
    struct.pack_into("<I", record, 8, length)
    struct.pack_into("<H", record, 14, group)
    flags = struct.unpack_from("<H", record, 18)[0]
    flags = (flags | MUTED_CLIP_FLAG) if muted else (flags & ~MUTED_CLIP_FLAG)
    struct.pack_into("<H", record, 18, flags)
    struct.pack_into("<ii", record, 24, start_offset, end_offset)
    return bytes(record)


def build_pattern_notes(rng: random.Random, pattern: int, count: int, ppq: int) -> list[bytes]:
    """A pattern's notes, seeded so every fixture keeps the same shape."""
    beat = ppq
    notes: list[bytes] = []
    for index in range(count):
        position = index * (beat // 4) + rng.randrange(0, max(1, beat // 8))
        style = index % 7
        if style == 0:
            length = 0  # step-sequencer note
        elif style == 1:
            length = beat * rng.randrange(8, 20)  # long tail, crosses clip edges
        elif style == 2:
            length = 1
        else:
            length = rng.randrange(1, beat * 2)
        notes.append(make_note(rng, position, length))
    # A note that starts well before any cropped left edge, with a long tail.
    notes.append(make_note(rng, 0, beat * 64))
    return notes


def build(
    path: Path,
    *,
    seed: int,
    ppq: int,
    version: str,
    playlist_size: int,
    patterns: int,
    notes_per_pattern: int,
    arrangements: int,
    clips_per_arrangement: int,
    play_truncated: bool,
    blob_bytes: int,
    suffix_bytes: int,
) -> None:
    rng = random.Random(seed)
    events = bytearray()

    events += ev_blob(EV_FL_VERSION, version.encode("ascii") + b"\0")
    events += ev_byte(7, 1)  # opaque 1-byte event
    events += ev_word(66, 3)  # opaque 2-byte event
    events += ev_dword(156, 0x1234)  # opaque 4-byte event
    events += ev_byte(EV_PLAY_TRUNCATED, 1 if play_truncated else 0)
    if blob_bytes:
        # Stands in for plugin/sample state: large, opaque, must be copied as-is.
        events += ev_blob(212, rng.randbytes(blob_bytes))

    pattern_lengths: dict[int, int] = {}
    for pattern in range(1, patterns + 1):
        events += ev_word(EV_NEW_PATTERN, pattern)
        events += ev_blob(EV_PATTERN_NAME, f"Pattern {pattern}".encode("utf-16le") + b"\0\0")
        events += ev_dword(EV_PATTERN_LENGTH, 0)
        events += ev_blob(EV_PATTERN_CONTROLLERS, rng.randbytes(12 * (pattern % 5)))
        if pattern == 2 and patterns >= 2:
            # A pattern with no notes at all.
            events += ev_blob(EV_PATTERN_NOTES, b"")
            pattern_lengths[pattern] = ppq * 4
            continue
        notes = build_pattern_notes(rng, pattern, notes_per_pattern, ppq)
        if pattern == 3 and len(notes) > 4:
            # Two note events for one pattern: both must be merged, in order.
            split = len(notes) // 2
            events += ev_blob(EV_PATTERN_NOTES, b"".join(notes[:split]))
            events += ev_blob(EV_PATTERN_NOTES, b"".join(notes[split:]))
        else:
            events += ev_blob(EV_PATTERN_NOTES, b"".join(notes))
        pattern_lengths[pattern] = ppq * 16

    for arrangement in range(arrangements):
        events += ev_word(EV_NEW_ARRANGEMENT, arrangement)
        records = bytearray()
        position = 0
        for index in range(clips_per_arrangement):
            pattern = 1 + (index % patterns)
            length = pattern_lengths.get(pattern, ppq * 4)
            kind = index % 11

            if kind == 5:
                # Audio Clip / Automation Clip: item_index <= pattern_base.
                records += make_playlist_record(
                    rng, playlist_size, position, length,
                    item_index=PATTERN_BASE - (index % 7), start_offset=-1, end_offset=-1,
                    muted=False,
                )
                position += length
                continue

            item_index = PATTERN_BASE + pattern
            muted = kind == 7
            if kind == 1:
                start_offset, end_offset = -1, -1           # whole pattern
            elif kind == 2:
                start_offset, end_offset = ppq, ppq + length  # cropped left edge
            elif kind == 3:
                start_offset, end_offset = 0, length * 2      # stretched (squeeze)
            elif kind == 4:
                start_offset, end_offset = 0, max(1, length // 2)  # stretched (expand)
            elif kind == 9:
                start_offset, end_offset = ppq * 2, ppq * 2 + length // 3  # cropped both
            else:
                start_offset, end_offset = 0, length          # plain

            records += make_playlist_record(
                rng, playlist_size, position, length,
                item_index=item_index, start_offset=start_offset, end_offset=end_offset,
                muted=muted, group=index % 3,
            )
            position += length + (ppq if kind == 6 else 0)

        events += ev_blob(EV_PLAYLIST, bytes(records))

    events += ev_word(EV_CURRENT_ARRANGEMENT, 0)
    events += ev_blob(215, rng.randbytes(64))  # trailing opaque event

    header = struct.pack("<HHH", 0, 16, ppq)
    out = bytearray()
    out += b"FLhd" + struct.pack("<I", len(header)) + header
    out += b"FLdt" + struct.pack("<I", len(events)) + bytes(events)
    if suffix_bytes:
        out += rng.randbytes(suffix_bytes)  # nonstandard trailing bytes

    path.write_bytes(bytes(out))


PROFILES: dict[str, dict] = {
    "tiny": dict(
        seed=1, ppq=96, version="20.8.4.2576", playlist_size=32, patterns=3,
        notes_per_pattern=8, arrangements=1, clips_per_arrangement=6,
        play_truncated=True, blob_bytes=0, suffix_bytes=0,
    ),
    "small": dict(
        seed=2, ppq=96, version="21.0.3.3517", playlist_size=60, patterns=8,
        notes_per_pattern=64, arrangements=2, clips_per_arrangement=40,
        play_truncated=True, blob_bytes=4096, suffix_bytes=17,
    ),
    "no-truncate": dict(
        seed=3, ppq=192, version="21.0.3.3517", playlist_size=60, patterns=6,
        notes_per_pattern=48, arrangements=2, clips_per_arrangement=30,
        play_truncated=False, blob_bytes=1024, suffix_bytes=5,
    ),
    "highppq": dict(
        seed=4, ppq=960, version="24.1.1.4234", playlist_size=60, patterns=5,
        notes_per_pattern=100, arrangements=3, clips_per_arrangement=25,
        play_truncated=True, blob_bytes=2048, suffix_bytes=0,
    ),
    "oldformat": dict(
        seed=5, ppq=24, version="12.5.1.165", playlist_size=32, patterns=4,
        notes_per_pattern=30, arrangements=1, clips_per_arrangement=20,
        play_truncated=True, blob_bytes=512, suffix_bytes=3,
    ),
    "medium": dict(
        seed=6, ppq=96, version="21.0.3.3517", playlist_size=60, patterns=40,
        notes_per_pattern=400, arrangements=3, clips_per_arrangement=600,
        play_truncated=True, blob_bytes=1 << 20, suffix_bytes=64,
    ),
    "large": dict(
        seed=7, ppq=96, version="21.0.3.3517", playlist_size=60, patterns=120,
        notes_per_pattern=1200, arrangements=4, clips_per_arrangement=2500,
        play_truncated=True, blob_bytes=8 << 20, suffix_bytes=128,
    ),
    "huge": dict(
        seed=8, ppq=96, version="21.0.3.3517", playlist_size=60, patterns=200,
        notes_per_pattern=2500, arrangements=4, clips_per_arrangement=6000,
        play_truncated=True, blob_bytes=32 << 20, suffix_bytes=0,
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--profile", action="append", choices=sorted(PROFILES), default=None)
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    for name in args.profile or sorted(PROFILES):
        path = args.outdir / f"{name}.flp"
        build(path, **PROFILES[name])
        print(f"{path}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
