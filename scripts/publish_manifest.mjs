#!/usr/bin/env node
// Prepares the manifest that gets published, and nothing else.
//
// This lives in its own file instead of being embedded in a `node -e '...'` string:
// quoting a JavaScript literal inside a shell script had become a recurring source
// of bugs (one apostrophe in a message broke the whole step). The publish script
// still owns downloading, unpacking, overlaying and calling npm.
//
// Every mode takes the publish spec the renderer generated, as JSON on argv:
//
//   --shell <spec>                        P_* shell assignments for the caller
//   --verify-integrity <spec> <tarball>   compare sha512 with base.integrity
//   --prepare <spec> <dir>                write <dir>/package.json (+ README.md)
//   --assert-served <spec> <dir>          fail unless the registry serves exactly these bytes
//
// The spec mirrors one npm_publish entry after the renderer resolved it:
//   { name, version, registry, dir, kind, manifest, base?, overlay?, allow_overwrite?,
//     optional?, os?, cpu?, libc?, main?, engines?, manifest_json?, readme_text? }
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const argv = process.argv.slice(2);
const mode = argv[0];

function fail(message) {
  console.error("[publish] " + message);
  process.exit(1);
}

function readSpec(value) {
  // The publish script feeds the spec inline (a quoted heredoc); a path is read from
  // disk, which keeps longer specs reviewable in tests.
  const raw = String(value ?? "").trim();
  const text = raw.startsWith("{") ? raw : readFileSync(raw, "utf8");
  try {
    return JSON.parse(text);
  } catch (error) {
    fail("bad publish spec: " + error.message);
  }
}

