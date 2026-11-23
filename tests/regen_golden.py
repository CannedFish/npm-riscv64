#!/usr/bin/env python3
"""Regenerate the golden rendered action (L0 snapshot fixtures).

The golden files are the review surface for rendering changes: if a template edit
or a renderer change alters the generated action, the diff shows up in review
instead of silently shipping. Regenerate deliberately, then read the diff:

    python3 tests/regen_golden.py && git diff -- tests/golden
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "tests/golden/bufferutil-4.2.0"
PKG = "bufferutil"
VER = "4.2.0"
STEPS_DIR = ".rendered/action"


def render_into(target: Path) -> None:
    """Render the reference recipe with paths that do not depend on the output dir."""
    if target.exists():
        shutil.rmtree(target)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/render.py"),
            "--pkg",
            PKG,
            "--ver",
            VER,
            "--out-dir",
            str(target),
            "--steps-dir",
            STEPS_DIR,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"render failed:\n{result.stdout}\n{result.stderr}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "action"
        render_into(staging)
        if GOLDEN.exists():
            shutil.rmtree(GOLDEN)
        GOLDEN.mkdir(parents=True)
        shutil.copy2(staging / "action.yml", GOLDEN / "action.yml")
        shutil.copytree(staging / "steps", GOLDEN / "steps")
    print(f"golden updated: {GOLDEN.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
