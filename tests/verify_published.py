#!/usr/bin/env python3
"""Delivered-artifact check: compare what a registry serves with upstream.

The recipes in this repository promise "upstream's package plus the riscv64
increment, nothing else". That is a byte comparison, not an eyeball, so this script
compares the published tarball with the upstream one file by file. It needs a
registry that actually holds the version and a network to reach npmjs.org, which
is why it is not part of the unit suite.

Two package shapes exist (.dsh/plan.md D33):

* ``prebuild`` - upstream's package plus one directory, ``prebuilds/linux-riscv64``;
* ``wrapper`` - upstream's package where the only change is one more entry in
  ``optionalDependencies`` (rolldown-style: the binary ships as its own package).

Usage::

    python3 tests/verify_published.py bufferutil 4.2.0
    python3 tests/verify_published.py rolldown 1.3.3 --shape wrapper \\
        --bindings @rolldown/binding-linux-riscv64-gnu
    python3 tests/verify_published.py ... --registry http://127.0.0.1:4873
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import urllib.request

DEFAULT_REGISTRY = "http://127.0.0.1:4890"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def members(raw: bytes) -> dict[str, bytes]:
    """Files of an npm tarball, keyed without the leading ``package/``."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for info in tar.getmembers():
            if info.isfile():
                name = info.name.split("package/", 1)[-1]
                out[name] = tar.extractfile(info).read()
    return out


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def elf_machine(data: bytes) -> int | None:
    if data[:4] != b"\x7fELF":
        return None
    return int.from_bytes(data[18:20], "little")


def compare(ours: dict[str, bytes], base: dict[str, bytes], shape: str,
            bindings: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (added, removed, problems) for the shape the recipe publishes."""
    added = sorted(set(ours) - set(base))
    removed = sorted(set(base) - set(ours))
    changed = sorted(p for p in (set(base) & set(ours)) if digest(base[p]) != digest(ours[p]))
    problems: list[str] = []
    if shape == "prebuild":
        if removed:
            problems.append(f"upstream files missing from the published package: {removed}")
        if changed not in ([], ["package.json"]):
            problems.append(f"files changed beyond the manifest: {changed}")
    else:
        if added:
            problems.append(f"the wrapper gained files: {added}")
        if removed:
            problems.append(f"upstream files missing from the published package: {removed}")
        if changed not in ([], ["package.json"]):
            problems.append(f"files changed beyond the manifest: {changed}")
        upstream_manifest = json.loads(base.get("package.json", b"{}"))
        manifest = json.loads(ours.get("package.json", b"{}"))
        before = set((upstream_manifest.get("optionalDependencies") or {}))
        after = set((manifest.get("optionalDependencies") or {}))
        expected_after = before | set(bindings)
        if after != expected_after:
            problems.append(
                f"optionalDependencies are {sorted(after)}, expected upstream's "
                f"{len(before)} plus {bindings}"
            )
        for field in ("main", "files", "engines", "scripts", "dependencies", "bin", "exports"):
            if upstream_manifest.get(field) != manifest.get(field):
                problems.append(f"upstream field '{field}' did not survive")
    return added, removed, problems


def check_identity(ours: dict[str, bytes], name: str, version: str) -> list[str]:
    manifest = json.loads(ours.get("package.json", b"{}"))
    if manifest.get("name") != name:
        return [f"the published manifest is named {manifest.get('name')}, expected {name}"]
    if str(manifest.get("version")) != version:
        return [f"the published manifest has version {manifest.get('version')}, expected {version}"]
    return []


def check_prebuild(ours: dict[str, bytes], name: str) -> list[str]:
    path = f"prebuilds/linux-riscv64/{name}.node"
    node = ours.get(path, b"")
    machine = elf_machine(node)
    if machine is None:
        return [f"{path} is not an ELF"]
    if machine != 243:  # EM_RISCV
        return [f"{path} is an ELF for machine {machine}, not RISC-V (243)"]
    print(f"  riscv64 ELF: {path} ({len(node)} bytes, e_machine=243)")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="package name as published")
    parser.add_argument("version", help="version as published")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--base", default=None, help="upstream tarball URL (default: npmjs)")
    parser.add_argument("--shape", choices=("prebuild", "wrapper"), default="prebuild")
    parser.add_argument(
        "--bindings",
        action="append",
        default=None,
        help="platform package the wrapper added to optionalDependencies (repeatable)",
    )
    args = parser.parse_args(argv)

    bindings = args.bindings or []
    if args.shape == "wrapper" and not bindings:
        parser.error("--shape wrapper needs at least one --bindings entry")

    base_url = args.base or f"https://registry.npmjs.org/{args.name}/-/{args.name}-{args.version}.tgz"
    ours_url = f"{args.registry.rstrip('/')}/{args.name}/-/{args.name}-{args.version}.tgz"
    ours = members(download(ours_url))
    base = members(download(base_url))

    added, removed, problems = compare(ours, base, args.shape, bindings)
    problems += check_identity(ours, args.name, args.version)

    print(f"=== {args.name}@{args.version} ({args.shape})")
    print(f"  upstream: {len(base)} files, published: {len(ours)} files")
    print(f"  added  : {added}")
    print(f"  changed: {[p for p in (set(base) & set(ours)) if digest(base[p]) != digest(ours[p])]}")
    print(f"  removed: {removed}")
    if args.shape == "prebuild":
        problems += check_prebuild(ours, args.name)

    manifest = json.loads(ours.get("package.json", b"{}"))
    print(f"  manifest: name/version {manifest.get('name')}@{manifest.get('version')}, "
          f"publishConfig {manifest.get('publishConfig')}")
    if args.shape == "prebuild":
        expected = [f"prebuilds/linux-riscv64/{args.name}.node"]
        if added != expected:
            problems.append(f"added files are {added}, expected exactly {expected}")
    for problem in problems:
        print(f"  [FAIL] {problem}")
    if not problems:
        print("  [ok] the published package is upstream plus the riscv64 increment")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
