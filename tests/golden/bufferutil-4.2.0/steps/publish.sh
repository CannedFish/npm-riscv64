set -euo pipefail

# Publishing runs on the host: moving files and talking to a registry does
# not need riscv64 emulation, and it keeps token handling straightforward.
REPO_ROOT="$(pwd)"
SRC_DIR="${SRC_DIR:-src}"
ARTIFACT_GLOB="$SRC_DIR/prebuilds/linux-riscv64/*.node"
export NPM_CONFIG_USERCONFIG="$REPO_ROOT/.npmrc.publish"
: > "$NPM_CONFIG_USERCONFIG"

log() { echo "[publish] $*"; }

setup_auth() {
  local registry="$1" token_env="$2" auth_kind="$3"
  [ -n "$token_env" ] || return 0
  if [ -z "${!token_env:-}" ]; then
    log "no credential in $token_env; talking to $registry unauthenticated"
    return 0
  fi
  # npm looks credentials up by the registry URL it talks to, path included:
  # "//host/repository/name/" is the key for a registry at .../repository/name/.
  local dart="${registry#*://}"
  dart="${dart%%\?*}"
  dart="//$dart"
  [ "${dart: -1}" = "/" ] || dart="$dart/"
  if [ "$auth_kind" = "basic" ]; then
    # Basic credentials: base64 of user:password, as the registry asks for it
    # (WWW-Authenticate: Basic).
    printf '%s:_auth=${%s}\n' "$dart" "$token_env" >> "$NPM_CONFIG_USERCONFIG"
  else
    printf '%s:_authToken=${%s}\n' "$dart" "$token_env" >> "$NPM_CONFIG_USERCONFIG"
  fi
  log "auth configured for $dart via $token_env ($auth_kind)"
}

publish_one() {
  local rel_dir="$1" name="$2" version="$3" registry="$4" mode="$5"
  local req_name="$6" req_version="$7" os_name="$8" cpu_name="$9"
  # dir is relative to the fetched source tree, never to the repository root.
  local dir="$SRC_DIR/$rel_dir"
  log "package $name@$version (dir=$dir, manifest=$mode)"
  local main_file=""
  if [ "$rel_dir" != "." ]; then
    mkdir -p "$dir"
    local f
    for f in $ARTIFACT_GLOB; do
      [ -e "$f" ] || continue
      cp -v "$f" "$dir/"
      main_file=$(basename "$f")
    done
  fi
  # Always publish an absolute path: npm reads a two-segment relative path
  # such as "src/." as a github shorthand spec, not as a directory.
  local abs_dir
  abs_dir=$(cd "$dir" && pwd) || { log "cannot enter $dir"; return 1; }
  node -e 'const fs=require("node:fs");
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
' "$dir" "$name" "$version" "$mode" "$req_name" "$req_version" "$main_file" "$os_name" "$cpu_name" "$registry"
  # Publish, then read the registry answer, instead of trusting a lookup:
  # a registry that proxies or groups another one serves upstream versions of
  # the same name, so "npm view <name>@<version>" would report our own
  # package as already published and silently skip the whole point. Only the
  # target registry can say whether *this* version is present in it.
  local out status
  set +e
  out=$(npm publish "$abs_dir" --registry "$registry" --access public \
    --ignore-scripts --no-audit --no-fund 2>&1)
  status=$?
  set -e
  if [ $status -eq 0 ]; then
    log "published $name@$version"
    printf '%s\n' "$out" | tail -3
    return 0
  fi
  if printf '%s' "$out" | grep -qE "EPUBLISHCONFLICT|409 Conflict|already exists|previously published|cannot publish over"; then
    log "already published: $name@$version (the registry refused a second publish; skipped)"
    return 0
  fi
  log "publish failed: $name@$version"
  printf '%s\n' "$out" >&2
  return 1
}

# Credentials first: publishing may need auth before it can answer.
setup_auth "https://forgestash-old.verify.oepkgs.net/repository/npm-rv-test-local" "NODE_AUTH_TOKEN" "basic"

# Publish order: native/binary packages first, then the JS package that
# references them through optionalDependencies.
publish_one "." "bufferutil" "4.2.0" "https://forgestash-old.verify.oepkgs.net/repository/npm-rv-test-local" "generate" "" "" "" ""

log "publish finished for bufferutil@4.2.0"
