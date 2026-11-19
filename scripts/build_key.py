#!/usr/bin/env python3
"""Content key for the build cache (see .dsh/plan.md D16).

A cache hit has to be trustworthy: the workflow restores a previously built
workspace tree and skips fetch, patch and build entirely. That is only safe if the
key covers *everything* that can change the output:

* the version directory (``.stat``, ``commands/``, ``patches/``),
* the shared ``common`` layer next to it,
* the action template (it decides the image, the flags and the step order),
* ``scripts/`` (the renderer generates the fetch/patch snippets),
* ``docker/`` (the toolchain itself),
* the resolved container image id,
* anything passed with ``--extra`` (render-time rewrites).

A different recipe, a different commit message or a different PR cannot influence
the built artifact, so they are deliberately absent from the key: two runs with the
same key would have produced byte-identical trees.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recipe_lib as rl  # noqa: E402

#: Recipes live here, relative to the repository root.
PACKAGES_DIR = "packages"

#: Inputs outside the recipe directory that still change the built artifact.
SHARED_INPUTS = (
    ".github/templates",
    "scripts",
    "docker",
)

#: Checked-in artefacts that never take part in a build.
_SKIP_SUFFIXES = (".pyc", ".pyo")


def _iter_files(path: Path) -> Iterator[Path]:
    if path.is_file():
        if not path.name.endswith(_SKIP_SUFFIXES):
            yield path
        return
    if not path.is_dir():
        return
    for entry in sorted(path.rglob("*")):
        if entry.is_file() and not entry.name.endswith(_SKIP_SUFFIXES):
            yield entry


def collect_inputs(root: Path, pkg: str, ver: str) -> list[Path]:
    """Return the hashed files, sorted by repository-relative path."""
    root = Path(root).resolve()
    target = rl.find_target(root / PACKAGES_DIR, pkg, ver)

    candidates: list[Path] = list(_iter_files(target.path))
    common = target.path.parent / rl.COMMON_DIR
    candidates.extend(_iter_files(common))
    for relative in SHARED_INPUTS:
        candidates.extend(_iter_files(root / relative))

    # Deduplicate: a version directory can also be reached through `scripts/` style
    # globs in future inputs, and sorting keeps the digest stable.
    unique = {
        path.relative_to(root).as_posix(): path
        for path in candidates
        if path.is_file() and path.is_relative_to(root)
    }
    return [unique[name] for name in sorted(unique)]


def content_digest(files: Iterable[Path], root: Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def build_key(
    root: Path,
    pkg: str,
    ver: str,
    image_id: str = "",
    extra: str = "",
) -> str:
    """Return a short key that identifies one build configuration."""
    root = Path(root).resolve()
    files = collect_inputs(root, pkg, ver)
    digest = hashlib.sha256()
    digest.update(content_digest(files, root).encode("utf-8"))
    digest.update(b"\0image\0")
    digest.update(str(image_id or "").encode("utf-8"))
    digest.update(b"\0extra\0")
    digest.update(str(extra or "").encode("utf-8"))
    return digest.hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="print the build cache key for one recipe",
    )
    parser.add_argument("--pkg", required=True, help="package name, e.g. @scope/name")
    parser.add_argument("--ver", required=True, help="version directory name")
    parser.add_argument("--root", default=".", help="recipe root (default: .)")
    parser.add_argument(
        "--image-id",
        default="",
        help="resolved container image id (docker image inspect --format '{{.Id}}')",
    )
    parser.add_argument(
        "--extra",
        default="",
        help="additional inputs that change the build (for example render flags)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="also print the hashed inputs, one relative path per line",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        key = build_key(root, args.pkg, args.ver, args.image_id, args.extra)
    except rl.RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.list:
        for path in collect_inputs(root, args.pkg, args.ver):
            print(path.relative_to(root).as_posix())
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
