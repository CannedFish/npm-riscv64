"""Change-detector tests (L0: static/unit level).

Each test builds a throw-away git repository, so the diff logic is exercised the
way GitHub Actions drives it (three-dot for pull requests, two-dot for pushes).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import detect_targets as dt  # noqa: E402

TEMPLATE = ".github/templates/action.template.yml"

STAT = """\
upstream:
  repo: https://github.com/example/demo
  ref: v{ver}

artifacts:
  path: build/*.node

npm_publish:
  - role: main
    name: "@demo/{name}"
    version: {ver}
    dir: .
"""


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


class GitFixture:
    def __init__(self, root: Path):
        self.root = root
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "test")

    def write(self, relative: str, content: str = "x\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def recipe(self, pkg: str, ver: str, *, stat: bool = True) -> None:
        base = f"packages/{pkg}/{ver}"
        if stat:
            name = pkg.split("/")[-1]
            self.write(f"{base}/.stat", STAT.format(ver=ver, name=name))
        self.write(f"{base}/commands/build", "echo build\n")
        self.write(f"{base}/commands/test", "echo test\n")

    def common(self, pkg: str) -> None:
        self.write(f"packages/{pkg}/common/commands/build", "echo common\n")

    def commit(self, message: str = "change") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", message)
        return git(self.root, "rev-parse", "HEAD").strip()

    def mark_origin_main(self, sha: str) -> None:
        git(self.root, "update-ref", "refs/remotes/origin/main", sha)

    def branch(self, name: str = "feature") -> None:
        git(self.root, "checkout", "-q", "-b", name)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitFixture(Path(self.tmp.name))
        self.repo.write(TEMPLATE, "name: x\n")
        self.repo.recipe("plain", "1.0.0")
        self.repo.recipe("@scope/demo", "2.0.0")
        self.repo.recipe("plain", "1.1.0")
        base = self.repo.commit("base")
        self.repo.mark_origin_main(base)
        self.repo.branch()
        self.packages = self.repo.root / "packages"

    def tearDown(self):
        self.tmp.cleanup()

    def run_detect(self, *, event="pull_request", before="", base_ref="main") -> dict:
        head = self.repo.commit("feature change")
        changed, rebuild_all = dt.changed_files(
            event, base_ref, head, before or head, cwd=self.repo.root
        )
        targets, self_check = dt.resolve_targets(
            self.packages, changed, TEMPLATE, rebuild_all
        )
        return {
            "targets": [(t.pkg, t.ver) for t in targets],
            "self_check": self_check,
            "problems": dt.verify_recipes(targets),
        }

    def test_version_change_selects_one_target(self):
        self.repo.write("packages/plain/1.0.0/commands/build", "echo changed\n")
        result = self.run_detect()
        self.assertEqual(result["targets"], [("plain", "1.0.0")])
        self.assertEqual(result["problems"], [])

    def test_scoped_path_is_parsed(self):
        self.repo.write("packages/@scope/demo/2.0.0/commands/test", "echo t2\n")
        result = self.run_detect()
        self.assertEqual(result["targets"], [("@scope/demo", "2.0.0")])

    def test_common_change_expands_to_every_version(self):
        self.repo.common("plain")
        result = self.run_detect()
        self.assertEqual(sorted(result["targets"]), [("plain", "1.0.0"), ("plain", "1.1.0")])

    def test_template_change_rebuilds_everything(self):
        self.repo.write(TEMPLATE, "name: y\n")
        result = self.run_detect()
        self.assertEqual(
            sorted(result["targets"]),
            [("@scope/demo", "2.0.0"), ("plain", "1.0.0"), ("plain", "1.1.0")],
        )

    def test_scripts_change_requests_self_check_only(self):
        self.repo.write("scripts/render.py", "# changed\n")
        result = self.run_detect()
        self.assertTrue(result["self_check"])
        self.assertEqual(result["targets"], [])

    def test_documentation_change_builds_nothing(self):
        self.repo.write("README.md", "docs\n")
        result = self.run_detect()
        self.assertEqual(result["targets"], [])
        self.assertFalse(result["self_check"])

    def test_deleted_version_is_skipped(self):
        git(self.repo.root, "rm", "-q", "-r", "packages/plain/1.1.0")
        result = self.run_detect()
        self.assertEqual(result["targets"], [])

    def test_missing_stat_is_reported(self):
        self.repo.recipe("plain", "3.0.0", stat=False)
        result = self.run_detect()
        self.assertEqual(result["targets"], [("plain", "3.0.0")])
        self.assertTrue(result["problems"])
        self.assertIn("missing .stat", result["problems"][0])

    def test_push_with_zero_before_rebuilds_everything(self):
        self.repo.write("packages/plain/1.0.0/commands/build", "echo z\n")
        head = self.repo.commit("push change")
        changed, rebuild_all = dt.changed_files(
            "push", "main", head, "0" * 40, cwd=self.repo.root
        )
        self.assertTrue(rebuild_all)
        targets, _ = dt.resolve_targets(self.packages, changed, TEMPLATE, rebuild_all)
        self.assertEqual(len(targets), 3)

    def test_push_range_selects_only_changed_recipe(self):
        before = git(self.repo.root, "rev-parse", "HEAD").strip()
        self.repo.write("packages/@scope/demo/2.0.0/commands/test", "echo z\n")
        head = self.repo.commit("push change")
        changed, rebuild_all = dt.changed_files(
            "push", "main", head, before, cwd=self.repo.root
        )
        self.assertFalse(rebuild_all)
        targets, _ = dt.resolve_targets(self.packages, changed, TEMPLATE, rebuild_all)
        self.assertEqual([(t.pkg, t.ver) for t in targets], [("@scope/demo", "2.0.0")])


class DetectOutputTests(unittest.TestCase):
    def test_github_output_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = GitFixture(root)
            repo.write(TEMPLATE, "name: x\n")
            repo.recipe("@scope/demo", "2.0.0")
            base = repo.commit("base")
            repo.mark_origin_main(base)
            repo.branch()
            repo.write("packages/@scope/demo/2.0.0/commands/build", "echo changed\n")
            head = repo.commit("change")

            output = root / "github_output"
            output.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env.update(
                EVENT_NAME="pull_request",
                BASE_REF="main",
                HEAD_SHA=head,
                GITHUB_OUTPUT=str(output),
                RECIPE_ROOT="packages",
                TEMPLATE_PATH=TEMPLATE,
            )
            saved = dict(os.environ)
            os.environ.update(env)
            try:
                code = dt.main(cwd=root)
            finally:
                os.environ.clear()
                os.environ.update(saved)

            self.assertEqual(code, 0)
            lines = output.read_text(encoding="utf-8").splitlines()
            keys = dict(line.split("=", 1) for line in lines)
            self.assertEqual(keys["has_targets"], "true")
            self.assertEqual(keys["publish"], "false")
            payload = json.loads(keys["targets"])
            self.assertEqual(payload[0]["pkg"], "@scope/demo")
            self.assertEqual(payload[0]["slug"], "scope-demo/2.0.0")
            self.assertEqual(payload[0]["artifact"], "scope-demo-2.0.0-riscv64")
            self.assertEqual(payload[0]["artifact_glob"], "build/*.node")


if __name__ == "__main__":
    unittest.main()
