# npm-riscv64

Recipes for building riscv64 binaries of npm packages, plus the CI that turns them
into published packages.

Published packages keep the upstream API and follow npm's `optionalDependencies`
convention, so `npm install <package>` on riscv64 picks up a prebuilt binary
without a compiler and without the user noticing that anything special happened.

## How it works

```
packages/<pkg>/<version>/.stat      recipe: source, patches, build/test commands, publish targets
        │
        ├──> scripts/render.py      renders one recipe into a composite action
        │       .rendered/action/action.yml + steps/*.sh
        │
        ├──> .github/workflows/build.yml
        │       pull_request -> fetch, patch, build, test
        │       push to main -> the same, plus publish
        │
        └──> docs/recipe-spec.md    the recipe format, in full
```

* A recipe is data (`.stat`) plus shell snippets (`commands/`), with an optional
  `common/` layer shared by all versions of a package.
* Every version directory is a buildable recipe; version directories are the
  complete set, and each one must carry `.stat`.
* Building and testing run inside a riscv64 container (`docker run --platform
  linux/riscv64`), so the result is a real RISC-V object, not a cross-compiled
  guess.
* Publishing is either a native package plus a small JS package that references it
  through `optionalDependencies`, or a single package that ships the prebuild.

## Quick start

```bash
# validate every recipe and render it (no build)
python3 scripts/render.py --check-all

# render one recipe into an action plus its step scripts
python3 scripts/render.py --pkg @ruyisdk/bufferutil --ver 4.2.0 --out-dir .rendered/action

# what would CI build for the current change?
EVENT_NAME=pull_request BASE_REF=main python3 scripts/detect_targets.py
```

## Adding a package

1. `mkdir -p packages/<pkg>/<version>` (scoped packages keep the `@scope` segment:
   `packages/@scope/name/1.2.3`).
2. Write `.stat`: where the source comes from, the artifact glob, and the publish
   targets.
3. Write `commands/build` and `commands/test`. The test must prove the artifact is
   really riscv64 (`file`), not just that a build command exited zero.
4. `python3 scripts/render.py --check-all`, then
   `python3 tests/run_action_locally.py --pkg <pkg> --ver <version>`.

`docs/recipe-spec.md` documents the fields, the merge rules, the generated
fetch/patch/publish snippets, and the validation rules.

Rendered output for the reference recipe is snapshotted under
`tests/golden/bufferutil-4.2.0/`. If you change the template or the renderer and the
test fails, read the diff - when the change is intended, refresh the snapshot with
`python3 tests/regen_golden.py` and include it in the same commit.

## Verifying

| level | command | covers |
| --- | --- | --- |
| L0 | `python3 -m unittest discover -s tests` | renderer, layering, change detection, rendered YAML validity, golden snapshot of the reference recipe |
| L1 | `python3 tests/run_action_locally.py --pkg @ruyisdk/bufferutil --ver 4.2.0` | the rendered action actually fetches, builds and tests under riscv64 emulation |
| L2 | `python3 tests/l2_publish.py` | publish order A→B, idempotency, manifest merge, name identity, partial-failure re-entry (verdaccio) |
| L3 | GitHub Actions in a fork | emulated qemu, artifact reuse, real registry publishing |

L1 and L2 need docker. For L1, build the toolchain image once:

```bash
docker build -f docker/riscv64-toolchain.Dockerfile -t npm-riscv64/toolchain:riscv64 .
```

## Caching

A finished workspace tree is uploaded as an artifact keyed by a hash of the *rendered*
build commands (the fetch/patch/build steps, the patch files, the image id). A later run
with the same key restores the tree and skips fetch, patch and build - the test step
still runs, and publish works off the restored tree. A miss just builds.

Because the key describes what actually runs, it also decides what a change touches: a
template or renderer edit that adds an input, an `if:` guard or a comment changes no
command, so no recipe is rebuilt, while recipe changes always rebuild. See
`docs/recipe-spec.md` for the exact rules.

## Container images

There are two, matching the `toolchain:` value of a recipe:

| image | used by | contents |
| --- | --- | --- |
| `npm-riscv64-toolchain:riscv64` | `native` recipes (default) | riscv64 base, node, node-gyp, native build tools |
| `npm-riscv64-toolchain:cross` | `cross` recipes | amd64 base, pinned rust toolchain + `riscv64-linux-gnu` gcc, cross linker env |

A cross recipe builds in the first image's amd64 counterpart and *tests* in the
riscv64 one, so the artifact is still executed on the target CPU. Neither image is
built on every run: build them once, publish them, and let CI pull them.

```bash
# local builds (the riscv64 one needs qemu binfmt, or docker buildx --platform)
docker build --platform linux/riscv64 \
  -f docker/riscv64-toolchain.Dockerfile \
  -t ghcr.io/<owner>/npm-riscv64-toolchain:riscv64 .
docker build --platform linux/amd64 \
  -f docker/riscv64-cross.Dockerfile \
  -t ghcr.io/<owner>/npm-riscv64-toolchain:cross .

# publish them (needs a token with write:packages)
echo "$GH_TOKEN" | docker login ghcr.io -u <owner> --password-stdin
docker push ghcr.io/<owner>/npm-riscv64-toolchain:riscv64
docker push ghcr.io/<owner>/npm-riscv64-toolchain:cross
```

`ghcr.io/<owner>/...` must be lowercase, so `CannedFish` becomes `cannedfish`. The
`build toolchain images` workflow does the same on a runner
(`Actions -> build toolchain images -> Run workflow`); run it again whenever a
`docker/*.Dockerfile` changes.

CI resolves the build image as `vars.RISCV_IMAGE` when set, otherwise
`ghcr.io/${{ github.repository_owner }}/npm-riscv64-toolchain:riscv64`, and the
cross image as `vars.RISCV_CROSS_IMAGE` or (from a custom `RISCV_IMAGE`) the same
repository with the `:cross` tag. Both are pulled before the build; the cross image
only by the jobs that use it. It only authenticates when a `GHCR_TOKEN` secret is set: a public
package pulls anonymously, and logging in with the wrong token turns a public image
into `403 Forbidden` (docker never falls back to anonymous once credentials exist).
For a private package, either connect the package to the repository or add a token
with `read:packages` as `GHCR_TOKEN`. Locally, L1 works the same way: it uses the
local tag if present and pulls `RISCV_IMAGE` when it is missing.

## Running it in a fork

Publishing to GitHub Packages requires the npm scope to equal the repository owner,
so a fork does not publish `@ruyisdk/*`. Set repository variables instead of editing
recipes:

| variable | value | effect |
| --- | --- | --- |
| `PUBLISH_SCOPE` | `cannedfish` | rewrites every publish target to `@cannedfish/...` (references included) |
| `PUBLISH_REGISTRY` | `https://npm.pkg.github.com` | points every publish target at that registry |
| `SKIP_PUBLISH` | `true` | keeps the build running but skips publishing |

```bash
gh variable set PUBLISH_SCOPE --body cannedfish --repo <owner>/npm-riscv64
```

Pull requests only build and test; merging to `main` is the approval gate that also
publishes. If publishing fails, the artifact is still there - the four build steps run
before it.

## Layout

```
packages/<pkg>/<version>/   recipes (.stat, commands/, patches/)
docker/                     riscv64 toolchain image
scripts/                    renderer, change detector, recipe helpers
.github/templates/          the composite action template
.github/workflows/build.yml the single workflow for pull requests and main
docs/recipe-spec.md         recipe format specification
tests/                      L0 unit tests, L1 runner, L2 publish checks
```
