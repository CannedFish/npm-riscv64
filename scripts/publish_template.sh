set -euo pipefail

# Publishing runs on the host: moving files and talking to a registry does not need
# riscv64 emulation, and it keeps token handling straightforward.
REPO_ROOT="$(pwd)"
SRC_DIR="${SRC_DIR:-src}"
# The manifest helper travels next to this script, so no path comes from the workflow.
STEPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export NPM_CONFIG_USERCONFIG="$REPO_ROOT/.npmrc.publish"
: > "$NPM_CONFIG_USERCONFIG"

log() { echo "[publish] $*"; }

# Temporary package trees go away however the step ends.
WORK_DIRS=()
cleanup() {
  local dir
  for dir in "${WORK_DIRS[@]}"; do
    if [ -n "$dir" ]; then
      rm -rf "$dir"
    fi
  done
}
trap cleanup EXIT

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

publish_pkg() {
  # Reads one publish spec (JSON) from stdin, prepares the package directory and
  # publishes it. See generate_publish in scripts/render.py for the two kinds:
  #
  #   source-tree       the directory the build produced, inside $SRC_DIR
  #   upstream-tarball  the upstream npm package, verified against base.integrity and
  #                     extended with the recipe's overlay, so the published package
  #                     keeps upstream's structure and gains only our riscv64 files
  local spec_json assignments
  spec_json="$(cat)"
  # A failing helper must stop the step: eval of an empty string would succeed.
  assignments="$(node "$STEPS_DIR/manifest.mjs" --shell "$spec_json")" || return 1
  eval "$assignments"
  log "package $P_NAME@$P_VERSION (kind=$P_KIND, manifest=$P_MANIFEST)"

  local work_dir publish_dir
  work_dir="$(mktemp -d "$REPO_ROOT/.publish-work.XXXXXX")"
  WORK_DIRS+=("$work_dir")

  if [ "$P_KIND" = "upstream-tarball" ]; then
    [ -n "$P_BASE_URL" ] && [ -n "$P_BASE_INTEGRITY" ] || {
      log "upstream-tarball needs base.url and base.integrity"
      return 1
    }
    curl -sSfL --retry 3 -o "$work_dir/base.tgz" "$P_BASE_URL" || {
      log "cannot fetch $P_BASE_URL"
      return 1
    }
    node "$STEPS_DIR/manifest.mjs" --verify-integrity "$spec_json" "$work_dir/base.tgz" ||
      return 1
    mkdir -p "$work_dir/pkg"
    tar xzf "$work_dir/base.tgz" -C "$work_dir/pkg" --strip-components=1 || {
      log "cannot unpack $P_BASE_URL"
      return 1
    }
    local rel src_rel dst_rel
    for rel in $P_OVERLAY; do
      src_rel="${rel%%:*}"
      dst_rel="${rel#*:}"
      [ "$dst_rel" = "$rel" ] && dst_rel="$src_rel"
      [ -e "$SRC_DIR/$src_rel" ] || {
        log "overlay source missing: $SRC_DIR/$src_rel"
        return 1
      }
      if [ -e "$work_dir/pkg/$dst_rel" ]; then
        log "refusing to overwrite an upstream file with the overlay: $dst_rel"
        return 1
      fi
      mkdir -p "$(dirname "$work_dir/pkg/$dst_rel")"
      cp -a "$SRC_DIR/$src_rel" "$work_dir/pkg/$dst_rel"
      log "overlay $src_rel -> $dst_rel"
    done
    publish_dir="$work_dir/pkg"
  else
    publish_dir="$SRC_DIR/$P_DIR"
    [ -d "$publish_dir" ] || {
      log "missing package directory: $publish_dir"
      return 1
    }
  fi

  node "$STEPS_DIR/manifest.mjs" --prepare "$spec_json" "$publish_dir" || return 1

  # Always publish an absolute path: npm reads a two-segment relative path such as
  # "src/." as a github shorthand spec, not as a directory.
  local abs_dir
  abs_dir="$(cd "$publish_dir" && pwd)" || {
    log "cannot enter $publish_dir"
    return 1
  }

  # Publish, then read the registry answer instead of trusting a lookup: a registry
  # that proxies or groups another one also serves upstream versions of the same
  # name, so "npm view <name>@<version>" would call our own package published and
  # silently skip the point. Only the target registry knows about its own content.
  #
  # The output is streamed rather than captured: a slow upload otherwise looks frozen
  # in the log until npm exits, which is exactly how a stuck publish reads.
  local out_file status
  out_file="$(mktemp)"
  set +e
  npm publish "$abs_dir" --registry "$P_REGISTRY" --access public \
    --ignore-scripts --no-audit --no-fund --loglevel http 2>&1 |
    tee "$out_file"
  status=${PIPESTATUS[0]}
  set -e
  if [ $status -eq 0 ]; then
    rm -f "$out_file"
    log "published $P_NAME@$P_VERSION"
    return 0
  fi
  if grep -qE "EPUBLISHCONFLICT|409 Conflict|already exists|previously published|cannot publish over" "$out_file"; then
    rm -f "$out_file"
    log "already published: $P_NAME@$P_VERSION (the registry refused a second publish; skipped)"
    return 0
  fi
  log "publish failed: $P_NAME@$P_VERSION"
  printf '%s\n' "$out_file" >&2
  log "registry answer is in $out_file"
  return 1
}

# Credentials first: publishing may need auth before it can answer.
@@AUTH@@

# Publish order: platform packages first, then the package that references them
# through optionalDependencies.
@@SPECS@@

log "publish finished for @@IDENT@@"
