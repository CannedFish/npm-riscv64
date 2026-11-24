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
//
// The spec mirrors one npm_publish entry after the renderer resolved it:
//   { name, version, registry, dir, kind, manifest, base?, overlay?, allow_overwrite?,
//     optional?, os?, cpu?, libc?, main?, engines?, manifest_json?, readme_text? }
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

const spec = mode === "--shell" || mode === "--verify-integrity" || mode === "--prepare"
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
    const before = Object.keys(pkg.optionalDependencies || {}).length;
    pkg.optionalDependencies = { ...(pkg.optionalDependencies || {}) };
    for (const [depName, depVersion] of Object.entries(spec.optional)) {
      pkg.optionalDependencies[depName] = depVersion;
      changes.push("optionalDependencies " + depName + "@" + depVersion);
    }
    // Adding a platform must never drop the platforms upstream already ships.
    const after = Object.keys(pkg.optionalDependencies).length;
    if (after <= before) {
      fail(
        "optionalDependencies did not grow (" + before + " before, " + after + " after); " +
          "refusing to publish a package that may have lost upstream platforms"
      );
    }
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
