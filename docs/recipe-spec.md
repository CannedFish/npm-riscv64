# Recipe specification

A **recipe** describes how to build one version of one npm package for riscv64:
where the source comes from, which patches to apply, how to build and test it, and
which npm packages the result becomes. Everything else - the composite action, the
container invocation, the workflow matrix - is generated from the recipe.

## 1. Layout

```
packages/
  <pkg>/                      # unscoped:  packages/bufferutil
    <version>/                # scoped:    packages/@scope/name/<version>
      .stat                   # required, see §2
      commands/               # optional
        fetch  patch  build  test  publish
      patches/                # optional
        0001-*.patch
        .disable              # optional, see §4
    common/                   # optional baseline layer, never built on its own
      commands/  patches/
```

Rules:

* A directory under `packages/` is a **package**; scoped names use the npm form
  (`@scope/name`) and therefore occupy two path segments.
* Every directory inside a package - except `common` - is a **version** and must
  contain `.stat`. The set of version directories *is* the set of buildable
  recipes; a version directory without `.stat` is an error, not a draft.
* `common` is a shared baseline layer. It has no `.stat`, is never built, and is
  merged into every version of that package.

## 2. `.stat`

YAML. All keys are optional unless a rule below says otherwise.

```yaml
# Where the source comes from. Routes are tried in this priority order:
# upstream > source > npm. Each one may be omitted; at least one is required.
upstream:
  repo: https://github.com/websockets/bufferutil
  ref: v4.2.0            # must be immutable: tag name or commit sha
  subdir: ""             # optional, source root inside the checkout
source:
  url: https://example.com/pkg-1.0.0.tar.gz
  sha256: "…"            # required whenever `source` is present
npm:
  name: some-package     # tarball route; the registry metadata API is used to
  version: 1.0.0         # resolve dist.tarball and dist.integrity
  registry: https://registry.npmjs.org

artifacts:
  path: prebuilds/linux-riscv64/*.node    # glob, relative to the source root

npm_publish:
  - role: main           # main | binary
    name: "@ruyisdk/example"
    version: 1.0.0
    dir: .               # directory to publish, relative to the source root
    registry: https://npm.pkg.github.com
    token_env: NODE_AUTH_TOKEN
    generate_manifest: true
```

Validation (the renderer refuses to build otherwise):

| Rule | Why |
| --- | --- |
| `upstream.ref` must not be a moving ref (`main`, `master`, `develop`, `HEAD`, `latest`, `v1`, `v1.2`, or a branch-looking name) | a rebuild must produce the same bytes |
| `source.sha256` is mandatory when `source` is present | a mirror without a checksum is not reproducible |
| `npm_publish[].version` must equal the recipe's version directory | one version directory describes exactly one release |
| every `npm_publish` entry needs `name` and `dir` | publishing an unnamed or unrooted package is meaningless |
| `role: binary` entries need `os` and `cpu` | that is what makes npm pick the right optional dependency |
| a `requires` entry must name another `npm_publish` entry of the same recipe, at the same version | a renamed dependency must not be silently accepted |
| package directories must not be empty | an empty directory is a half-finished recipe, not a recipe |

## 3. `commands/` snippets

Five fixed names: `fetch`, `patch`, `build`, `test`, `publish`.

* `build` and `test` are **required** (from the version layer or `common`).
* `fetch`, `patch`, `publish` are **generated** when absent (see §5, §6).
* Any other file name in `commands/` is an error - a typo like `biuld` must not
  silently do nothing.
* A snippet is plain shell, run with `bash` (`set -euo pipefail` is yours to add).
  It runs either inside the riscv64 container (fetch/patch/build/test) or on the
  runner (publish), with the working directory as described in §7.

## 4. Merging `common` and `patches`

For every file name, the version layer wins:

* **same name in both** -> the version's file replaces the `common` one entirely
  (file-level replace, not a line-level merge);
* **only in `common`** -> inherited;
* **patches** -> the `common` patches are collected first, then the version
  patches, each list sorted by file name;
* **`patches/.disable`** -> one patch file name per line (`#` comments allowed);
  listed patches are dropped. This is how a version turns off an inherited patch
  without copying the whole directory;
* **dotfiles** (`.disable`, `.keep`, …) are never treated as patches.

