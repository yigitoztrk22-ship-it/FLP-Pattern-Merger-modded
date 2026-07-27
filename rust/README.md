# FLP Note Merger — Rust engine

A Rust port of `flp_note_merger.py`. Same file format, same timing rules, same
output: every fixture in `tools/compare_impls.py` produces a **byte-identical**
`.flp` in both turbo and `--sorted` modes.

## Build

```bash
cd rust
cargo build --release
# -> target/release/flp-note-merger
```

Only two dependencies: `memmap2` and `rayon`.

## Use

```bash
flp-note-merger song.flp song_merged_notes.flp
```

| Option | Meaning |
|---|---|
| `--skip-muted` | Skip muted Pattern Clips so the audible arrangement is preserved. |
| `--sorted` | Disable turbo mode and globally sort note records by position. |
| `--threads N` | Worker threads (default: all cores; `1` disables parallelism). |
| `--no-fsync` | Skip the flush-to-disk before the atomic rename. |
| `--repeat N` | Run the merge N times and report best/median timings. |
| `-v, --verbose` | Per-stage timing breakdown on stderr. |
| `--json` | Machine-readable summary with all stage timings. |
| `--run-records N` | Accepted for CLI compatibility; unused (there is no external sort). |

The flags mirror the Python CLI, so existing scripts keep working.

## Performance

Measured on a 4-core Linux container, best of 7 runs, synthetic projects from
`tools/make_test_flp.py`. **merge** is scan + clip index + note expansion +
rewrite plan — everything except pushing the output file to disk.

| project | source | merged notes | output | merge | total (fsync) | total (page cache) | Python 1.2 | speedup |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `small` | 0.02 MB | 3,602 | 0.1 MB | **0.04 ms** | 1.5 ms | 0.1 ms | 126 ms | 84x |
| `medium` | 1.5 MB | 102,972 | 3.5 MB | **0.61 ms** | 7.7 ms | 1.3 ms | 254 ms | 33x |
| `large` | 12.4 MB | 581,264 | 22.3 MB | **2.34 ms** | 36.9 ms | 8.3 ms | 895 ms | 24x |
| `huge` | 47.0 MB | 1,400,272 | 67.2 MB | **10.17 ms** | 150.1 ms | 36.1 ms | 2123 ms | 14x |

`--sorted` mode is where the gap is widest: on `huge` the Python external merge
sort takes ~40 s against 313 ms here, because a stable radix sort over the
32-bit position field replaces the temp-file merge entirely.

Two honest caveats:

* **Merge is under 20 ms in every case above; end-to-end is not, past ~20 MB of
  output.** Writing 67 MB and fsyncing it is storage-bound work no amount of
  CPU optimisation removes. `--no-fsync` and `--verbose` let you see the split.
* Timings depend on storage, CPU, project structure and note reuse. Numbers
  from your own projects are the only ones that matter.

## How it goes fast

| | Python 1.2 | Rust |
|---|---|---|
| Source access | buffered reads + `seek` per note event | one read-only `mmap`, walked as a slice |
| Clip expansion | NumPy batch per clip, re-transforming reused patterns | one transform per distinct clip *shape*, then rebase |
| Merged payload | temp file on disk | one exactly-sized allocation, filled in parallel |
| Output assembly | copy every event through a buffered writer | list of borrowed spans, streamed once |
| `--sorted` | external merge sort with temp files | in-memory stable radix sort |

The two changes that matter most:

**Shape caching.** Clips sharing `(pattern, source range, clip length)` produce
identical records apart from one additive position offset. Each distinct shape
is transformed once; every clip using it re-emits the block with its own base
added. The README's pathological case — one small pattern reused thousands of
times — collapses from thousands of transforms to one transform plus
memcpy-speed rebasing.

**No intermediate image.** The rewrite emits a list of pieces (spans borrowed
from the mapped source, the merged payload, a few generated event bytes) rather
than building a second full-size buffer. On the `huge` fixture this alone took
the rewrite stage from 39.9 ms to 0.02 ms, because an 80 MB allocate-and-copy
turned into pointer bookkeeping.

Exactness came along for free: the note position mapping uses a 128-bit
intermediate product, so the arbitrary-precision fallback the NumPy path needs
for pathological notes is unnecessary — the fast path is always the correct one.

## Testing

```bash
cd rust && cargo test --release          # 24 behavioural tests
python3 tools/compare_impls.py           # byte-identical vs. the Python build
python3 tools/benchmark.py               # the table above
```

`cargo test` builds minimal projects in memory and asserts the timing rules
directly: cropped left edges under both `Play truncated notes in clips`
settings, tails running past a clip's right edge, stretch ratios in both
directions, zero-length step notes, per-arrangement section layout, muted and
audio/automation clips, old 32-byte and new 60-byte Playlist records, and 24 to
960 PPQ timebases.

## Library use

```rust
use flp_note_merger::{merge_in_memory, MergeOptions};

let merged: Vec<u8> = merge_in_memory(&project_bytes, &MergeOptions::default())?;
```

`merge_flp(source, output, &options, status)` is the file-based entry point and
returns `MergeStats` with the per-stage timings.

## Not ported

The Tkinter GUI in `flp_note_merger.py` has no Rust equivalent — this is the
engine plus a CLI. The Python GUI still works and still uses the Python engine.
