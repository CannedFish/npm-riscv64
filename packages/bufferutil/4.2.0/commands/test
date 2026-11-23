#!/usr/bin/env bash
# Verifies the bufferutil prebuild the way a consumer will use it: no build/
# directory, node-gyp-build must resolve the riscv64 prebuild, and the mask API
# must round-trip on this architecture.
set -euo pipefail

artifact="prebuilds/linux-riscv64/bufferutil.node"

if [ ! -f "$artifact" ]; then
  echo "ERROR: missing artifact $artifact" >&2
  exit 1
fi
echo "artifact: $artifact"
file "$artifact"
if ! file "$artifact" | grep -Eqi 'risc-?v'; then
  echo "ERROR: artifact is not a RISC-V object" >&2
  exit 1
fi

# node-gyp-build prefers build/Release when present, so hide it: a package
# installed from the registry only ships prebuilds/.
if [ -d build ]; then
  mv build build.hidden
  trap 'mv build.hidden build' EXIT
fi

node - <<'EOF'
const assert = require('node:assert');
const resolved = require('node-gyp-build').path(__dirname);
assert.ok(
  resolved.includes('prebuilds/linux-riscv64'),
  `expected the riscv64 prebuild, got: ${resolved}`
);

const { mask, unmask } = require(resolved);
const payload = Buffer.from('ruyi-riscv64-bufferutil');
const key = Buffer.from('0123456789abcdef');
const masked = Buffer.alloc(payload.length);

mask(payload, key, masked, 0, payload.length);
assert.notDeepStrictEqual(masked, payload, 'mask must change the payload');
unmask(masked, key);
assert.deepStrictEqual(masked, payload, 'unmask must restore the payload');

console.log(`bufferutil ok: ${resolved} round-tripped ${payload.length} bytes`);
EOF
