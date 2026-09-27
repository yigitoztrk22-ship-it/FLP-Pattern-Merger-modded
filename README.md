# FLP Note Merger

Creates a new FL Studio `.flp` where **all arranged Pattern Clip notes are flattened into one pattern**. It does not render or merge audio.

The application is now split into a C# Windows GUI and a Rust processing engine:

- **`rust/` — the native engine.** It owns FLP parsing, note expansion, MIDI export, and output writing. See [`rust/README.md`](rust/README.md).
- **`dotnet/` — the Windows GUI.** A WinForms application that launches the Rust engine and displays its JSON results. No Python installation is needed to run the application.

The Python implementation remains in the repository as a legacy reference and fixture-comparison tool; it is no longer part of the Windows application path.

```bash
cd rust && cargo build --release
./target/release/flp-note-merger song.flp song_merged_notes.flp
```

The Rust CLI accepts the same flags as the Python CLI (`--skip-muted`, `--sorted`, `--run-records`), so existing scripts keep working. `tools/compare_impls.py` proves the two agree byte for byte across every fixture profile in both turbo and `--sorted` modes.

The program is designed for very large projects. The Rust engine uses memory mapping, parallel expansion, shape caching, direct binary writes, and an in-memory stable radix sort for compatibility mode.

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


## Version 1.2 Turbo expansion

Turbo mode is enabled by default:


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

## Run on Windows

Requires the published application folder from the build step below. Double-click `run_windows.bat`, choose an input `.flp` and a different output path, then click **Merge notes**. The GUI is C# WinForms and the FLP work is performed by the bundled Rust executable.

## Build the standalone Windows application

Double-click:

```text
build_windows.bat
```

The script builds the Rust engine and publishes the self-contained C# app folder:

```text
release\FLP_Note_Merger\FLP_Note_Merger.exe
```

Keep that complete folder together when copying it. The app does not require Python on the destination PC. The build publishes a self-contained C# WinForms executable and copies the Rust engine beside it.

The Windows Rust build uses the GNU target and MinGW-w64 `gcc`; MSVC `link.exe` is not required.

## Command-line mode

```bat
release\flp-note-merger.exe "C:\Music\song.flp" "C:\Music\song_merged_notes.flp"
```

Skip muted Pattern Clips:

```bat
release\flp-note-merger.exe input.flp output.flp --skip-muted
```

Disable Turbo and force the old globally sorted compatibility path:

```bat
release\flp-note-merger.exe input.flp output.flp --sorted
```

Adjust the number of notes sorted in each compatibility-mode memory run (default `150000`):

```bat
release\flp-note-merger.exe input.flp output.flp --run-records 250000
```

Larger values can be faster but use more RAM.

Export one MIDI file per FL Studio Arrangement:

```bat
release\flp-note-merger.exe input.flp song.mid --midi
```

This creates files such as `song_arrangement_0.mid` and `song_arrangement_1.mid`. Each file starts at MIDI tick 0. The GUI provides the same **Split MIDI by Arrangement** option.

Split MIDI into numbered files with a maximum number of notes per file:

```bat
release\flp-note-merger.exe input.flp song.mid --midi
```

This creates `song_part_001.mid`, `song_part_002.mid`, and so on.

## Very large projects

- Use the provided self-contained 64-bit C# application folder.
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
