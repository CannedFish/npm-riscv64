#!/usr/bin/env python3
"""Build cache key (see .dsh/plan.md D16).

The workflow restores a previously built workspace tree instead of building again, so
the key has to answer one question precisely: **would this recipe produce the same
artifact?** That is narrower than "did any file change":

* a template edit that only adds an input or an ``if:`` guard changes the action but not
  a single executed command - the artifact is identical;
* a ``.stat`` edit that only moves a publish target or pins another registry changes
  nothing about the build;
* a change to the ``test`` or ``publish`` step likewise leaves the artifact alone.

So the key hashes what actually runs, after rendering:

1. the rendered ``run:`` blocks of the fetch / patch / build steps (image, mounts,
   working directory, environment, flags),
2. the rendered ``steps/{fetch,patch,build}.sh`` bodies (fetch and patch are generated
   from the template and the ``.stat``; build is the recipe snippet),
3. the patch files, which the patch step reads from the repository at run time,
4. the resolved container image id,

plus anything passed with ``--extra``. Rendering makes the key self-contained: a
renderer change shows up as changed command text, so ``scripts/`` needs no separate
input.

A different recipe, a different commit message or a different PR cannot influence the
built artifact, so they are deliberately absent: two runs with the same key would have
produced equivalent trees.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recipe_lib as rl  # noqa: E402
import render  # noqa: E402

#: Recipes live here, relative to the repository root.
PACKAGES_DIR = "packages"

#: The default action template, relative to the repository root.
TEMPLATE = ".github/templates/action.template.yml"

#: Where the workflow writes the rendered action. Fixed on purpose: the key must not
#: change because a local run wrote somewhere else.
STEPS_DIR = ".rendered/action"

#: Steps whose execution produces the artifact.
BUILD_STEPS = ("fetch upstream source", "apply patches", "build")

#: Scripts those steps run.
BUILD_SCRIPTS = ("fetch", "patch", "build")


def build_inputs(
    root: Path, pkg: str, ver: str, template: Path | None = None
) -> list[str]:
    """Return the build-relevant text for one recipe, in a stable order."""
    import yaml

    root = Path(root).resolve()
    template = Path(template).resolve() if template else root / TEMPLATE
    packages = root / PACKAGES_DIR

    rendered, scripts = render.render_target(
        packages, pkg, ver, template, steps_dir=STEPS_DIR
    )
    action = yaml.safe_load(rendered)

    parts: list[str] = []
    for step in action["runs"]["steps"]:
        if step.get("name") in BUILD_STEPS:
            parts.append(str(step.get("run") or ""))
    for name in BUILD_SCRIPTS:
        if name in scripts:
            parts.append(scripts[name])

    # The patch step addresses files by path, so their content has to be hashed too:
    # the common layer applies first, then the version layer.
    target = rl.find_target(packages, pkg, ver)
    for directory in (target.path.parent / rl.COMMON_DIR, target.path):
        patches = directory / "patches"
        if not patches.is_dir():
            continue
        for entry in sorted(patches.iterdir()):
            if entry.is_file():
                parts.append(f"patches/{entry.name}\0{entry.read_text(encoding='utf-8')}")
    return parts


def build_key(
    root: Path,
    pkg: str,
    ver: str,
    image_id: str = "",
    template: Path | None = None,
    extra: str = "",
) -> str:
    """Return a short key that identifies one build configuration."""
    digest = hashlib.sha256()
    for part in build_inputs(root, pkg, ver, template):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
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
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument(
        "--template",
        default=None,
        help=f"action template (default: <root>/{TEMPLATE})",
    )
    parser.add_argument(
        "--image-id",
        default="",
        help="resolved image id (docker image inspect --format '{{.Id}}')",
    )
    parser.add_argument(
        "--extra",
        default="",
        help="further inputs that change the build (for example render flags)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="also print the hashed build inputs",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    template = Path(args.template) if args.template else None
    try:
        key = build_key(root, args.pkg, args.ver, args.image_id, template, args.extra)
    except (rl.RecipeError, render.RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.list:
        for part in build_inputs(root, args.pkg, args.ver, template):
            print(f"--- {len(part)} chars ---")
            print(part)
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
