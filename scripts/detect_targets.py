#!/usr/bin/env python3
"""Detect which recipes need to be built for the current change.

Two modes, both driven by environment variables so the same script serves the
pull-request and the post-merge workflow:

* ``pull_request`` - diff the merge base against the head commit.
* ``push``         - diff ``before..head``; an all-zero ``before`` (new branch or
  force push) means "rebuild everything".

Output (``GITHUB_OUTPUT``)::

    targets=[{"pkg":"@ruyisdk/bufferutil","ver":"4.2.0","dir":"packages/...","slug":"..."}]
    has_targets=true
    self_check=true|false

Rules:

* ``packages/<pkg>/<ver>/**``      -> that (package, version)
* ``packages/<pkg>/common/**``     -> every version of that package
* ``<template>`` changed           -> every recipe
* ``scripts/**`` changed           -> no build, but self-check the renderer
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recipe_lib as rl  # noqa: E402

DEFAULT_ROOT = "packages"
DEFAULT_TEMPLATE = ".github/templates/action.template.yml"
ZERO_SHA = "0" * 40


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=cwd
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def changed_files(
    event: str,
    base_ref: str,
    head_sha: str,
    before_sha: str,
    cwd: Path | None = None,
) -> tuple[list[str], bool]:
    """Return (changed paths, rebuild_everything)."""
    if event == "push":
        if not before_sha or set(before_sha) == {"0"}:
            return [], True
        # A pushed commit range: plain two-dot diff of the actual change.
        out = git("diff", "--name-only", f"{before_sha}..{head_sha}", cwd=cwd)
    else:
        # Pull request: three-dot diff against the merge base, so unrelated
        # commits that landed on the base branch do not leak into the change set.
        out = git("diff", "--name-only", f"origin/{base_ref}...{head_sha}", cwd=cwd)
    return [line for line in out.splitlines() if line.strip()], False


def resolve_targets(
    root: Path, changed: list[str], template_path: str, rebuild_all: bool
) -> tuple[list[rl.Target], bool]:
    """Map changed paths onto build targets; return (targets, self_check)."""
    self_check = False

    if rebuild_all or template_path in changed:
        reason = "template change" if template_path in changed else "full rebuild"
        print(f"# {reason}: selecting every recipe", file=sys.stderr)
        return rl.enumerate_targets(root), self_check

    selected: dict[tuple[str, str], rl.Target] = {}
    root_prefix = root.name + "/"

    for path in changed:
        if path.startswith("scripts/"):
            self_check = True
            continue
        if not path.startswith(root_prefix):
            continue

        relative = path[len(root_prefix) :]
        parsed = rl.split_recipe_path(relative)
        if parsed is None:
            print(f"# ignoring {path}: not inside a package directory", file=sys.stderr)
            continue
        pkg, ver = parsed

        if ver == rl.COMMON_DIR:
            versions = rl.versions_of(root, pkg)
            if not versions:
                print(f"# {path}: common changed but {pkg} has no version yet", file=sys.stderr)
                continue
            for one in versions:
                selected[(pkg, one)] = rl.Target(pkg=pkg, ver=one, path=root.joinpath(*pkg.split("/")) / one)
            continue

        target = rl.Target(pkg=pkg, ver=ver, path=root.joinpath(*pkg.split("/")) / ver)
        if not target.path.is_dir():
            print(f"# {path}: version directory is gone, nothing to build", file=sys.stderr)
            continue
        selected[(pkg, ver)] = target

    return [selected[key] for key in sorted(selected)], self_check


def verify_recipes(targets: list[rl.Target]) -> list[str]:
    """Every version directory must carry a .stat; fail early otherwise."""
    problems = []
    for target in targets:
        stat = target.path / rl.STAT_FILE
        if not stat.is_file():
            problems.append(
                f"{target.ident}: missing {rl.STAT_FILE} in {target.path} "
                "(every version directory is a buildable recipe and needs metadata)"
            )
    return problems


def main(cwd: Path | None = None) -> int:
    cwd = Path(cwd or Path.cwd())
    root_option = os.environ.get("RECIPE_ROOT", DEFAULT_ROOT)
    root = Path(root_option)
    if not root.is_absolute():
        root = cwd / root
    template_path = os.environ.get("TEMPLATE_PATH", DEFAULT_TEMPLATE)
    event = os.environ.get("EVENT_NAME", "pull_request")
    base_ref = os.environ.get("BASE_REF", "main")
    head_sha = os.environ.get("HEAD_SHA") or git("rev-parse", "HEAD", cwd=cwd).strip()
    before_sha = os.environ.get("BEFORE_SHA", "")

    try:
        changed, rebuild_all = changed_files(event, base_ref, head_sha, before_sha, cwd=cwd)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    targets, self_check = resolve_targets(root, changed, template_path, rebuild_all)

    problems = rl.repository_problems(root) + verify_recipes(targets)
    if problems:
        print("error: the recipe tree is not valid, refusing to build:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    payload = []
    for t in targets:
        try:
            stat = rl.load_stat(t)
        except rl.RecipeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        glob = str((stat.get("artifacts") or {}).get("path") or "")
        payload.append(
            {
                "pkg": t.pkg,
                "ver": t.ver,
                "dir": t.path.as_posix(),
                "slug": f"{t.slug_pkg}/{t.slug_ver}",
                "artifact": t.artifact_name,
                "artifact_glob": glob,
                "artifact_dir": str(Path(glob).parent).replace("\\", "/") if glob else "",
            }
        )
    compact = json.dumps(payload, separators=(",", ":"))
    # publish is only allowed after the merge lands on the default branch (D5).
    publish = "true" if (event == "push" and payload) else "false"
    outputs = {
        "targets": compact,
        "has_targets": "true" if payload else "false",
        "self_check": "true" if self_check else "false",
        "publish": publish,
        "changed_count": str(len(changed)),
    }

    print(f"# event={event} changed={len(changed)} targets={len(payload)} self_check={self_check}")
    for entry in payload:
        print(f"  - {entry['pkg']}@{entry['ver']}  ({entry['dir']})")

    for key, value in outputs.items():
        print(f"{key}={value}")
        target_file = os.environ.get("GITHUB_OUTPUT")
        if target_file:
            with open(target_file, "a", encoding="utf-8") as handle:
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
