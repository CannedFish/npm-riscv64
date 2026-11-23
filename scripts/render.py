#!/usr/bin/env python3
"""Render a recipe (``packages/<pkg>/<ver>``) into a composite action.

The renderer is the only place that knows how a recipe maps onto YAML:

* it merges the ``common`` layer with the version layer (same-named file wins),
* it turns ``.stat`` metadata into template variables,
* it generates the ``fetch`` / ``patch`` / ``publish`` snippets when the recipe
  does not provide them,
* it substitutes placeholders with correct indentation, so multi-line snippets
  stay valid YAML,
* it validates the result (YAML parse + structural checks).

Usage::

    render.py --pkg @scope/name --ver 1.2.3 --out out/action.yml
    render.py --check-all

All comments and messages are English on purpose: the repository is meant to be
read by users outside of RuyiSDK (see .dsh/plan.md, N1).
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recipe_lib as rl  # noqa: E402

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
CONDITION_OPEN = re.compile(r"^\s*\{\{\s*#(if|unless)\s+([A-Za-z0-9_.\-]+)\s*\}\}\s*$")
CONDITION_CLOSE = re.compile(r"^\s*\{\{\s*/(if|unless)\s*\}\}\s*$")

DEFAULT_TEMPLATE = ".github/templates/action.template.yml"
DEFAULT_ROOT = "packages"
SRC_DIR = "src"

#: The Rust target triple a cross recipe builds for. Cargo spells the linker /
#: compiler environment variables after it, and Cargo.toml-level tooling expects
#: the same spelling.
RISCV_TARGET_TRIPLE = "riscv64gc-unknown-linux-gnu"


class RenderError(Exception):
    pass


# --------------------------------------------------------------------------
# template engine
# --------------------------------------------------------------------------


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip() != ""


def apply_conditionals(text: str, variables: dict) -> str:
    """Handle ``{{#if key}}...{{/if}}`` and ``{{#unless key}}...{{/unless}}``.

    Blocks are processed by a tiny stack machine; nesting is supported. This
    keeps the five steps of the action in the template (structure stays in one
    place) while still allowing optional steps to disappear entirely.
    """
    out: list[str] = []
    stack: list[tuple[str, bool]] = []  # (kind, emitting)

    for line in text.split("\n"):
        opener = CONDITION_OPEN.match(line)
        if opener:
            kind, key = opener.group(1), opener.group(2)
            if key not in variables:
                raise RenderError(
                    f"conditional on unknown variable '{key}' "
                    "(define it or fix the template)"
                )
            value = _truthy(variables[key])
            emitting = value if kind == "if" else not value
            if stack and not stack[-1][1]:
                emitting = False
            stack.append((kind, emitting))
            continue

        closer = CONDITION_CLOSE.match(line)
        if closer:
            if not stack:
                raise RenderError(f"unexpected {closer.group(0).strip()}")
            kind, _ = stack.pop()
            if kind != closer.group(1):
                raise RenderError(
                    f"mismatched block end: opened with {kind}, "
                    f"closed with {closer.group(1)}"
                )
            continue

        if stack and not stack[-1][1]:
            continue
        out.append(line)

    if stack:
        raise RenderError(f"unclosed conditional block: {stack[-1][0]}")
    return "\n".join(out)


def substitute(text: str, variables: dict) -> tuple[str, set[str], set[str]]:
    """Replace placeholders; re-indent continuation lines of multi-line values.

    A placeholder that expands to several lines must have every following line
    prefixed with the indentation of the line the placeholder sits on, otherwise
    the result is not valid YAML (this is the defect the old renderer had).
    """
    missing: set[str] = set()
    used: set[str] = set()
    out_lines: list[str] = []

    for line in text.split("\n"):
        if "{{" not in line:
            out_lines.append(line)
            continue

        indent = line[: len(line) - len(line.lstrip())]

        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in variables:
                missing.add(key)
                return match.group(0)
            used.add(key)
            value = variables[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            value = str(value)
            if "\n" not in value:
                return value
            head, *rest = value.split("\n")
            tail = [("" if not seg.strip() else indent + seg) for seg in rest]
            return "".join([head, *("\n" + seg for seg in tail)])

        out_lines.append(PLACEHOLDER.sub(replace, line))

    return "\n".join(out_lines), missing, used


def render_template(text: str, variables: dict, *, allow_unresolved: bool = False) -> str:
    expanded = apply_conditionals(text, variables)
    rendered, missing, _ = substitute(expanded, variables)
    if missing and not allow_unresolved:
        raise RenderError(
            "unresolved placeholders: " + ", ".join(sorted(missing))
        )
    # Optional blocks leave empty lines behind; keep the generated YAML tidy.
    return re.sub(r"\n{3,}", "\n\n", rendered)


# --------------------------------------------------------------------------
# generated snippets
# --------------------------------------------------------------------------


def _sh_single(value: str) -> str:
    """Quote a value for safe embedding in a shell single-quoted string."""
    return value.replace("'", "'\\''")


def _upstream_fn(stat: dict) -> str:
    upstream = stat.get("upstream") or {}
    if not upstream:
        return (
            "fetch_upstream() {\n"
            '  echo "  [fetch] route upstream: not configured" >&2\n'
            "  return 1\n"
            "}"
        )
    repo = _sh_single(str(upstream.get("repo", "")))
    ref = _sh_single(str(upstream.get("ref", "")))
    return f"""fetch_upstream() {{
  echo "  [fetch] route upstream: {repo}@{ref}"
  rm -rf "$SRC_DIR"
  if ! git clone --quiet --depth 1 --branch '{ref}' '{repo}' "$SRC_DIR" 2>/dev/null; then
    echo "  [fetch] shallow clone of '{ref}' failed, retrying with a full clone" >&2
    rm -rf "$SRC_DIR"
    git clone --quiet '{repo}' "$SRC_DIR" || return 1
    git -C "$SRC_DIR" checkout --quiet '{ref}' || return 1
  fi
  # Keep .git: the patch step can then use `git apply --3way` for context drift.
}}"""


def _source_fn(stat: dict) -> str:
    source = stat.get("source") or {}
    if not source:
        return (
            "fetch_source() {\n"
            '  echo "  [fetch] route source: not configured" >&2\n'
            "  return 1\n"
            "}"
        )
    url = _sh_single(str(source.get("url", "")))
    sha = _sh_single(str(source.get("sha256", "")))
    return f"""fetch_source() {{
  echo "  [fetch] route source: {url}"
  local tgz="$TMP_DIR/source-archive.tar.gz"
  curl -fsSL '{url}' -o "$tgz" || return 1
  if [ -n '{sha}' ]; then
    if ! echo '{sha}  '"$tgz" | sha256sum -c - >/dev/null 2>&1; then
      echo "  [fetch] sha256 mismatch for {url}" >&2
      return 1
    fi
  fi
  rm -rf "$SRC_DIR"
  mkdir -p "$SRC_DIR"
  tar xzf "$tgz" -C "$SRC_DIR" --strip-components=1 || return 1
}}"""


def _npm_fn(stat: dict) -> str:
    npm = stat.get("npm") or {}
    if not npm:
        return (
            "fetch_npm() {\n"
            '  echo "  [fetch] route npm: not configured" >&2\n'
            "  return 1\n"
            "}"
        )
    name = _sh_single(str(npm.get("name", "")))
    version = _sh_single(str(npm.get("version", "")))
    registry = _sh_single(str(npm.get("registry") or "https://registry.npmjs.org"))
    return f"""fetch_npm() {{
  echo "  [fetch] route npm: {name}@{version} from {registry}"
  local registry='{registry}'
  local encoded tarball integrity tgz="$TMP_DIR/npm-package.tgz"
  encoded=$(printf '%s' '{name}' | sed 's|/|%2F|g')
  # Resolve dist.tarball through the registry metadata API; never build URLs by hand.
  local meta
  meta=$(curl -fsSL "${{registry%/}}/$encoded") || return 1
  tarball=$(printf '%s' "$meta" | jq -r --arg v '{version}' '.versions[$v].dist.tarball // empty')
  if [ -z "$tarball" ]; then
    echo "  [fetch] version {version} is not published on $registry" >&2
    return 1
  fi
  curl -fsSL "$tarball" -o "$tgz" || return 1
  integrity=$(printf '%s' "$meta" | jq -r --arg v '{version}' '.versions[$v].dist.integrity // empty')
  case "$integrity" in
    sha512-*)
      local expected actual
      expected=$(printf '%s' "${{integrity#sha512-}}" | base64 -d | od -An -v -tx1 | tr -d ' \\n')
      actual=$(sha512sum "$tgz" | cut -d' ' -f1)
      if [ "$expected" != "$actual" ]; then
        echo "  [fetch] registry integrity mismatch for $tarball" >&2
        return 1
      fi
      ;;
  esac
  rm -rf "$SRC_DIR"
  mkdir -p "$SRC_DIR"
  tar xzf "$tgz" -C "$SRC_DIR" --strip-components=1 || return 1
  echo "  [fetch] note: registry tarballs may not contain buildable sources"
}}"""


def generate_fetch(stat: dict) -> str:
    routes = [
        ("upstream", bool(stat.get("upstream"))),
        ("source", bool(stat.get("source"))),
        ("npm", bool(stat.get("npm"))),
    ]
    attempted = [name for name, present in routes if present]
    return f"""set -euo pipefail

SRC_DIR="${{SRC_DIR:-{SRC_DIR}}}"
TMP_DIR="${{TMP_DIR:-.fetch-tmp}}"
mkdir -p "$TMP_DIR"
echo "[fetch] resolved source routes: {' '.join(attempted)}"

{_upstream_fn(stat)}

{_source_fn(stat)}

{_npm_fn(stat)}

# Highest priority wins: upstream > source > npm. A route only falls through
# when it actually fails, so a broken mirror never blocks a good upstream.
if fetch_upstream; then
  :
elif fetch_source; then
  :
elif fetch_npm; then
  :
else
  echo "ERROR: every source route failed ({', '.join(attempted) or 'none configured'})" >&2
  exit 1
fi

echo "[fetch] source tree ready at $SRC_DIR ($(ls -A "$SRC_DIR" | wc -l) entries)\""""


def generate_patch(patches: list[Path], recipe_dir: str) -> str:
    lines = [
        "set -euo pipefail",
        "",
        'REPO_ROOT="$(pwd)"',
        f'RECIPE_DIR="$REPO_ROOT/{recipe_dir}"',
        f'cd "${{SRC_DIR:-{SRC_DIR}}}"',
        "",
        "# Patches apply in order: the common layer first, the version layer after.",
    ]
    for patch in patches:
        rel = patch.name
        lines.append(f'PATCH="$RECIPE_DIR/patches/{rel}"')
        lines.append('echo "[patch] applying $PATCH"')
        lines.append(
            'if [ -d .git ]; then\n'
            '  git apply -p1 --3way "$PATCH" || { echo "ERROR: patch failed: $PATCH" >&2; exit 1; }\n'
            "else\n"
            '  git apply -p1 "$PATCH" || { echo "ERROR: patch failed: $PATCH" >&2; exit 1; }\n'
            "fi"
        )
    lines.append("")
    lines.append(f'echo "[patch] applied {len(patches)} patch(es)"')
    return "\n".join(lines)


MANIFEST_JS = r"""const fs=require("node:fs");
const path=require("node:path");
const args=process.argv.slice(1);
const dir=args[0], name=args[1], version=args[2], mode=args[3];
const reqName=args[4]||"", reqVersion=args[5]||"", mainFile=args[6]||"";
const osName=args[7]||"", cpuName=args[8]||"", registryUrl=args[9]||"";
const file=path.join(dir, "package.json");
const exists=fs.existsSync(file);
if (mode === "verify") {
  if (!exists) {
    console.error("[publish] missing manifest: " + file);
    process.exit(1);
  }
  const pkg=JSON.parse(fs.readFileSync(file, "utf8"));
  if (pkg.name !== name) {
    console.error("[publish] name mismatch: manifest has " + pkg.name + ", recipe wants " + name);
    console.error("[publish] a rename would publish a different package; refusing");
    process.exit(1);
  }
  if (String(pkg.version) !== String(version)) {
    console.error("[publish] version mismatch: manifest has " + pkg.version + ", recipe wants " + version);
    process.exit(1);
  }
  if (reqName && (pkg.optionalDependencies || {})[reqName] !== reqVersion) {
    console.error("[publish] " + reqName + " must be pinned to " + reqVersion + " in optionalDependencies");
    process.exit(1);
  }
  // A manifest that pins a different registry would publish outside the intent of
  // the recipe, and --registry cannot override publishConfig (see generate mode).
  // (No apostrophes here: this JavaScript is embedded in a single-quoted shell string.)
  if (registryUrl && pkg.publishConfig && pkg.publishConfig.registry
      && String(pkg.publishConfig.registry).replace(/\/+$/, "") !== registryUrl.replace(/\/+$/, "")) {
    console.error("[publish] " + file + " pins publishConfig.registry=" + pkg.publishConfig.registry
      + " but this recipe publishes to " + registryUrl);
    console.error("[publish] npm prefers publishConfig over --registry; align the manifest or let the renderer generate it (generate_manifest: true)");
    process.exit(1);
  }
  process.exit(0);
}
if (mode !== "generate" && mode !== "verify") {
  console.error("[publish] unknown manifest mode: " + mode);
  process.exit(1);
}
const pkg = exists ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
pkg.name = name;
pkg.version = version;
if (mainFile && !pkg.main) { pkg.main = mainFile; }
if (osName) { pkg.os = [osName]; }
if (cpuName) { pkg.cpu = [cpuName]; }
if (reqName) {
  pkg.optionalDependencies = pkg.optionalDependencies || {};
  pkg.optionalDependencies[reqName] = reqVersion;
}
// npm gives publishConfig.registry precedence over the --registry flag, so an
// upstream manifest that pins its own registry would silently send the tarball
// somewhere else. The registry from the recipe is the single source of truth (D13).
if (registryUrl && pkg.publishConfig && pkg.publishConfig.registry
    && String(pkg.publishConfig.registry).replace(/\/+$/, "") !== registryUrl.replace(/\/+$/, "")) {
  console.log("[publish] publishConfig.registry " + pkg.publishConfig.registry
    + " -> " + registryUrl + " (the recipe decides where this package goes)");
  pkg.publishConfig.registry = registryUrl;
}
fs.writeFileSync(file, JSON.stringify(pkg, null, 2) + "\n");
console.log("[publish] wrote " + file + " (" + mode + ")");
"""


def _publish_order(entries: list[dict]) -> list[dict]:
    """Binary first, then topological order over ``requires``."""
    by_name = {str(e.get("name")): e for e in entries if isinstance(e, dict)}

    def deps(entry: dict) -> list[str]:
        return [str(d) for d in (entry.get("requires") or [])]

    ordered: list[dict] = []
    seen: set[str] = set()
    pending = sorted(
        [e for e in entries if isinstance(e, dict)],
        key=lambda e: (0 if e.get("role") == "binary" else 1, str(e.get("name"))),
    )

    def visit(entry: dict) -> None:
        name = str(entry.get("name"))
        if name in seen:
            return
        for dep in deps(entry):
            if dep in by_name:
                visit(by_name[dep])
        seen.add(name)
        ordered.append(entry)

    for entry in pending:
        visit(entry)
    return ordered


def generate_publish(stat: dict, target: rl.Target, artifact_glob: str) -> str:
    entries = _publish_order(
        [e for e in (stat.get("npm_publish") or []) if isinstance(e, dict)]
    )
    lines: list[str] = [
        "set -euo pipefail",
        "",
        "# Publishing runs on the host: moving files and talking to a registry does",
        "# not need riscv64 emulation, and it keeps token handling straightforward.",
        'REPO_ROOT="$(pwd)"',
        'SRC_DIR="${SRC_DIR:-src}"',
        'ARTIFACT_GLOB="'
        + (('$SRC_DIR/' + artifact_glob) if artifact_glob else "")
        + '"',
        'export NPM_CONFIG_USERCONFIG="$REPO_ROOT/.npmrc.publish"',
        ': > "$NPM_CONFIG_USERCONFIG"',
        "",
        'log() { echo "[publish] $*"; }',
        "",
        "setup_auth() {",
        '  local registry="$1" token_env="$2"',
        '  [ -n "$token_env" ] || return 0',
        '  if [ -z "${!token_env:-}" ]; then',
        '    log "no token in $token_env; talking to $registry unauthenticated"',
        "    return 0",
        "  fi",
        '  local host="${registry#*://}"; host="${host%%/*}"',
        "  printf '//%s/:_authToken=${%s}\\n' \"$host\" \"$token_env\" >> \"$NPM_CONFIG_USERCONFIG\"",
        '  log "auth configured for $host via $token_env"',
        "}",
        "",
        "publish_one() {",
        '  local rel_dir="$1" name="$2" version="$3" registry="$4" mode="$5"',
        '  local req_name="$6" req_version="$7" os_name="$8" cpu_name="$9"',
        '  # dir is relative to the fetched source tree, never to the repository root.',
        '  local dir="$SRC_DIR/$rel_dir"',
        '  log "package $name@$version (dir=$dir, manifest=$mode)"',
        '  local main_file=""',
        '  if [ "$rel_dir" != "." ]; then',
        '    mkdir -p "$dir"',
        "    local f",
        '    for f in $ARTIFACT_GLOB; do',
        '      [ -e "$f" ] || continue',
        '      cp -v "$f" "$dir/"',
        '      main_file=$(basename "$f")',
        "    done",
        "  fi",
        '  # Always publish an absolute path: npm reads a two-segment relative path',
        '  # such as "src/." as a github shorthand spec, not as a directory.',
        '  local abs_dir',
        '  abs_dir=$(cd "$dir" && pwd) || { log "cannot enter $dir"; return 1; }',
        "  node -e '" + MANIFEST_JS + "' \"$dir\" \"$name\" \"$version\" \"$mode\" "
        '"$req_name" "$req_version" "$main_file" "$os_name" "$cpu_name" "$registry"',
        '  if npm view "$name@$version" version --registry "$registry" >/dev/null 2>&1; then',
        '    log "already published: $name@$version (skipped)"',
        "    return 0",
        "  fi",
        '  npm publish "$abs_dir" --registry "$registry" --access public \\',
        "    --ignore-scripts --no-audit --no-fund",
        "}",
        "",
        "# Credentials first: the idempotency probe itself may need auth.",
    ]

    seen_auth: set[tuple[str, str]] = set()
    for entry in entries:
        registry = str(entry.get("registry") or "https://registry.npmjs.org")
        token_env = str(entry.get("token_env") or "")
        key = (registry, token_env)
        if key in seen_auth:
            continue
        seen_auth.add(key)
        lines.append(f'setup_auth "{registry}" "{token_env}"')

    lines.append("")
    lines.append(
        "# Publish order: native/binary packages first, then the JS package that"
    )
    lines.append("# references them through optionalDependencies.")
    for entry in entries:
        dir_value = str(entry.get("dir", "."))
        name = str(entry.get("name", ""))
        version = str(entry.get("version", ""))
        registry = str(entry.get("registry") or "https://registry.npmjs.org")
        mode = "generate" if entry.get("generate_manifest") else "verify"
        requires = [str(d) for d in (entry.get("requires") or [])]
        req_name = requires[0] if requires else ""
        req_version = ""
        if req_name:
            for candidate in entries:
                if str(candidate.get("name")) == req_name:
                    req_version = str(candidate.get("version", ""))
                    break
        lines.append(
            "publish_one "
            + " ".join(
                [
                    f'"{dir_value}"',
                    f'"{name}"',
                    f'"{version}"',
                    f'"{registry}"',
                    f'"{mode}"',
                    f'"{req_name}"',
                    f'"{req_version}"',
                    f'"{entry.get("os") or ""}"',
                    f'"{entry.get("cpu") or ""}"',
                ]
            )
        )

    lines.append("")
    lines.append('log "publish finished for ' + str(target.ident) + '"')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# variables
# --------------------------------------------------------------------------


def flatten(prefix: str, value, out: dict) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            flatten(f"{prefix}.{key}" if prefix else str(key), sub, out)
    elif isinstance(value, list):
        # Lists are exposed through dedicated variables, not flattened.
        return
    else:
        out[prefix] = "" if value is None else value


def build_variables(
    target: rl.Target, stat: dict, layers: rl.Layers, recipe_dir: str, steps_dir: str
) -> tuple[dict, dict[str, str]]:
    variables: dict = {}
    flatten("", stat, variables)

    # Optional metadata is exposed as empty strings so templates can reference it
    # unconditionally; only genuine typos end up as unresolved placeholders.
    for key in (
        "upstream.repo",
        "upstream.ref",
        "upstream.subdir",
        "source.url",
        "source.sha256",
        "npm.name",
        "npm.version",
        "npm.registry",
        "artifacts.path",
    ):
        variables.setdefault(key, "")

    # Which toolchain the recipe asks for decides where the steps run: a native
    # recipe builds and tests inside the riscv64 container (qemu), a cross recipe
    # builds on the host architecture with a riscv64 cross toolchain and tests in
    # the riscv64 container, so the artifact is still exercised on its own CPU.
    cross = rl.toolchain_kind(stat) == rl.TOOLCHAIN_CROSS
    if cross:
        build_platform = "linux/amd64"
        build_image = "npm-riscv64/toolchain:cross"
        build_dockerfile = "docker/riscv64-cross.Dockerfile"
        test_image = "npm-riscv64/toolchain:riscv64"
    else:
        build_platform = "linux/riscv64"
        build_image = test_image = "npm-riscv64/toolchain:riscv64"
        build_dockerfile = "docker/riscv64-toolchain.Dockerfile"

    main_name = rl.main_publish_name(stat, target.pkg)
    variables.update(
        {
            "pkg.name": main_name,
            "pkg.dirname": target.pkg.split("/")[-1],
            "pkg.full": target.pkg,
            "pkg.version": target.ver,
            "pkg.ident": target.ident,
            "pkg.slug": target.slug_pkg,
            "pkg.artifact": target.artifact_name,
            "pkg.target": f"{target.pkg}/{target.ver}",
            "recipe.dir": recipe_dir,
            "src.dir": SRC_DIR,
            "steps.dir": steps_dir,
            "has.patches": bool(layers.patches),
            "job.cross": cross,
            "job.toolchain": rl.toolchain_kind(stat),
            "build.platform": build_platform,
            "build.image": build_image,
            "build.dockerfile": build_dockerfile,
            "test.platform": "linux/riscv64",
            "test.image": test_image,
            "riscv.triple": RISCV_TARGET_TRIPLE,
        }
    )

    scripts: dict[str, str] = {
        name: rl.read_snippet(path) for name, path in layers.commands.items()
    }
    artifact_glob = str((stat.get("artifacts") or {}).get("path") or "")

    if "fetch" not in scripts:
        scripts["fetch"] = generate_fetch(stat)
    if "patch" not in scripts and layers.patches:
        scripts["patch"] = generate_patch(layers.patches, recipe_dir)
    if "publish" not in scripts:
        scripts["publish"] = generate_publish(stat, target, artifact_glob)

    return variables, scripts


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def check_rendered(target: rl.Target, rendered: str) -> list[str]:
    """Structural checks on the rendered action."""
    problems: list[str] = []
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RenderError("PyYAML is required to validate the rendered action") from exc

    try:
        data = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        return [f"{target.ident}: rendered action is not valid YAML: {exc}"]

    if not isinstance(data, dict):
        return [f"{target.ident}: rendered action is not a mapping"]

    runs = data.get("runs") or {}
    if runs.get("using") != "composite":
        problems.append(f"{target.ident}: runs.using must be 'composite'")
    steps = runs.get("steps") or []
    if not steps:
        problems.append(f"{target.ident}: rendered action has no steps")

    names = [str(step.get("name", "")) for step in steps if isinstance(step, dict)]
    for expected in ("fetch", "build", "test", "publish"):
        if not any(expected in name for name in names):
            problems.append(f"{target.ident}: missing step for '{expected}'")

    for step in steps:
        if not isinstance(step, dict):
            continue
        if "uses" in step:
            continue
        if not step.get("shell"):
            problems.append(
                f"{target.ident}: step '{step.get('name')}' has no shell (composite actions require it)"
            )
        body = str(step.get("run", ""))
        if not body.strip():
            problems.append(f"{target.ident}: step '{step.get('name')}' has an empty run")
    return problems


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def render_target(
    root: Path,
    pkg: str,
    ver: str,
    template: Path,
    overrides: dict[str, str] | None = None,
    *,
    steps_dir: str | None = None,
    allow_unresolved: bool = False,
    publish_scope: str | None = None,
    publish_registry: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Render one recipe.

    Returns ``(action_yaml, scripts)`` where ``scripts`` maps snippet names to
    the shell content that belongs in ``<out-dir>/steps/<name>.sh``.
    """
    target = rl.find_target(root, pkg, ver)
    stat = rl.load_stat(target)
    for dotted, value in (overrides or {}).items():
        apply_override(stat, dotted, value)
    if publish_scope:
        apply_publish_scope(stat, publish_scope)
    if publish_registry:
        apply_publish_registry(stat, publish_registry)

    problems = rl.validate_stat(target, stat)
    layers = rl.resolve_layers(root, target)
    problems.extend(f"{target.ident}: {p}" for p in layers.problems)

    for name in rl.REQUIRED_SNIPPETS:
        if name not in layers.commands:
            problems.append(
                f"{target.ident}: required command snippet '{name}' is missing "
                f"(looked in commands/ of the version and of common/)"
            )
    if problems:
        raise RenderError("\n".join(problems))

    recipe_dir = rl.relative_recipe_dir(root, target)
    if steps_dir is None:
        steps_dir = f".rendered/{target.slug_pkg}/{target.slug_ver}"
    variables, scripts = build_variables(target, stat, layers, recipe_dir, steps_dir)
    text = template.read_text(encoding="utf-8")
    rendered = render_template(text, variables, allow_unresolved=allow_unresolved)

    problems.extend(check_rendered(target, rendered))
    if problems:
        raise RenderError("\n".join(problems))
    return rendered, scripts


def apply_override(stat: dict, dotted: str, value: str) -> None:
    """Set ``stat`` field addressed by a dotted path (``npm_publish.0.registry``)."""
    parts = dotted.split(".")
    node = stat
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if isinstance(node, list):
            try:
                position = int(part)
            except ValueError as exc:
                raise RenderError(f"override '{dotted}': list index expected, got '{part}'") from exc
            if position >= len(node):
                raise RenderError(f"override '{dotted}': index {position} out of range")
            if last:
                node[position] = value
            else:
                node = node[position]
        elif isinstance(node, dict):
            if last:
                node[part] = value
            else:
                if part not in node or not isinstance(node[part], (dict, list)):
                    node[part] = {}
                node = node[part]
        else:
            raise RenderError(f"override '{dotted}': cannot descend into '{part}'")


def apply_publish_scope(stat: dict, scope: str) -> None:
    """Rewrite the npm scope of every publish target, references included.

    GitHub Packages only accepts packages whose scope equals the repository owner,
    so a fork cannot publish ``@ruyisdk/*``. Rewriting here keeps the recipe
    untouched and makes the rename explicit at render time.
    """
    clean = scope.strip().lstrip("@").strip("/")
    if not clean:
        raise RenderError("--publish-scope needs a non-empty scope name")

    entries = [e for e in (stat.get("npm_publish") or []) if isinstance(e, dict)]
    renamed: dict[str, str] = {}
    for entry in entries:
        name = str(entry.get("name") or "")
        if not name:
            continue
        local = name.split("/", 1)[1] if name.startswith("@") and "/" in name else name
        renamed[name] = f"@{clean}/{local}"

    for entry in entries:
        name = str(entry.get("name") or "")
        if name in renamed:
            entry["name"] = renamed[name]
        requires = entry.get("requires")
        if isinstance(requires, list):
            entry["requires"] = [renamed.get(str(dep), str(dep)) for dep in requires]


def apply_publish_registry(stat: dict, registry: str) -> None:
    """Point every publish target at one registry (local, fork, or mirror)."""
    url = registry.strip().rstrip("/")
    if not url:
        raise RenderError("--publish-registry needs a registry URL")
    for entry in stat.get("npm_publish") or []:
        if isinstance(entry, dict):
            entry["registry"] = url


def cmd_check_all(args: argparse.Namespace) -> int:
    root = Path(args.root)
    template = Path(args.template)
    targets = rl.enumerate_targets(root)
    if not targets:
        print(f"no recipes found under {root}")
        return 0

    failures = 0
    for problem in rl.repository_problems(root):
        failures += 1
        print(f"[FAIL] repository: {problem}")

    for target in targets:
        try:
            rendered, scripts = render_target(
                root, target.pkg, target.ver, template, {}, allow_unresolved=args.allow_unresolved
            )
        except RenderError as exc:
            failures += 1
            print(f"[FAIL] {target.ident}")
            for line in str(exc).splitlines():
                print(f"       {line}")
            continue
        steps = rendered.count("\n    - name:")
        print(
            f"[ OK ] {target.ident}  (steps≈{steps}, scripts={','.join(sorted(scripts))}, "
            f"{len(rendered)} bytes)"
        )

    print(f"\nchecked {len(targets)} recipe(s), {failures} failure(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a recipe into a composite action")
    parser.add_argument("--pkg", help="package name, e.g. @scope/name")
    parser.add_argument("--ver", help="version directory, e.g. 1.2.3")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="recipes root (default: packages)")
    parser.add_argument(
        "--template", default=DEFAULT_TEMPLATE, help=f"action template (default: {DEFAULT_TEMPLATE})"
    )
    parser.add_argument("--out", help="output file for the rendered action")
    parser.add_argument(
        "--out-dir",
        help="output directory: writes action.yml and steps/*.sh (preferred over --out)",
    )
    parser.add_argument(
        "--steps-dir",
        help="workspace-relative directory the action refers to (default: --out-dir); "
        "used by the golden test so the rendered paths do not depend on where it is written",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="override a .stat field, e.g. npm_publish.0.registry=http://127.0.0.1:4873",
    )
    parser.add_argument(
        "--publish-scope",
        help="rewrite the npm scope of every publish target (forks publishing to "
        "GitHub Packages need scope == repository owner)",
    )
    parser.add_argument(
        "--publish-registry",
        help="point every publish target at this registry URL",
    )
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="keep unresolved placeholders instead of failing (debugging only)",
    )
    parser.add_argument(
        "--check-all",
        action="store_true",
        help="render every recipe, validate it and report (no files written)",
    )
    args = parser.parse_args(argv)

    overrides: dict[str, str] = {}
    for item in args.override:
        if "=" not in item:
            parser.error(f"--override expects PATH=VALUE, got '{item}'")
        key, value = item.split("=", 1)
        overrides[key.strip()] = value

    if args.check_all:
        return cmd_check_all(args)

    if not args.pkg or not args.ver:
        parser.error("--pkg and --ver are required (or use --check-all)")
    if not args.out and not args.out_dir:
        parser.error("either --out-dir (preferred) or --out is required")

    out_dir = Path(args.out_dir) if args.out_dir else None
    steps_dir = args.steps_dir or (out_dir.as_posix() if out_dir else None)

    try:
        rendered, scripts = render_target(
            Path(args.root),
            args.pkg,
            args.ver,
            Path(args.template),
            overrides,
            steps_dir=steps_dir,
            allow_unresolved=args.allow_unresolved,
            publish_scope=args.publish_scope,
            publish_registry=args.publish_registry,
        )
    except rl.RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        target_path = out_dir / "action.yml"
        target_path.write_text(rendered, encoding="utf-8")
        steps_path = out_dir / "steps"
        steps_path.mkdir(exist_ok=True)
        for name, content in sorted(scripts.items()):
            script_path = steps_path / f"{name}.sh"
            script_path.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
            script_path.chmod(0o755)
        print(
            f"rendered {args.pkg}@{args.ver} -> {out_dir}"
            f" (action.yml + {len(scripts)} script(s))"
        )
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        print(f"rendered {args.pkg}@{args.ver} -> {out} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
