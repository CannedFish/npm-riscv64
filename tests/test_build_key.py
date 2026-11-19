"""Build cache key tests (L0).

The key decides whether a previous tree is restored instead of rebuilding, so it
has to be both stable (same inputs, same key) and sensitive (any input change, new
key). Both directions are tested, plus the property that unrelated recipes do not
invalidate a key.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_key  # noqa: E402

STAT = """\
upstream:
  repo: https://github.com/example/demo
  ref: v1.0.0

artifacts:
  path: build/Release/*.node

npm_publish:
  - role: main
    name: "@demo/widget"
    version: 1.0.0
    dir: .
    generate_manifest: true
"""


class CacheRepo:
    """A miniature repository: recipes plus the shared inputs a key hashes."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, relative: str, content: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def recipe(self, pkg: str, ver: str = "1.0.0", name: str = "@demo/widget") -> None:
        base = f"packages/{pkg}/{ver}"
        self.write(f"{base}/.stat", STAT.replace("@demo/widget", name))
        self.write(f"{base}/commands/build", "npm install --ignore-scripts\n")
        self.write(f"{base}/commands/test", "file build/Release/*.node\n")

    def scaffold(self) -> None:
        self.write(".github/templates/action.template.yml", "name: demo\n")
        self.write("scripts/render.py", "print('render')\n")
        self.write("docker/toolchain.Dockerfile", "FROM debian:trixie-slim\n")


class BuildKeyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = CacheRepo(Path(self._tmp.name))
        self.repo.scaffold()
        self.repo.recipe("demo")

    def tearDown(self):
        self._tmp.cleanup()

    def key(self, pkg: str = "demo", **kwargs) -> str:
        return build_key.build_key(self.repo.root, pkg, "1.0.0", **kwargs)

    def test_same_inputs_give_the_same_key(self):
        self.assertEqual(self.key(), self.key())

    def test_key_is_short_hex(self):
        value = self.key()
        self.assertEqual(len(value), 12)
        int(value, 16)

    def test_recipe_snippet_change_invalidates(self):
        before = self.key()
        self.repo.write("packages/demo/1.0.0/commands/build", "node-gyp rebuild\n")
        self.assertNotEqual(before, self.key())

    def test_stat_change_invalidates(self):
        before = self.key()
        self.repo.write("packages/demo/1.0.0/.stat", STAT.replace("v1.0.0", "v1.0.1"))
        self.assertNotEqual(before, self.key())

    def test_common_layer_change_invalidates(self):
        before = self.key()
        # the shared baseline sits next to the version directories
        self.repo.write("packages/demo/common/commands/build", "make\n")
        self.assertNotEqual(before, self.key())

    def test_template_change_invalidates(self):
        before = self.key()
        self.repo.write(".github/templates/action.template.yml", "name: changed\n")
        self.assertNotEqual(before, self.key())

    def test_renderer_change_invalidates(self):
        before = self.key()
        self.repo.write("scripts/render.py", "print('changed')\n")
        self.assertNotEqual(before, self.key())

    def test_toolchain_change_invalidates(self):
        before = self.key()
        self.repo.write("docker/toolchain.Dockerfile", "FROM debian:bookworm-slim\n")
        self.assertNotEqual(before, self.key())

    def test_image_id_invalidates(self):
        before = self.key(image_id="sha256:aaaa")
        self.assertNotEqual(before, self.key(image_id="sha256:bbbb"))
        self.assertEqual(self.key(image_id="sha256:aaaa"), before)

    def test_extra_input_invalidates(self):
        before = self.key()
        self.assertNotEqual(before, self.key(extra="--publish-scope demo"))

    def test_other_recipe_does_not_invalidate(self):
        before = self.key()
        self.repo.recipe("other", name="@demo/other")
        self.assertEqual(before, self.key())

    def test_missing_recipe_is_an_error(self):
        with self.assertRaises(build_key.rl.RecipeError):
            self.key(pkg="absent")

    def test_inputs_are_sorted_and_cover_the_shared_files(self):
        inputs = [
            path.relative_to(self.repo.root).as_posix()
            for path in build_key.collect_inputs(self.repo.root, "demo", "1.0.0")
        ]
        self.assertEqual(inputs, sorted(inputs))
        self.assertIn("packages/demo/1.0.0/commands/build", inputs)
        self.assertIn(".github/templates/action.template.yml", inputs)
        self.assertIn("scripts/render.py", inputs)
        self.assertIn("docker/toolchain.Dockerfile", inputs)

    def test_pycache_is_ignored(self):
        before = self.key()
        self.repo.write("scripts/__pycache__/render.cpython-314.pyc", "junk")
        self.assertEqual(before, self.key())

    def test_cli_prints_a_key(self):
        self.assertEqual(build_key.main([
            "--pkg", "demo",
            "--ver", "1.0.0",
            "--root", str(self.repo.root),
        ]), 0)

    def test_cli_reports_a_missing_recipe(self):
        self.assertEqual(build_key.main([
            "--pkg", "absent",
            "--ver", "1.0.0",
            "--root", str(self.repo.root),
        ]), 1)


class RealRepositoryKeyTests(unittest.TestCase):
    """The key of the shipped recipe must track the real files."""

    def test_bufferutil_key_is_stable(self):
        first = build_key.build_key(REPO_ROOT, "@ruyisdk/bufferutil", "4.2.0")
        second = build_key.build_key(REPO_ROOT, "@ruyisdk/bufferutil", "4.2.0")
        self.assertEqual(first, second)

    def test_bufferutil_inputs_include_the_recipe(self):
        inputs = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in build_key.collect_inputs(REPO_ROOT, "@ruyisdk/bufferutil", "4.2.0")
        ]
        self.assertIn("packages/@ruyisdk/bufferutil/4.2.0/.stat", inputs)
        self.assertIn("packages/@ruyisdk/bufferutil/4.2.0/commands/build", inputs)
        self.assertIn(".github/templates/action.template.yml", inputs)


if __name__ == "__main__":
    unittest.main()
