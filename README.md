# FLP Note Merger

Creates a new FL Studio `.flp` where **all arranged Pattern Clip notes are flattened into one pattern**. It does not render or merge audio.

Two implementations ship here, and they produce byte-identical output:

- **`rust/` — the fast engine (recommended).** A Rust CLI. Merges a 1.4 million note project in **10 ms**, 14–84x faster end to end than the Python build. See [`rust/README.md`](rust/README.md).
- **`flp_note_merger.py` — the reference implementation.** Python + NumPy, and the only one with the Windows GUI.

```bash
cd rust && cargo build --release
./target/release/flp-note-merger song.flp song_merged_notes.flp
```

The Rust CLI accepts the same flags as the Python CLI (`--skip-muted`, `--sorted`, `--run-records`), so existing scripts keep working. `tools/compare_impls.py` proves the two agree byte for byte across every fixture profile in both turbo and `--sorted` modes.

The program is designed for very large projects. Version 1.2 of the Python build uses NumPy's compiled native vector loops to transform large note batches and writes them directly, avoiding one Python operation per note and avoiding the old external-sort bottleneck. A bounded-memory scalar/sorted fallback remains available.

## Rust engine performance

Best of 7 runs on a 4-core Linux container, synthetic projects from `tools/make_test_flp.py`. **merge** is the transformation itself — scan, clip index, note expansion and rewrite plan — separated from pushing the output file to disk.

| project | source | merged notes | output | merge | total (fsync) | total (page cache) | Python 1.2 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `small` | 0.02 MB | 3,602 | 0.1 MB | **0.04 ms** | 1.5 ms | 0.1 ms | 126 ms |
| `medium` | 1.5 MB | 102,972 | 3.5 MB | **0.61 ms** | 7.7 ms | 1.3 ms | 254 ms |
| `large` | 12.4 MB | 581,264 | 22.3 MB | **2.34 ms** | 36.9 ms | 8.3 ms | 895 ms |
| `huge` | 47.0 MB | 1,400,272 | 67.2 MB | **10.17 ms** | 150.1 ms | 36.1 ms | 2123 ms |

The merge stays under 20 ms even at 1.4 million merged notes. End-to-end time past roughly 20 MB of output is dominated by writing and flushing the file, which is storage-bound rather than CPU-bound — run with `--verbose` to see the split, or `--no-fsync` to skip the flush.

Reproduce with `python3 tools/benchmark.py`.

## What it does

- Reads every Playlist track in every Arrangement.
- Uses Pattern Clips only; ignores Audio Clips and Automation Clips.
- Preserves the FL note record: channel/rack assignment, pitch, velocity, pan, release, fine pitch, Mod X/Y, color/MIDI channel, group, slide flags, and other raw note bits.
- Applies each Pattern Clip's Playlist position, visible range, start offset, and stretch ratio.
- Preserves the complete duration of every note whose note-on occurs inside a Pattern Clip; note tails are not incorrectly cut at the clip's right edge.
- Restores notes crossing a cropped left edge according to the project's **Play truncated notes in clips** setting.
- Handles FLP header timebases from **24 through 65,535 PPQ** without hardcoded 96-PPQ timing.
- Expands reused Pattern Clips to their real positions on the song timeline.
- Keeps every FL Arrangement as a separate offset section inside one target pattern.
- Replaces each Arrangement's Playlist with one Pattern Clip pointing at that section.
- Clears old pattern-note payloads and pattern event-controller payloads.
- Uses native vector batches and direct binary writes in default Turbo mode.
- Never modifies the source project.

## Version 1.2 Turbo expansion

Turbo mode is enabled by default:

- NumPy transforms up to one million packed FL note records per native batch.
- Position filtering, PPQ/stretch scaling, and duration calculations run in compiled native loops rather than Python note-by-note loops.
- Complete 24-byte note rows are copied in bulk; only position and length fields are patched.
- The global external sort is bypassed. FL Studio reads the packed notes by their stored positions and does not require the event rows to be globally ordered.
- Disk traffic and temporary-space use are greatly reduced.
- If NumPy is unavailable, the program automatically uses a scalar streaming fallback.
- Disable **Turbo expansion** or pass `--sorted` only if a particular project needs globally ordered note records.

This substantially accelerates expansion, but no universal “faster than C++” claim is possible: speed depends on storage, project structure, note reuse, CPU, and FLP size. The Turbo calculations themselves execute in NumPy's optimized native code.

## Version 1.1 timing fix

Version 1.1 fixes the long-note bug from version 1.0. Notes beginning inside a Pattern Clip now keep their complete original tails, including tails extending beyond the clip's visible right edge. Cropped-left notes still follow FL Studio's **Play truncated notes in clips** behavior. PPQ calculations use the unsigned 16-bit FLP timebase directly, supporting values from 24 to 65,535.

