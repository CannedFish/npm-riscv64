"""Recipe model shared by the renderer and the change detector.

A recipe lives at::

    packages/<pkg>/<version>/            # <pkg> may be scoped: @scope/name
        .stat                            # required metadata (YAML)
        commands/<name>                  # optional: fetch, patch, build, test, publish
        patches/*.patch                  # optional
        patches/.disable                 # optional: patch file names to skip

``packages/<pkg>/common/`` is a special version directory holding snippets and
patches shared by every version of that package. It is never built itself and
never carries a ``.stat``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SNIPPET_NAMES = ("fetch", "patch", "build", "test", "publish")
REQUIRED_SNIPPETS = ("build", "test")
COMMON_DIR = "common"
STAT_FILE = ".stat"
DISABLE_FILE = ".disable"
ROLES = ("binary", "main")

#: How the recipe is built. A native recipe compiles inside a riscv64
#: container (qemu); a cross recipe compiles on the host architecture with a
#: riscv64 cross toolchain and is then tested in the riscv64 container.
TOOLCHAIN_NATIVE = "native"
TOOLCHAIN_CROSS = "cross"
TOOLCHAIN_KINDS = (TOOLCHAIN_NATIVE, TOOLCHAIN_CROSS)
DEFAULT_TOOLCHAIN = TOOLCHAIN_NATIVE

#: Reference names that move and therefore cannot pin a source tree.
MUTABLE_REFS = frozenset(
    {"main", "master", "develop", "dev", "head", "latest", "trunk", "next"}
)


class RecipeError(Exception):
    """Raised when a recipe is malformed."""


@dataclass(frozen=True)
class Target:
    """A buildable (package, version) pair."""

    pkg: str
    ver: str
    path: Path

    @property
    def slug_pkg(self) -> str:
        """Filesystem/identifier-safe form of the package name."""
        return slugify(self.pkg)

    @property
    def slug_ver(self) -> str:
        return slugify(self.ver)

    @property
    def artifact_name(self) -> str:
        return f"{self.slug_pkg}-{self.slug_ver}-riscv64"

    @property
    def ident(self) -> str:
        return f"{self.pkg}@{self.ver}"


def slugify(value: str) -> str:
    """Turn ``@scope/name`` into ``scope-name``.

    ``@`` and ``/`` are not usable in job names, artifact names or directory
    names, and ``.``/``-`` are kept so versions stay readable.
    """
    out = value[1:] if value.startswith("@") else value
    out = out.replace("/", "-")
    keep = []
    for ch in out:
        keep.append(ch if (ch.isalnum() or ch in ".-_") else "-")
    return "".join(keep)


def split_recipe_path(relative: str) -> tuple[str, str] | None:
    """Parse a path below ``packages/`` into ``(pkg, version)``.

    ``rolldown/1.0.0/...``      -> ("rolldown", "1.0.0")
    ``@scope/name/1.0.0/...``   -> ("@scope/name", "1.0.0")
    ``@scope`` and anything else that is not a concrete version -> None
    """
    parts = [p for p in relative.split("/") if p]
    if not parts:
        return None
    if parts[0].startswith("@"):
        if len(parts) < 3:
            return None
        return f"{parts[0]}/{parts[1]}", parts[2]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def package_names(root: Path) -> list[str]:
    """Every package name under ``root``, in sorted order (scopes expanded)."""
    names: list[str] = []
    if not root.is_dir():
        return names
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("@"):
            for sub in sorted(entry.iterdir()):
                if sub.is_dir():
                    names.append(f"{entry.name}/{sub.name}")
        else:
            names.append(entry.name)
    return names


def versions_of(root: Path, pkg: str) -> list[str]:
    """Version directories of ``pkg`` (``common`` excluded), sorted."""
    pkg_dir = root.joinpath(*pkg.split("/"))
    if not pkg_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in pkg_dir.iterdir()
        if entry.is_dir() and entry.name != COMMON_DIR
    )


def enumerate_targets(root: Path) -> list[Target]:
    targets: list[Target] = []
    for pkg in package_names(root):
        pkg_dir = root.joinpath(*pkg.split("/"))
        for ver in versions_of(root, pkg):
            targets.append(Target(pkg=pkg, ver=ver, path=pkg_dir / ver))
    return targets


def find_target(root: Path, pkg: str, ver: str) -> Target:
    path = root.joinpath(*pkg.split("/")) / ver
    if not path.is_dir():
        raise RecipeError(f"recipe directory not found: {path}")
    return Target(pkg=pkg, ver=ver, path=path)


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def toolchain_kind(stat: dict) -> str:
    """Return the toolchain a recipe asks for (native when unspecified)."""
    value = str(stat.get("toolchain") or DEFAULT_TOOLCHAIN).strip().lower()
    return value if value in TOOLCHAIN_KINDS else DEFAULT_TOOLCHAIN


def load_stat(target: Target) -> dict:
    """Load and minimally shape ``.stat``."""
    stat_path = target.path / STAT_FILE
    if not stat_path.is_file():
        raise RecipeError(f"{target.ident}: missing {STAT_FILE} ({stat_path})")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RecipeError("PyYAML is required to read .stat") from exc

    text = stat_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RecipeError(f"{target.ident}: cannot parse {STAT_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError(f"{target.ident}: {STAT_FILE} must be a mapping")
    return data


def is_mutable_ref(ref: str) -> bool:
    """True when ``ref`` is a branch-like name that can move under our feet."""
    cleaned = ref.strip().rstrip("/")
    if cleaned.startswith("refs/heads/"):
        return True
    base = cleaned.rsplit("/", 1)[-1]
    return base.lower() in MUTABLE_REFS


def validate_stat(target: Target, stat: dict) -> list[str]:
    """Return a list of human readable problems; empty when the recipe is sane."""
    problems: list[str] = []

    upstream = stat.get("upstream") or {}
    source = stat.get("source") or {}
    npm_src = stat.get("npm") or {}
    artifacts = stat.get("artifacts") or {}
    publish = stat.get("npm_publish") or []

    if not (upstream or source or npm_src):
        problems.append(
            "no source route: at least one of upstream / source / npm is required"
        )

    if upstream:
        repo = upstream.get("repo")
        ref = upstream.get("ref")
        if not repo:
            problems.append("upstream.repo is required")
        if not ref:
            problems.append("upstream.ref is required")
        elif is_mutable_ref(str(ref)):
            problems.append(
                f"upstream.ref '{ref}' is a mutable reference; "
                "use a tag or a commit SHA"
            )

    if source:
        url = source.get("url")
        sha = source.get("sha256")
        if not url:
            problems.append("source.url is required")
        elif is_mutable_ref(str(url)):
            problems.append(
                f"source.url '{url}' points at a mutable reference; "
                "pin a specific version"
            )
        if not sha:
            problems.append(
                "source.sha256 is required: an unpinned archive makes the "
                "source tree ambiguous"
            )
        elif len(str(sha)) != 64 or any(c not in "0123456789abcdefABCDEF" for c in str(sha)):
            problems.append("source.sha256 must be a 64 character hex digest")

    if npm_src:
        if not npm_src.get("name"):
            problems.append("npm.name is required")
        if not npm_src.get("version"):
            problems.append("npm.version is required")

    if not artifacts.get("path"):
        problems.append("artifacts.path is required (glob of the built artifact)")

    raw_toolchain = stat.get("toolchain")
    if raw_toolchain is not None and str(raw_toolchain).strip().lower() not in TOOLCHAIN_KINDS:
        problems.append(
            f"toolchain '{raw_toolchain}' is not supported; use one of {TOOLCHAIN_KINDS} "
            f"(omit the field for the default '{DEFAULT_TOOLCHAIN}')"
        )

    if not isinstance(publish, list) or not publish:
        problems.append(
            "npm_publish must be a non-empty list (at least one publish target)"
        )
    else:
        problems.extend(_validate_publish_entries(target, publish))

    return problems


def _validate_publish_entries(target: Target, publish: Iterable) -> list[str]:
    problems: list[str] = []
    names: list[str] = []
    versions: dict[str, str] = {}

    for index, entry in enumerate(publish):
        where = f"npm_publish[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue
        role = entry.get("role")
        name = entry.get("name")
        version = entry.get("version")
        if role not in ROLES:
            problems.append(f"{where}.role must be one of {ROLES}")
        if not name:
            problems.append(f"{where}.name is required")
        elif name in names:
            problems.append(f"{where}.name '{name}' is duplicated")
        else:
            names.append(name)
            versions[name] = str(version or "")
        if not version:
            problems.append(f"{where}.version is required")
        elif str(version) != target.ver:
            problems.append(
                f"{where}.version '{version}' does not match the recipe version "
                f"directory '{target.ver}' (one directory describes one release)"
            )
        if not entry.get("dir"):
            problems.append(f"{where}.dir is required (path inside the source tree)")
        if role == "binary" and not (entry.get("os") and entry.get("cpu")):
            problems.append(f"{where}: role=binary requires os and cpu")

    for index, entry in enumerate(publish):
        if not isinstance(entry, dict):
            continue
        for dep in entry.get("requires") or []:
            if dep not in versions:
                problems.append(
                    f"npm_publish[{index}].requires references unknown package '{dep}'"
                )
            else:
                dep_version = versions[dep]
                entry_version = str(entry.get("version") or "")
                if dep_version and entry_version and dep_version != entry_version:
                    problems.append(
                        f"npm_publish[{index}] version {entry_version} does not "
                        f"match required package {dep}@{dep_version}"
                    )

    if not any(isinstance(e, dict) and e.get("role") == "main" for e in publish):
        problems.append("npm_publish needs at least one role=main entry")
    return problems


def repository_problems(root: Path) -> list[str]:
    """Structural problems of the whole recipe tree.

    Every version directory is a buildable recipe (D1-C), so an empty package
    directory or a version directory without .stat is a half-finished recipe and
    must fail loudly instead of being skipped silently.
    """
    problems: list[str] = []
    for pkg in package_names(root):
        pkg_dir = root.joinpath(*pkg.split("/"))
        versions = [
            entry
            for entry in sorted(pkg_dir.iterdir())
            if entry.is_dir() and entry.name != COMMON_DIR
        ]
        for entry in sorted(pkg_dir.iterdir()):
            if entry.is_file():
                problems.append(
                    f"{pkg}: unexpected file '{entry.name}' in the package directory "
                    "(versions hold the data, the package directory only groups them)"
                )
        if not versions:
            problems.append(f"{pkg}: package directory has no version subdirectory")
        for version_dir in versions:
            if not (version_dir / STAT_FILE).is_file():
                problems.append(
                    f"{pkg}/{version_dir.name}: version directory is missing {STAT_FILE}"
                )
    return problems


def main_publish_name(stat: dict, fallback: str) -> str:
    for entry in stat.get("npm_publish") or []:
        if isinstance(entry, dict) and entry.get("role") == "main" and entry.get("name"):
            return str(entry["name"])
    return fallback


# --------------------------------------------------------------------------
# layering
# --------------------------------------------------------------------------


@dataclass
class Layers:
    """Common + version overlay resolution for one recipe."""

    commands: dict[str, Path] = field(default_factory=dict)
    patches: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def read_disable_list(directory: Path) -> set[str]:
    disable = directory / DISABLE_FILE
    if not disable.is_file():
        return set()
    names = set()
    for raw in disable.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def resolve_layers(root: Path, target: Target) -> Layers:
    """Merge ``common`` (baseline) with the version directory (override).

    Same-named snippet or patch in the version directory replaces the common
    one; other files are inherited/added. ``patches/.disable`` removes entries
    from the effective patch list.
    """
    layers = Layers()
    common = target.path.parent / COMMON_DIR

    disabled: set[str] = set()
    for directory in (common, target.path):
        disabled |= read_disable_list(directory / "patches")

    patch_files: list[Path] = []
    seen: dict[str, Path] = {}
    for directory in (common, target.path):
        patches_dir = directory / "patches"
        if not patches_dir.is_dir():
            continue
        for entry in sorted(patches_dir.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.suffix != ".patch":
                layers.problems.append(f"ignoring non-.patch file: {entry}")
                continue
            seen[entry.name] = entry
    for name in sorted(seen):
        if name not in disabled:
            patch_files.append(seen[name])
    layers.patches = patch_files

    for directory in (common, target.path):
        commands_dir = directory / "commands"
        if not commands_dir.is_dir():
            continue
        for entry in sorted(commands_dir.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.name not in SNIPPET_NAMES:
                layers.problems.append(
                    f"unknown command snippet '{entry.name}' in {commands_dir} "
                    f"(expected one of {', '.join(SNIPPET_NAMES)})"
                )
                continue
            layers.commands[entry.name] = entry
    return layers


def read_snippet(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip("\n")


def relative_recipe_dir(root: Path, target: Target) -> str:
    """POSIX path of the recipe directory relative to ``root``'s parent."""
    return os.path.relpath(target.path, start=root.parent).replace(os.sep, "/")
