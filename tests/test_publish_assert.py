#!/usr/bin/env python3
"""Tests for the served-artifact assertion (`manifest.mjs --assert-served`).

The publish step used to be green on two very different outcomes: "the registry
stored this artifact" and "the registry refused and already serves something else".
The second case is how a mirror that proxies npmjs silently kept serving upstream's
package without the riscv64 increment, so the step now verifies what the registry
actually serves. These tests pin that check with a stub npm on PATH, which is the
only way to exercise both branches without a real registry.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "publish_manifest.mjs"

STUB = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  pack)
    # npm <=10 prints an array, npm >=11 an object keyed by name; the helper reads both.
    printf '%s' "$STUB_PACK_JSON"
    ;;
  view)
    printf '%s' "$STUB_VIEW_JSON"
    ;;
  *)
    echo "stub npm: unexpected command $1" >&2
    exit 1
    ;;
esac
"""


class AssertServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="dsh-assert-served-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg_dir = self.tmp / "pkg"
        self.pkg_dir.mkdir()
        (self.pkg_dir / "package.json").write_text(
            json.dumps({"name": "example", "version": "1.0.0"}, indent=2) + "\n"
        )
        self.stub_dir = self.tmp / "stub"
        self.stub_dir.mkdir()
        stub = self.stub_dir / "npm"
        stub.write_text(STUB)
        stub.chmod(0o755)

    def spec(self, optional=None):
        return {
            "name": "example",
            "version": "1.0.0",
            "registry": "https://registry.example.test/",
            "dir": ".",
            "kind": "source-tree",
            "manifest": "upstream",
            "optional": optional or {},
        }

    def run_helper(self, pack_json, view_json, optional=None):
        env = dict(os.environ)
        env.pop("npm_execpath", None)
        env["PATH"] = f"{self.stub_dir}:{env.get('PATH', '')}"
        env["STUB_PACK_JSON"] = pack_json
        env["STUB_VIEW_JSON"] = view_json
        return subprocess.run(
            ["node", str(HELPER), "--assert-served",
             json.dumps(self.spec(optional)), str(self.pkg_dir)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.tmp),
        )

    def test_identical_artifact_passes(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours, "files": []}]),
            json.dumps({"dist": {"integrity": ours}}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the registry serves this artifact", result.stdout)

    def test_object_shaped_pack_output_is_read_too(self):
        # npm >= 11 prints {"<name>": {...}} instead of [{...}]
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps({"example": {"integrity": ours, "files": []}}),
            json.dumps({"dist": {"integrity": ours}}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_different_artifact_fails_and_names_both(self):
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps({"dist": {"integrity": "sha512-theirs=="}}),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("the registry does not serve this artifact", result.stderr)
        self.assertIn("sha512-ours==", result.stderr)
        self.assertIn("sha512-theirs==", result.stderr)
        # The message has to say what to do, because this is an operator decision.
        self.assertIn("enable redeploy", result.stderr)

    def test_a_missing_increment_fails(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours}]),
            json.dumps({"dist": {"integrity": ours}, "optionalDependencies": {}}),
            optional={"@scope/platform": "1.0.0"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("@scope/platform@1.0.0", result.stderr)
        self.assertIn("would not pull the riscv64 build", result.stderr)

    def test_a_present_increment_passes(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours}]),
            json.dumps(
                {
                    "dist": {"integrity": ours},
                    "optionalDependencies": {"@scope/platform": "1.0.0"},
                }
            ),
            optional={"@scope/platform": "1.0.0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unreadable_registry_answer_fails(self):
        # Silence here would turn "could not check" into "checked and fine".
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            "not json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read what example@1.0.0 serves", result.stderr)


if __name__ == "__main__":
    unittest.main()