## 5. Generated `fetch`

```bash
if fetch_upstream; then          # upstream > source > npm
  :
elif fetch_source; then
  :
elif fetch_npm; then
  :
else
  exit 1
fi
```

* `upstream` clones the repo at the pinned ref into `src/`, shallow first and with
  a full-clone + checkout fallback (a shallow clone cannot resolve a bare sha).
  `.git` is kept so `git apply --3way` stays available to the patch step.
* `source` downloads the URL, verifies `sha256` when set, and extracts with
  `--strip-components=1`.
* `npm` fetches the registry metadata for the package, reads
  `versions[<v>].dist.tarball` and `.integrity`, verifies the sha512 checksum when
  present, and extracts with `--strip-components=1`. Scoped names are
  percent-encoded (`@scope%2Fname`) - URLs are never assembled by hand.
* A route that is not configured is a stub that returns 1, so the chain has the
  same shape whether one or three routes exist.

## 6. Generated `publish`

Order: native (`role: binary`) packages first, then `main`, following `requires`
so a dependency is always published before the package that references it.

For every entry:

1. if `dir != "."`, the artifact glob (`.stat: artifacts.path`) is copied into
   `dir/` and the first copied file becomes `main` when the manifest has none;
2. the manifest is handled according to the tier:

   | tier | condition | behaviour |
   | --- | --- | --- |
   | custom | `commands/publish` exists | the renderer gets out of the way |
   | generate | `generate_manifest: true` | create the manifest when it is missing, update it when it exists: `name` and `version` come from `.stat`, every other field is preserved |
   | verify | `generate_manifest` absent/false | the manifest must exist and its `name`/`version` must match `.stat`; a mismatch is a hard failure |

3. idempotency: `npm view <name>@<version>` - already present means "skip";
4. credentials: `token_env` names an environment variable; when it is set, a
   `//<host>/:_authToken=${TOKEN_ENV}` line is written to a throwaway `.npmrc`,
   before the idempotency probe (a private registry needs auth even to answer it);
5. `npm publish <dir> --registry <registry> --access public --ignore-scripts`.

`name` is identity: with `verify`, renaming a package is refused rather than
published under a second name. With `generate_manifest: true`, a rename is a
deliberate re-brand and is allowed.

## 7. What the renderer generates

```bash
python3 scripts/render.py --pkg @ruyisdk/bufferutil --ver 4.2.0 --out-dir .rendered/action
python3 scripts/render.py --check-all          # every recipe, YAML validated
python3 scripts/render.py --pkg … --ver … --out .rendered/action/action.yml
```

`--out-dir` writes `action.yml` plus `steps/{fetch,patch,build,test,publish}.sh`.
Wrapping shell in YAML by hand is where quoting bugs live, so the scripts stay
separate files and the action only calls them.

Generated variables available in the template: `pkg.name`, `pkg.dirname`,
`pkg.full`, `pkg.version`, `pkg.ident`, `pkg.slug`, `pkg.artifact`,
`pkg.target`, `recipe.dir`, `src.dir`, `steps.dir`, `has.patches`, plus every
scalar from `.stat` flattened with dots (`upstream.repo`, `upstream.subdir`,
`npm_publish.0.registry`, …).

Template features:

* `{{key}}` substitution, indentation aware: a multi-line value gets the current
  indentation prefixed to every continuation line, so block scalars stay valid;
* `{{#if key}} … {{/if}}` and `{{#unless key}} … {{/unless}}` blocks (nestable) so
  optional steps disappear instead of becoming empty `run:` blocks;
* an unresolved placeholder is an error, not an empty string.

`--steps-dir` overrides the workspace-relative directory written into the action,
so the same render can be written anywhere (that is how the golden fixtures stay
stable).

`--override key=value` (repeatable, dotted paths, list indices as numbers) patches
`.stat` at render time. It exists for local runs, e.g.
`--override npm_publish.0.registry=http://127.0.0.1:4873`; promoting a recipe to a
different registry never requires editing it.

Two dedicated flags cover the common fork cases:

* `--publish-scope cannedfish` rewrites the npm scope of every publish target (and
  of the `requires` references between them) to `@cannedfish/...`. GitHub Packages
  only accepts packages whose scope equals the repository owner.
* `--publish-registry URL` points every publish target at one registry.

