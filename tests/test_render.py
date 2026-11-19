"""Renderer tests (L0: static/unit level).

The fixtures are built in a temporary directory instead of being committed:
each test states the recipe it needs, which keeps intent visible and avoids a
second copy of the recipe format drifting away from docs/recipe-spec.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import recipe_lib as rl  # noqa: E402
import render  # noqa: E402

REAL_TEMPLATE = REPO_ROOT / ".github/templates/action.template.yml"

STAT_TEMPLATE = """\
upstream:
  repo: https://github.com/example/demo
  ref: v{ver}

artifacts:
  path: build/Release/*.node

npm_publish:
  - role: main
    name: "{name}"
    version: {ver}
    dir: .
    generate_manifest: true
"""


class TempRepo:
    """Helper that materialises a small recipe tree on disk."""

    def __init__(self, root: Path):
        self.root = root
        self.packages = root / "packages"

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def add_recipe(
        self,
        pkg: str,
        ver: str,
        *,
        stat: str | None = None,
        build: str | None = "echo build",
        test: str | None = "echo test",
        patches: dict[str, str] | None = None,
        disable: list[str] | None = None,
        common_build: str | None = None,
        common_patches: dict[str, str] | None = None,
    ) -> Path:
        pkg_dir = self.packages.joinpath(*pkg.split("/"))
        recipe = pkg_dir / ver
        recipe.mkdir(parents=True, exist_ok=True)
        if stat is not False:
            (recipe / rl.STAT_FILE).write_text(
                textwrap.dedent(stat or STAT_TEMPLATE.format(ver=ver, name=f"@demo/{pkg.split('/')[-1]}")),
                encoding="utf-8",
            )
        if build is not None:
            (recipe / "commands").mkdir(exist_ok=True)
            (recipe / "commands" / "build").write_text(build + "\n", encoding="utf-8")
        if test is not None:
            (recipe / "commands").mkdir(exist_ok=True)
            (recipe / "commands" / "test").write_text(test + "\n", encoding="utf-8")
        for name, content in (patches or {}).items():
            (recipe / "patches").mkdir(exist_ok=True)
            (recipe / "patches" / name).write_text(content, encoding="utf-8")
        if disable is not None:
            (recipe / "patches").mkdir(exist_ok=True)
            (recipe / "patches" / rl.DISABLE_FILE).write_text(
                "\n".join(disable) + "\n", encoding="utf-8"
            )

        common = pkg_dir / rl.COMMON_DIR
        if common_build is not None:
            (common / "commands").mkdir(parents=True, exist_ok=True)
            (common / "commands" / "build").write_text(common_build + "\n", encoding="utf-8")
        for name, content in (common_patches or {}).items():
            (common / "patches").mkdir(parents=True, exist_ok=True)
            (common / "patches" / name).write_text(content, encoding="utf-8")
        return recipe


class EngineTests(unittest.TestCase):
    def test_multiline_values_are_reindented(self):
        template = "run: |\n  {{script}}\n"
        rendered, missing, _ = render.substitute(template, {"script": "line1\nline2\nline3"})
        self.assertEqual(rendered, "run: |\n  line1\n  line2\n  line3\n")
        self.assertEqual(missing, set())

    def test_blank_lines_in_value_are_not_indented(self):
        rendered, _, _ = render.substitute("a: |\n  {{v}}\n", {"v": "one\n\ntwo"})
        self.assertEqual(rendered, "a: |\n  one\n\n  two\n")

    def test_unresolved_placeholder_fails(self):
        with self.assertRaises(render.RenderError):
            render.render_template("x: {{nope}}\n", {})

    def test_conditionals_if_and_unless(self):
        template = textwrap.dedent(
            """\
            keep-me
            {{#if flag}}
            if-body
            {{/if}}
            {{#unless flag}}
            unless-body
            {{/unless}}
            """
        )
        on = render.apply_conditionals(template, {"flag": True})
        self.assertIn("if-body", on)
        self.assertNotIn("unless-body", on)
        off = render.apply_conditionals(template, {"flag": False})
        self.assertNotIn("if-body", off)
        self.assertIn("unless-body", off)

    def test_nested_conditionals(self):
        template = "{{#if a}}\nA\n{{#if b}}\nB\n{{/if}}\n{{/if}}\n"
        self.assertEqual(render.apply_conditionals(template, {"a": True, "b": False}).count("B"), 0)
        self.assertEqual(render.apply_conditionals(template, {"a": True, "b": True}).count("B"), 1)
        self.assertEqual(render.apply_conditionals(template, {"a": False, "b": True}).count("B"), 0)

    def test_unclosed_conditional_fails(self):
        with self.assertRaises(render.RenderError):
            render.apply_conditionals("{{#if a}}\n", {"a": True})

    def test_mismatched_close_fails(self):
        with self.assertRaises(render.RenderError):
            render.apply_conditionals("{{#if a}}\n{{/unless}}\n", {"a": True})

    def test_conditional_on_unknown_variable_fails(self):
        with self.assertRaises(render.RenderError):
            render.apply_conditionals("{{#if missing}}\n{{/if}}\n", {})

    def test_repeated_blank_lines_are_collapsed(self):
        rendered = render.render_template("a:\n\n\n\nb:\n", {})
        self.assertEqual(rendered, "a:\n\nb:\n")


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TempRepo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, pkg, ver, overrides=None, template=None, **extra):
        return render.render_target(
            self.repo.packages,
            pkg,
            ver,
            Path(template or REAL_TEMPLATE),
            overrides or {},
            **extra,
        )

    def test_scoped_slug_and_artifact_name(self):
        self.repo.add_recipe("@demo/pkg", "1.2.3")
        target = rl.find_target(self.repo.packages, "@demo/pkg", "1.2.3")
        self.assertEqual(target.slug_pkg, "demo-pkg")
        self.assertEqual(target.artifact_name, "demo-pkg-1.2.3-riscv64")

    def test_render_produces_valid_composite_action(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        rendered, scripts = self.render("@demo/pkg", "1.0.0")
        import yaml

        data = yaml.safe_load(rendered)
        self.assertEqual(data["runs"]["using"], "composite")
        names = [step.get("name") for step in data["runs"]["steps"]]
        self.assertIn("build", names)
        self.assertIn("test", names)
        self.assertIn("publish", names)
        # no patches in this recipe -> the patch step must be gone, not empty
        self.assertNotIn("apply patches", names)
        self.assertNotIn("patch", scripts)
        self.assertEqual(sorted(scripts), ["build", "fetch", "publish", "test"])

    def test_required_snippet_missing_is_rejected(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0", test=None)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("required command snippet 'test'", str(ctx.exception))

    def test_missing_stat_is_rejected(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=False)
        with self.assertRaises(rl.RecipeError):
            self.render("@demo/pkg", "1.0.0")

    def test_source_route_requires_sha256(self):
        stat = STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg") + textwrap.dedent(
            """\
            source:
              url: https://example.com/demo-1.0.0.tar.gz
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("source.sha256 is required", str(ctx.exception))

    def test_mutable_ref_is_rejected(self):
        stat = STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg").replace("v1.0.0", "main")
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("mutable reference", str(ctx.exception))

    def test_unknown_snippet_name_is_rejected(self):
        recipe = self.repo.add_recipe("@demo/pkg", "1.0.0")
        (recipe / "commands" / "biuld").write_text("echo typo\n", encoding="utf-8")
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("unknown command snippet 'biuld'", str(ctx.exception))

    def test_version_snippet_overrides_common(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            build="echo version-build",
            common_build="echo common-build",
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertIn("version-build", scripts["build"])
        self.assertNotIn("common-build", scripts["build"])

    def test_version_inherits_common_snippet(self):
        self.repo.add_recipe(
            "@demo/pkg", "1.0.0", build=None, common_build="echo common-build"
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertIn("common-build", scripts["build"])

    def test_patch_order_common_first_then_version(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            patches={"0002-version.patch": "version patch\n"},
            common_patches={"0001-common.patch": "common patch\n"},
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        body = scripts["patch"]
        self.assertLess(body.index("0001-common.patch"), body.index("0002-version.patch"))

    def test_disable_removes_common_patch(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            patches={"0002-version.patch": "version patch\n"},
            common_patches={"0001-common.patch": "common patch\n"},
            disable=["0001-common.patch"],
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertNotIn("0001-common.patch", scripts["patch"])
        self.assertIn("0002-version.patch", scripts["patch"])

    def test_dotfiles_are_not_patches(self):
        recipe = self.repo.add_recipe("@demo/pkg", "1.0.0", patches={"0001-a.patch": "a\n"})
        (recipe / "patches" / ".keep").write_text("", encoding="utf-8")
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertNotIn(".keep", scripts["patch"])

    def test_unquoted_scoped_name_is_caught(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        template = Path(self.tmp.name) / "bad-template.yml"
        template.write_text(
            "name: {{pkg.name}}\ndescription: x\nruns:\n  using: composite\n"
            "  steps:\n    - name: fetch\n      shell: bash\n      run: |\n        true\n"
            "    - name: build\n      shell: bash\n      run: |\n        true\n"
            "    - name: test\n      shell: bash\n      run: |\n        true\n"
            "    - name: publish\n      shell: bash\n      run: |\n        true\n",
            encoding="utf-8",
        )
        # `@demo/...` at the start of a scalar is not valid YAML: the quote rule
        # documented in docs/recipe-spec.md is enforced, not assumed.
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0", template=template)
        self.assertIn("not valid YAML", str(ctx.exception))

    def test_override_replaces_registry(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        _, scripts = self.render(
            "@demo/pkg",
            "1.0.0",
            overrides={"npm_publish.0.registry": "http://127.0.0.1:4873"},
        )
        self.assertIn("http://127.0.0.1:4873", scripts["publish"])

    def test_fetch_chain_covers_only_configured_routes(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        _, scripts = self.render("@demo/pkg", "1.0.0")
        fetch = scripts["fetch"]
        self.assertIn("route upstream", fetch)
        self.assertIn("not configured", fetch)  # source/npm stubs still present
        self.assertIn("fetch_upstream", fetch)

    def test_publish_order_puts_binary_first(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/Release/*.node

            npm_publish:
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
                requires: ["@demo/binding-linux-riscv64-gnu"]
                generate_manifest: true
              - role: binary
                name: "@demo/binding-linux-riscv64-gnu"
                version: 1.0.0
                dir: prebuilds-package
                os: linux
                cpu: riscv64
                generate_manifest: true
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        _, scripts = self.render("@demo/pkg", "1.0.0")
        publish = scripts["publish"]
        self.assertLess(
            publish.index("@demo/binding-linux-riscv64-gnu"),
            publish.index("@demo/main"),
        )
        self.assertIn("generate", publish)

    def test_publish_scope_rewrites_names_and_requires(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/Release/*.node

            npm_publish:
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
                requires: ["@demo/binding-linux-riscv64-gnu"]
              - role: binary
                name: "@demo/binding-linux-riscv64-gnu"
                version: 1.0.0
                dir: pkg
                os: linux
                cpu: riscv64
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        rendered, scripts = self.render("@demo/pkg", "1.0.0", publish_scope="cannedfish")
        publish = scripts["publish"]
        self.assertIn("@cannedfish/main", publish)
        self.assertIn("@cannedfish/binding-linux-riscv64-gnu", publish)
        self.assertNotIn("@demo/main", publish)
        self.assertNotIn("@demo/binding-linux-riscv64-gnu", publish)
        # the dependency follows the rename, otherwise validation would refuse
        # the recipe (requires must name a package of the same recipe)
        self.assertIn('"@cannedfish/binding-linux-riscv64-gnu"', publish)
        self.assertLess(
            publish.index("@cannedfish/binding-linux-riscv64-gnu"),
            publish.index("@cannedfish/main"),
        )

    def test_publish_scope_accepts_leading_at_and_unscoped_names(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: main
                name: plain-name
                version: 1.0.0
                dir: .
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        _, scripts = self.render("@demo/pkg", "1.0.0", publish_scope="@CannedFish/")
        self.assertIn("@CannedFish/plain-name", scripts["publish"])

    def test_publish_scope_rejects_an_empty_scope(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0", publish_scope="@/")
        self.assertIn("non-empty scope", str(ctx.exception))

    def test_publish_registry_applies_to_every_entry(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
                registry: https://registry.npmjs.org
              - role: binary
                name: "@demo/binding"
                version: 1.0.0
                dir: pkg
                os: linux
                cpu: riscv64
                registry: https://npm.pkg.github.com
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        _, scripts = self.render(
            "@demo/pkg", "1.0.0", publish_registry="http://127.0.0.1:4873"
        )
        publish = scripts["publish"]
        # one setup_auth call plus one publish_one line per entry
        self.assertEqual(publish.count("http://127.0.0.1:4873"), 3)
        self.assertNotIn("npm.pkg.github.com", publish)

    def test_publish_requires_os_and_cpu_for_binary(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: binary
                name: "@demo/binding"
                version: 1.0.0
                dir: pkg
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("requires os and cpu", str(ctx.exception))

    def test_publish_version_must_match_directory(self):
        stat = STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg").replace(
            "version: 1.0.0", "version: 9.9.9"
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("does not match the recipe version directory", str(ctx.exception))

    def test_requires_version_drift_is_rejected(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: binary
                name: "@demo/binding"
                version: 1.0.0
                dir: pkg
                os: linux
                cpu: riscv64
              - role: main
                name: "@demo/main"
                version: 2.0.0
                dir: .
                requires: ["@demo/binding"]
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("does not match required package", str(ctx.exception))


class RepositoryStructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TempRepo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_package_directory_is_reported(self):
        (self.repo.packages / "empty").mkdir(parents=True)
        problems = rl.repository_problems(self.repo.packages)
        self.assertTrue(any("no version subdirectory" in p for p in problems), problems)

    def test_version_directory_without_stat_is_reported(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        (self.repo.packages / "@demo/pkg/2.0.0").mkdir()
        problems = rl.repository_problems(self.repo.packages)
        self.assertTrue(any("missing .stat" in p for p in problems), problems)

    def test_stray_file_in_package_directory_is_reported(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        (self.repo.packages / "@demo/pkg/notes.txt").write_text("x", encoding="utf-8")
        problems = rl.repository_problems(self.repo.packages)
        self.assertTrue(any("unexpected file" in p for p in problems), problems)

    def test_clean_tree_has_no_problems(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        self.repo.add_recipe("@demo/pkg", "2.0.0")
        self.assertEqual(rl.repository_problems(self.repo.packages), [])


class GeneratedScriptSyntaxTests(unittest.TestCase):
    """Every generated script must at least be valid bash (L0 gate)."""

    def test_generated_scripts_pass_bash_syntax_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "action"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render.py",
                    "--pkg",
                    "@ruyisdk/bufferutil",
                    "--ver",
                    "4.2.0",
                    "--out-dir",
                    str(out),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            scripts = sorted((out / "steps").glob("*.sh"))
            self.assertTrue(scripts, "no scripts were generated")
            for script in scripts:
                check = subprocess.run(
                    ["bash", "-n", str(script)], capture_output=True, text=True
                )
                self.assertEqual(check.returncode, 0, f"{script}: {check.stderr}")

    def test_action_yml_is_self_consistent(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "action"
            subprocess.run(
                [
                    sys.executable,
                    "scripts/render.py",
                    "--pkg",
                    "@ruyisdk/bufferutil",
                    "--ver",
                    "4.2.0",
                    "--out-dir",
                    str(out),
                ],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            action = yaml.safe_load((out / "action.yml").read_text(encoding="utf-8"))
            self.assertEqual(action["runs"]["using"], "composite")
            # Every script the action mentions must exist, and every generated
            # script must be mentioned: a stale reference is a broken action.
            referenced = {
                line.split("/steps/")[-1].rstrip('"')
                for line in (out / "action.yml").read_text(encoding="utf-8").splitlines()
                if "/steps/" in line
            }
            on_disk = {script.name for script in (out / "steps").glob("*.sh")}
            self.assertEqual(referenced, on_disk, f"referenced {referenced}, on disk {on_disk}")


class GoldenTests(unittest.TestCase):
    """Snapshot the reference recipe so rendering drift is visible in review."""

    GOLDEN = REPO_ROOT / "tests/golden/bufferutil-4.2.0"

    def _render(self, target: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/render.py",
                "--pkg",
                "@ruyisdk/bufferutil",
                "--ver",
                "4.2.0",
                "--out-dir",
                str(target),
                "--steps-dir",
                ".rendered/action",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_render_matches_golden(self):
        import difflib

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "action"
            self._render(target)
            pairs = [(self.GOLDEN / "action.yml", target / "action.yml")]
            pairs += [
                (golden, target / "steps" / golden.name)
                for golden in sorted((self.GOLDEN / "steps").glob("*.sh"))
            ]
            for golden, actual in pairs:
                with self.subTest(file=golden.name):
                    self.assertTrue(golden.is_file(), f"missing golden {golden}")
                    expected = golden.read_text(encoding="utf-8")
                    got = actual.read_text(encoding="utf-8")
                    if expected != got:
                        diff = "".join(
                            difflib.unified_diff(
                                expected.splitlines(keepends=True),
                                got.splitlines(keepends=True),
                                fromfile=f"golden/{golden.name}",
                                tofile=f"rendered/{actual.name}",
                            )
                        )
                        self.fail(
                            "rendered output differs from the golden files; if the "
                            "change is intended run `python3 tests/regen_golden.py` "
                            f"and review the diff:\n{diff}"
                        )


class RepoIntegrationTests(unittest.TestCase):
    def test_check_all_passes_for_the_real_repository(self):
        result = subprocess.run(
            [sys.executable, "scripts/render.py", "--check-all"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[ OK ]", result.stdout)


if __name__ == "__main__":
    unittest.main()
