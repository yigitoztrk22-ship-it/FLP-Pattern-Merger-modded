#!/usr/bin/env python3
"""Benchmark the Rust engine against the Python reference.

Reports the merge time (scan + clip index + note expansion + rewrite plan) and
the end-to-end wall time, so I/O-bound output writing is visible separately
from the transformation itself.

    python3 tools/benchmark.py [--profiles medium,large,huge] [--repeat 7]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_MERGER = ROOT / "flp_note_merger.py"
RUST_MERGER = ROOT / "rust" / "target" / "release" / "flp-note-merger"

sys.path.insert(0, str(ROOT / "tools"))
from make_test_flp import PROFILES, build  # noqa: E402


def time_python(source: Path, out: Path, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        done = subprocess.run(
            [sys.executable, str(PYTHON_MERGER), str(source), str(out)],
            capture_output=True, text=True,
        )
        elapsed = time.perf_counter() - started
        if done.returncode != 0:
            raise SystemExit(f"python merger failed:\n{done.stdout}{done.stderr}")
        best = min(best, elapsed)
    return best * 1000.0


def time_rust(source: Path, out: Path, repeat: int, fsync: bool) -> dict:
    command = [str(RUST_MERGER), str(source), str(out), "--json", "--repeat", str(repeat)]
    if not fsync:
        command.append("--no-fsync")
    started = time.perf_counter()
    done = subprocess.run(command, capture_output=True, text=True)
    wall = (time.perf_counter() - started) * 1000.0 / max(1, repeat)
    if done.returncode != 0:
        raise SystemExit(f"rust merger failed:\n{done.stdout}{done.stderr}")
    stats = json.loads(done.stdout)
    stats["process_wall_ms"] = wall
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", default="small,medium,large,huge")
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--no-python", action="store_true", help="skip the Python reference")
    args = parser.parse_args()

    if not RUST_MERGER.is_file():
        print("Build the Rust binary first: cd rust && cargo build --release")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="flp_bench_"))
    rows = []
    for name in args.profiles.split(","):
        source = workdir / f"{name}.flp"
        build(source, **PROFILES[name])
        rust = time_rust(source, workdir / f"{name}.rs.flp", args.repeat, fsync=True)
        rust_nosync = time_rust(source, workdir / f"{name}.rs2.flp", args.repeat, fsync=False)
        python_ms = (
            None if args.no_python
            else time_python(source, workdir / f"{name}.py.flp", min(3, args.repeat))
        )
        rows.append((name, source.stat().st_size, rust, rust_nosync, python_ms))

    micro = lambda value: value / 1000.0  # noqa: E731
    print()
    print("| project | source | merged notes | output | scan+index+expand+plan | "
          "total (fsync) | total (page cache) | python 1.2 | speedup |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, size, rust, nosync, python_ms in rows:
        t = rust["timings_us"]
        speedup = f"{python_ms / (rust['timings_us']['total'] / 1000.0):.0f}x" if python_ms else "-"
        print(
            f"| `{name}` | {size / 1e6:.1f} MB | {rust['merged_notes']:,} | "
            f"{rust['output_size'] / 1e6:.1f} MB | **{micro(t['merge']):.2f} ms** | "
            f"{micro(t['total']):.1f} ms | {micro(nosync['timings_us']['total']):.1f} ms | "
            f"{python_ms:.0f} ms | {speedup} |"
            if python_ms
            else f"| `{name}` | {size / 1e6:.1f} MB | {rust['merged_notes']:,} | "
                 f"{rust['output_size'] / 1e6:.1f} MB | **{micro(t['merge']):.2f} ms** | "
                 f"{micro(t['total']):.1f} ms | {micro(nosync['timings_us']['total']):.1f} ms | - | - |"
        )
    print()

    for path in workdir.iterdir():
        path.unlink()
    workdir.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