The rendered action is validated before it is written: YAML must parse,
`runs.using` must be `composite`, every step must have `shell` and a non-empty
`run`, and the fetch/build/test/publish steps must all be present.

### Container user

Every step that touches the workspace runs its container as **the calling user**:

```bash
docker run --rm --platform linux/riscv64 \
  --user "${RISCV_DOCKER_USER:-$(id -u):$(id -g)}" \
  -e HOME=/w/.container-home -e npm_config_cache=/w/.container-home/.npm \
  ... -v "$GITHUB_WORKSPACE:/w" ...
```

Running as root leaves root-owned files in the workspace, and the publish step - which
runs on the host, outside emulation - then has to write a manifest it does not own. On
a GitHub runner the step user is `runner`, so the mismatch surfaced as
`EACCES: permission denied, open 'src/package.json'`.

HOME and the npm cache point inside the workspace because the container user has no
writable home of its own. `RISCV_DOCKER_USER` overrides the mapping.

### Quoting

`@` is a YAML reserved indicator character. Any template scalar that *starts*
with a variable must be quoted:

```yaml
name: "build {{pkg.ident}} (riscv64)"     # correct
name: build {{pkg.ident}}                 # parses as a tag, fails
```

The renderer catches this by parsing its own output.

## 8. Change detection

`scripts/detect_targets.py` decides what to build, from environment variables so
the same script serves both triggers:

| change | selection |
| --- | --- |
| `packages/<pkg>/<ver>/**` | that version |
| `packages/<pkg>/common/**` | every version of that package |
| `.github/templates/action.template.yml` | every recipe (the container and step order changed) |
| `scripts/**` | no build; renderer self-check (`--check-all`) |
| anything else | nothing |

Pull requests use a three-dot diff against the merge base, pushes a two-dot diff;
an all-zero `before` (new branch, force push) rebuilds everything. A selected
version directory without `.stat` fails the run before any build starts.

## 9. Build cache

Rebuilding the same recipe with the same inputs produces the same bytes, so a
finished workspace tree is cached as an artifact and restored instead of rebuilt.

Key (`scripts/build_key.py`) - every input that can change the artifact:

* the version directory (`.stat`, `commands/`, `patches/`),
* the package's `common/` layer,
* `.github/templates/`, `scripts/`, `docker/`,
* the resolved container image id.

Nothing else can influence the output, so a hit cannot be stale: no diff against the
source branch is needed, and the same key is produced by a pull request and by the
merge commit that follows it.

Cache unit is the **tree**, not just the artifact: publish needs the source tree and
the test needs `node_modules` to resolve the prebuild like a consumer would. `.git` is
excluded. Artifact name: `<slug>-<ver>-riscv64-tree-<key>` (e.g.
`ruyisdk-bufferutil-4.2.0-riscv64-tree-77f1d80d3571`), retention 90 days.

On a hit the action receives `reuse: true`, which skips fetch, patch and build. The
**test step still runs** - a restored artifact is validated like a fresh one - and
publish runs normally. Any failed lookup (no artifact, expired, API error) falls back
to a full build.

## 10. Verifying locally

| level | command | what it proves |
| --- | --- | --- |
| L0 | `python3 -m unittest discover -s tests` | renderer, layering, detection, generated YAML validity, golden snapshot (`tests/golden/`, refresh with `python3 tests/regen_golden.py`) |
| L1 | `python3 tests/run_action_locally.py --pkg @ruyisdk/bufferutil --ver 4.2.0` | the rendered action really fetches, builds and tests under riscv64 emulation |
| L2 | `python3 tests/l2_publish.py` | publish order, idempotency, manifest merge, identity, partial-failure re-entry against verdaccio |

L1 and L2 need docker. L1 uses the riscv64 toolchain image: it takes a local image
when one exists and otherwise pulls `RISCV_IMAGE`. In CI the image is published once
by the `toolchain-image` workflow and pulled per run - building it per run costs
minutes of emulated package installs and can silently succeed without producing a
local image. Set `RISCV_IMAGE` to override the image and `NPM_REGISTRY` to point
publishing at a local registry. L3 (real GitHub Actions) runs in a fork; there GitHub Packages
requires the npm scope to match the repository owner, which `--override` covers
without touching the recipes.