## Important limitations

FL Studio's FLP format is proprietary. This is an unofficial binary editor, so **always keep the original and verify the generated copy in FL Studio**.

- The result is notes-only at the Playlist level. Audio/automation clips are removed from the output Playlists.
- Audio channels, instruments, samples, mixer state, and plugins remain in the project because merged notes still need their original Channel Rack targets. This tool does not render audio.
- Pattern event automation is cleared because it cannot be placed correctly after all patterns become one notes-only pattern.
- Playlist-track organization, clip colors, grouping, and per-clip mute states cannot be retained in one merged clip.
- If **Include muted Pattern Clips** is enabled, muted clips' notes are retained but become unmuted in the merged result. Disable it to preserve the audible arrangement instead.
- Track-level mute/solo state is not baked into notes.
- A project with millions of actual note objects can still take time and memory for FL Studio itself to open. The tool removes redundant old note payloads and sorts the result, but it cannot change FL Studio's in-memory note representation.
- Reusing a small pattern thousands of times can create a much larger merged FLP because every placed copy must become real notes.
- The FLP `FLdt` chunk has a 4 GiB limit. The program stops safely if a generated project would exceed it.

## Run with Python on Windows

Requires 64-bit Python 3.10 or newer. Tkinter is included with the normal installer from [python.org](https://www.python.org/).

1. Extract this folder.
2. Double-click `run_windows.bat`.
3. Choose the input `.flp` and a different output path.
4. Click **Merge notes**.
5. Open the new copy in FL Studio and verify it before deleting or changing anything.

`run_windows.bat` checks for NumPy and installs it for the current user when needed. If installation is unavailable, the program still runs with its slower standard-library fallback.

You can also install manually and run:

```bat
py -3 -m pip install "numpy>=1.24"
py -3 flp_note_merger.py
```

## Build a standalone Windows EXE

Double-click:

```text
build_windows.bat
```

The script installs/updates NumPy and PyInstaller for your user account and creates the fast-starting app folder:

```text
release\FLP_Note_Merger\FLP_Note_Merger.exe
```

Keep that complete folder together when copying it. The app does not require Python on the destination PC. The build deliberately uses PyInstaller `--onedir` rather than `--onefile`: with NumPy, one-file executables must unpack a large native runtime to `%TEMP%` on every launch and start more slowly.

## Command-line mode

```bat
py -3 flp_note_merger.py "C:\Music\song.flp" "C:\Music\song_merged_notes.flp"
```

Skip muted Pattern Clips:

```bat
py -3 flp_note_merger.py input.flp output.flp --skip-muted
```

Disable Turbo and force the old globally sorted compatibility path:

```bat
py -3 flp_note_merger.py input.flp output.flp --sorted
```

Adjust the number of notes sorted in each compatibility-mode memory run (default `150000`):

```bat
py -3 flp_note_merger.py input.flp output.flp --run-records 250000
```

Larger values can be faster but use more RAM.

## Very large projects

- Use a 64-bit Python build or the provided 64-bit EXE.
- Put the Windows temporary folder on a drive with ample free space if necessary. The tool uses `%TEMP%` for external-sort files.
- One million FL note records occupy about 24 MB before FLP/event overhead. Turbo mode generally needs only the direct merged payload; `--sorted` compatibility mode can temporarily need two to three times that space.
- Native batches default to at most one million records (about 24 MB of raw input plus vector work arrays), keeping memory bounded even for much larger projects.
- The GUI has a Cancel button. A partial output is deleted when cancellation completes.
- Antivirus scanning of large temporary binary files can slow the operation.

## Safety behavior

- Input and output must be different paths.
- The source is opened read-only.
- Output is first written as `name.flp.partial` and atomically renamed only after a successful complete write.
- Temporary sort files are deleted after success, error, or cancellation. The Rust engine needs no temporary files at all.

## Development and verification

`tools/` holds the scripts that keep the two implementations honest:

```bash
python3 tools/make_test_flp.py out/           # synthetic .flp fixtures
python3 tools/compare_impls.py                # Rust vs Python, byte for byte
python3 tools/benchmark.py                    # the performance table above
cd rust && cargo test --release               # 24 behavioural tests
```

`make_test_flp.py` generates projects that exercise the awkward parts of the format: cropped left edges, tails past a clip's right edge, stretched clips in both directions, zero-length step notes, muted clips, audio/automation clips, patterns split across several note events, old 32-byte and new 60-byte Playlist records, 24 to 960 PPQ timebases, opaque plugin blobs, and trailing bytes after `FLdt`.

`compare_impls.py` runs both mergers over every fixture in both turbo and `--sorted` modes and compares the resulting projects byte for byte. Any behavioural difference between the two implementations fails the run.