function shellQuote(value) {
  return "'" + String(value ?? "").replace(/'/g, "'\\''") + "'";
}

function sameRegistry(left, right) {
  const trim = (value) => String(value ?? "").replace(/\/+$/, "");
  return trim(left) === trim(right);
}

const spec =
  mode === "--shell" || mode === "--verify-integrity" || mode === "--prepare" ||
  mode === "--assert-served"
    ? readSpec(argv[1])
    : fail("unknown mode: " + mode);

if (mode === "--shell") {
  // Only what the shell itself needs; the manifest work stays here.
  const scalars = {
    NAME: spec.name,
    VERSION: spec.version,
    REGISTRY: spec.registry,
    KIND: spec.kind || "source-tree",
    DIR: spec.dir || ".",
    MANIFEST: spec.manifest || "generated",
    BASE_URL: (spec.base || {}).url || "",
    BASE_INTEGRITY: (spec.base || {}).integrity || "",
    // Space separated: overlay paths are validated to contain no whitespace.
    OVERLAY: (spec.overlay || []).join(" "),
    ALLOW_OVERWRITE: spec.allow_overwrite ? "true" : "",
  };
  for (const [key, value] of Object.entries(scalars)) {
    console.log("P_" + key + "=" + shellQuote(value));
  }
  process.exit(0);
}

if (mode === "--verify-integrity") {
  const tarball = argv[2];
  const expected = (spec.base || {}).integrity || "";
  if (!expected) {
    fail("the publish spec has no base.integrity; refusing to publish an unverified tarball");
  }
  if (!existsSync(tarball)) fail("base tarball is missing: " + tarball);
  const digest =
    "sha512-" + createHash("sha512").update(readFileSync(tarball)).digest("base64");
  if (digest !== expected) {
    fail("base tarball integrity mismatch: expected " + expected + " but got " + digest);
  }
  console.log("[publish] base tarball verified (" + expected.slice(0, 19) + "...)");
  process.exit(0);
}

if (mode === "--prepare") {
  const dir = argv[2];
  const manifestMode = spec.manifest || "generated";
  const file = join(dir, "package.json");

  let pkg;
  if (manifestMode === "template") {
    if (!spec.manifest_json) fail("manifest=template without manifest_json");
    pkg = { ...spec.manifest_json };
  } else if (existsSync(file)) {
    pkg = JSON.parse(readFileSync(file, "utf8"));
  } else if (manifestMode === "upstream") {
    fail("missing manifest " + file + "; manifest=upstream publishes the base package as it is");
  } else {
    pkg = {};
  }

  // Name and version are asserted in every mode; only the generated and template
  // shapes are allowed to write them.
  if (manifestMode !== "upstream") {
    if (pkg.name !== spec.name) {
      console.log("[publish] name " + (pkg.name || "(none)") + " -> " + spec.name);
    }
    pkg.name = spec.name;
    pkg.version = spec.version;
  } else {
    if (pkg.name !== spec.name) {
      fail("the base package is " + pkg.name + ", the recipe publishes " + spec.name);
    }
    if (String(pkg.version) !== String(spec.version)) {
      fail("the base package is " + pkg.version + ", the recipe publishes " + spec.version);
    }
  }

  const changes = [];
  if (manifestMode !== "upstream") {
    if (spec.main && pkg.main !== spec.main) {
      changes.push("main " + spec.main);
    }
    if (spec.main) pkg.main = spec.main;
    if (spec.os) pkg.os = [spec.os];
    if (spec.cpu) pkg.cpu = [spec.cpu];
    if (spec.libc) pkg.libc = [spec.libc];
    if (spec.engines) pkg.engines = spec.engines;
  }

  const hasOptional = Object.keys(spec.optional || {}).length > 0;
  if (hasOptional) {
    const upstreamDeps = { ...(pkg.optionalDependencies || {}) };
    const upstreamNames = Object.keys(upstreamDeps);
    pkg.optionalDependencies = { ...upstreamDeps };
    let added = 0;
    for (const [depName, depVersion] of Object.entries(spec.optional)) {
      const previous = pkg.optionalDependencies[depName];
      if (previous === depVersion) continue;   // already merged by an earlier run
      pkg.optionalDependencies[depName] = depVersion;
      changes.push("optionalDependencies " + depName + "@" + depVersion);
      if (previous === undefined) added += 1;
    }
    // The merge is additive and idempotent: no platform upstream lists may disappear,
    // and nothing but the packages this recipe publishes may appear. "Count must grow"
    // would have refused the second run of a recipe that publishes out of the build
    // tree, because the first run already wrote the entry into that manifest.
    for (const name of upstreamNames) {
      if (pkg.optionalDependencies[name] === undefined) {
        fail("optionalDependencies lost " + name + ", a platform the base package ships");
      }
    }
    const extra = Object.keys(pkg.optionalDependencies).filter(
      (name) => !upstreamNames.includes(name) && !(name in spec.optional)
    );
    if (extra.length) {
      fail("optionalDependencies gained " + extra.join(", ") + ", which the recipe does not publish");
    }
    console.log(
      added
        ? "[publish] added " + added + " platform package(s) to optionalDependencies"
        : "[publish] optionalDependencies already list " + Object.keys(spec.optional).join(", ")
    );
  }

  // npm gives publishConfig.registry precedence over --registry, so a base package
  // that pins its own registry would send the tarball somewhere else (D13).
  if (spec.registry) {
    pkg.publishConfig = { ...(pkg.publishConfig || {}) };
    if (!pkg.publishConfig.access) pkg.publishConfig.access = "public";
    if (!sameRegistry(pkg.publishConfig.registry, spec.registry)) {
      changes.push(
        "publishConfig.registry " +
          (pkg.publishConfig.registry || "(none)") +
          " -> " +
          spec.registry
      );
    }
    pkg.publishConfig.registry = spec.registry;
  }

  if (spec.readme_text) {
    const readme = join(dir, "README.md");
    if (!existsSync(readme)) {
      writeFileSync(readme, spec.readme_text);
      changes.push("README.md written");
    }
  }

  writeFileSync(file, JSON.stringify(pkg, null, 2) + "\n");
  console.log(
    "[publish] " + file + " (" + manifestMode + ") " +
      (changes.length ? "changed: " + changes.join("; ") : "unchanged")
  );
}

// npm itself is used here, not a registry client: the tarball npm would upload and the
// credential the step configured are both npm's, so a second implementation could
// disagree with them and report a false conflict.
function runNpm(args, cwd) {
  // npm runs as a node script when npm_execpath is set (the usual case inside a step).
  const command = process.env.npm_execpath
    ? [process.execPath, process.env.npm_execpath]
    : ["npm"];
  const [cmd, ...prefix] = command;
  const result = spawnSync(cmd, [...prefix, ...args], {
    cwd,
    encoding: "utf8",
    env: process.env,
  });
  return { status: result.status, stdout: result.stdout || "", stderr: result.stderr || "" };
}

// dry-run packs without writing the tarball, and reports the digest of the bytes npm
// would upload: the same code path `npm publish` uses for the directory.
function packedIntegrity(dir) {
  const result = runNpm(
    ["pack", "--dry-run", "--json", "--ignore-scripts", dir],
    dir,
  );
  if (result.status !== 0) {
    return { error: result.stderr.trim() || "npm pack failed" };
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch (error) {
    return { error: "cannot read npm pack output: " + error.message };
  }
  // npm <=10 prints an array, npm >=11 an object keyed by package name.
  const entry = Array.isArray(parsed) ? parsed[0] : Object.values(parsed)[0];
  if (!entry || !entry.integrity) {
    return { error: "npm pack reported no integrity" };
  }
  return { integrity: entry.integrity, files: entry.files || [] };
}

function servedManifest() {
  const result = runNpm(
    ["view", spec.name + "@" + spec.version, "--json", "--registry", spec.registry],
    null,
  );
  if (result.status !== 0) {
    return { error: result.stderr.trim() || "npm view failed" };
  }
  try {
    return { manifest: JSON.parse(result.stdout) };
  } catch (error) {
    return { error: "cannot read npm view output: " + error.message };
  }
}

if (mode === "--assert-served") {
  // A published package is a claim about what consumers install. The registry answer
  // to `npm publish` says the upload was accepted; this says the registry now serves
  // *these* bytes. They are not the same statement: a repository that proxies npmjs
  // already serves upstream's copy of the version being republished, so a refused
  // publish used to be logged as "already published" while the mirror kept serving
  // upstream's package - green step, wrong package, riscv64 support missing.
  const dir = argv[2];
  const ours = packedIntegrity(dir);
  if (ours.error) fail("cannot compute what this publish would upload: " + ours.error);
  const theirs = servedManifest();
  if (theirs.error) {
    fail(
      "cannot read what " + spec.name + "@" + spec.version + " serves at " +
        spec.registry + ": " + theirs.error
    );
  }
  const served = theirs.manifest || {};
  const servedIntegrity = (served.dist || {}).integrity || "";
  if (servedIntegrity !== ours.integrity) {
    fail(
      "the registry does not serve this artifact:\n" +
        "  this publish:  " + ours.integrity + "\n" +
        "  it serves:     " + (servedIntegrity || "(no integrity)") + "\n" +
        "that version holds something else - upstream's copy, or an earlier publish of " +
        "ours - and the registry does not let a publish replace it. Nothing was " +
        "delivered: enable redeploy (or delete that version) on the registry, or give " +
        "the recipe a version it can own."
    );
  }
  const expected = spec.optional || {};
  for (const [depName, depVersion] of Object.entries(expected)) {
    const actual = (served.optionalDependencies || {})[depName];
    if (actual !== depVersion) {
      fail(
        "the served manifest does not reference " + depName + "@" + depVersion +
          " (" + (actual ? "it lists " + actual : "it has no such entry") + "); " +
          "installing this package would not pull the riscv64 build"
      );
    }
  }
  console.log(
    "[publish] the registry serves this artifact (" + ours.integrity.slice(0, 19) + "...)"
  );
}
