#!/usr/bin/env python3
"""Differential test: the Rust port must be byte-identical to the Python one.

Generates every fixture profile, runs both implementations in both turbo and
--sorted modes, and compares the resulting .flp files byte for byte.

    python3 tools/compare_impls.py [--profiles tiny,small,...] [--keep]
"""

from __future__ import annotations

import argparse
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


def run(command: list[str]) -> tuple[int, str, float]:
    started = time.perf_counter()
    done = subprocess.run(command, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    return done.returncode, (done.stdout + done.stderr), elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", default=None, help="comma-separated profile names")
    parser.add_argument("--keep", action="store_true", help="keep the working directory")
    args = parser.parse_args()

    if not RUST_MERGER.is_file():
        print(f"Build the Rust binary first: cargo build --release ({RUST_MERGER})")
        return 2

    names = args.profiles.split(",") if args.profiles else sorted(PROFILES)
    workdir = Path(tempfile.mkdtemp(prefix="flp_diff_"))
    failures = 0

    print(f"{'fixture':<14} {'mode':<8} {'notes':>10} {'python':>10} {'rust':>10} {'speedup':>9}  result")
    print("-" * 78)

    for name in names:
        source = workdir / f"{name}.flp"
        build(source, **PROFILES[name])

        for mode, extra in (("turbo", []), ("sorted", ["--sorted"])):
            py_out = workdir / f"{name}.{mode}.py.flp"
            rs_out = workdir / f"{name}.{mode}.rs.flp"

            py_code, py_log, py_time = run(
                [sys.executable, str(PYTHON_MERGER), str(source), str(py_out), *extra]
            )
            rs_code, rs_log, rs_time = run(
                [str(RUST_MERGER), str(source), str(rs_out), "--quiet", *extra]
            )

            if py_code != 0 or rs_code != 0:
                print(f"{name:<14} {mode:<8} {'-':>10} {'-':>10} {'-':>10} {'-':>9}  "
                      f"FAIL (exit {py_code}/{rs_code})")
                if py_code != 0:
                    print(f"    python: {py_log.strip().splitlines()[-1] if py_log.strip() else ''}")
                if rs_code != 0:
                    print(f"    rust:   {rs_log.strip().splitlines()[-1] if rs_log.strip() else ''}")
                failures += 1
                continue

            py_bytes = py_out.read_bytes()
            rs_bytes = rs_out.read_bytes()
            notes = next(
                (line.split()[1] for line in rs_log.splitlines() if line.startswith("Success:")),
                "?",
            )
            if py_bytes == rs_bytes:
                verdict = "identical"
            else:
                failures += 1
                first = next(
                    (i for i, (a, b) in enumerate(zip(py_bytes, rs_bytes)) if a != b),
                    min(len(py_bytes), len(rs_bytes)),
                )
                verdict = (f"DIFFER at byte {first} "
                           f"(py {len(py_bytes)} B, rs {len(rs_bytes)} B)")

            speedup = f"{py_time / rs_time:6.1f}x" if rs_time > 0 else "-"
            print(f"{name:<14} {mode:<8} {notes:>10} {py_time * 1000:9.0f}ms "
                  f"{rs_time * 1000:9.1f}ms {speedup:>9}  {verdict}")

    print("-" * 78)
    if failures:
        print(f"{failures} comparison(s) failed. Working files: {workdir}")
        return 1
    print("All outputs byte-identical to the Python reference.")
    if args.keep:
        print(f"Working files: {workdir}")
    else:
        for path in workdir.iterdir():
            path.unlink()
        workdir.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
