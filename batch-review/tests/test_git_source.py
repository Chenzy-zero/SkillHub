"""Local-only tests for Git source resolution.

The tests create ordinary and bare repositories under a temporary directory.
They never contact a network service and never execute files from the test
repositories.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from skill_batch_review.git_source import (  # noqa: E402
    BRANCH_CONTENT_CONFLICT,
    CommitNotFoundError,
    GitMirror,
    GitRunner,
    GitSourceResolver,
    SELECTED,
    SKIPPED_SUPERSEDED_BRANCH,
    STALE_INVENTORY,
)
from skill_batch_review.inventory import INVENTORY_COLUMNS, parse_inventory_csv  # noqa: E402


def git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run a test-only Git command using an argv vector."""

    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        shell=False,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return completed.stdout.strip()


def commit(
    repository: Path,
    message: str,
    *,
    timestamp: str,
) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
    )
    git("add", "--all", cwd=repository)
    git("commit", "-m", message, cwd=repository, env=env)
    return git("rev-parse", "HEAD", cwd=repository)


def inventory_row(
    *,
    branch: str,
    revision: str,
    skill_path: str = "skills/demo",
    skill_name: str = "demo",
) -> object:
    values = (
        skill_name,
        "team/demo",
        branch,
        skill_path,
        revision,
        "否",
        "active",
    )
    csv_text = ",".join(INVENTORY_COLUMNS) + "\n" + ",".join(values) + "\n"
    return parse_inventory_csv(
        csv_text.encode("utf-8"),
        status_mapping={"active": "ACTIVE"},
    ).rows[0]


class GitRunnerTests(unittest.TestCase):
    @patch("skill_batch_review.git_source.subprocess.run")
    def test_runner_uses_argv_and_shell_false(self, mocked_run) -> None:
        mocked_run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        result = GitRunner(default_timeout=None).run(["rev-parse", "HEAD"])

        self.assertTrue(result.ok)
        command = mocked_run.call_args.args[0]
        self.assertEqual(command, ["git", "rev-parse", "HEAD"])
        self.assertFalse(mocked_run.call_args.kwargs["shell"])
        self.assertFalse(mocked_run.call_args.kwargs["check"])

    def test_command_string_and_nul_are_rejected_before_process_start(self) -> None:
        runner = GitRunner()
        with self.assertRaises(TypeError):
            runner.run("git status")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            runner.run(["status\x00"])


class GitSourceResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git("init", "--initial-branch=main", cwd=self.source)
        git("config", "user.name", "Test User", cwd=self.source)
        git("config", "user.email", "test@example.invalid", cwd=self.source)
        (self.source / "skills/demo").mkdir(parents=True)
        (self.source / "skills/demo/SKILL.md").write_text("# Demo\ninitial\n", encoding="utf-8")
        (self.source / "skills/demo/check.sh").write_text(
            "#!/bin/sh\necho must never run\n", encoding="utf-8"
        )
        self.first = commit(
            self.source,
            "initial Skill",
            timestamp="2024-01-01T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _mirror(self) -> GitSourceResolver:
        destination = self.root / "mirror.git"
        mirror = GitMirror(str(self.source), destination)
        mirror.clone()
        self.assertTrue(mirror.is_bare())
        return GitSourceResolver(destination)

    def test_clone_and_fetch_are_explicit_and_resolver_only_reads(self) -> None:
        destination = self.root / "mirror.git"
        runner = GitRunner()
        mirror = GitMirror(str(self.source), destination, runner=runner)
        # Construction alone must not invoke Git or touch the source.
        self.assertFalse(destination.exists())
        mirror.clone()
        self.assertTrue(destination.exists())

        # Add another source commit, then explicitly update the mirror.
        (self.source / "README.md").write_text("unrelated\n", encoding="utf-8")
        second = commit(
            self.source,
            "unrelated change",
            timestamp="2024-01-02T00:00:00+00:00",
        )
        resolver = GitSourceResolver(destination)
        self.assertNotEqual(resolver.resolve_branch_head("main"), second)
        mirror.fetch()
        self.assertEqual(resolver.resolve_branch_head("main"), second)

    def test_branch_head_commit_type_reachability_and_skill_presence(self) -> None:
        resolver = self._mirror()
        head = resolver.resolve_branch_head("refs/heads/main")
        self.assertEqual(head, self.first)
        self.assertEqual(resolver.branch_head("main"), self.first)
        self.assertEqual(resolver.verify_commit(self.first[:10]), self.first)
        self.assertTrue(resolver.is_revision_reachable("main", self.first))
        self.assertTrue(resolver.skill_path_exists(self.first, "./skills/demo/"))
        self.assertFalse(resolver.skill_path_exists(self.first, "skills/missing"))
        with self.assertRaises(CommitNotFoundError):
            resolver.verify_commit("0" * 40)

        tree = git("rev-parse", f"{self.first}^{{tree}}", cwd=self.source)
        with self.assertRaises(CommitNotFoundError):
            resolver.verify_commit(tree)

    def test_path_last_change_returns_commit_and_timezone_aware_time(self) -> None:
        (self.source / "skills/demo/SKILL.md").write_text(
            "# Demo\nchanged\n", encoding="utf-8"
        )
        changed = commit(
            self.source,
            "update Skill",
            timestamp="2024-02-03T04:05:06+08:00",
        )
        resolver = self._mirror()
        change = resolver.path_last_change(changed, "skills/demo")
        self.assertEqual(change.revision, changed)
        self.assertEqual(change.skill_last_change_revision, changed)
        self.assertEqual(change.skill_last_change_time.utcoffset().total_seconds(), 8 * 3600)

    def test_root_skill_last_change_includes_non_anchor_files(self) -> None:
        (self.source / "SKILL.md").write_text("# Root Skill\n", encoding="utf-8")
        root_created = commit(
            self.source,
            "add root Skill",
            timestamp="2024-02-04T00:00:00+00:00",
        )
        (self.source / "root-helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        helper_changed = commit(
            self.source,
            "change root Skill helper",
            timestamp="2024-02-05T00:00:00+00:00",
        )
        resolver = self._mirror()
        change = resolver.path_last_change(helper_changed, ".")
        self.assertNotEqual(root_created, helper_changed)
        self.assertEqual(change.revision, helper_changed)

    def test_latest_branch_is_selected_by_path_change_time_not_sha_text(self) -> None:
        # Keep the original branch point, then give the two branches different
        # path-change times.  The test intentionally does not compare object
        # ID text; GitSourceResolver compares the parsed commit timestamps.
        git("branch", "release", self.first, cwd=self.source)
        (self.source / "skills/demo/SKILL.md").write_text("# Demo\nmain\n", encoding="utf-8")
        main_head = commit(
            self.source,
            "main Skill update",
            timestamp="2024-02-01T00:00:00+00:00",
        )
        git("checkout", "release", cwd=self.source)
        (self.source / "skills/demo/SKILL.md").write_text("# Demo\nrelease\n", encoding="utf-8")
        release_head = commit(
            self.source,
            "release Skill update",
            timestamp="2024-03-01T00:00:00+00:00",
        )
        git("checkout", "main", cwd=self.source)

        resolver = self._mirror()
        result = resolver.resolve_sources(
            [
                inventory_row(branch="main", revision=main_head),
                inventory_row(branch="release", revision=release_head),
            ]
        )
        by_branch = {record.row.branch: record for record in result.records}
        self.assertEqual(by_branch["release"].source_selection_status, SELECTED)
        self.assertEqual(by_branch["main"].source_selection_status, SKIPPED_SUPERSEDED_BRANCH)
        self.assertEqual(result.selected[0].source_revision, release_head)

    def test_same_path_change_time_with_different_commits_waits_for_snapshot(self) -> None:
        git("branch", "tie-a", self.first, cwd=self.source)
        git("branch", "tie-b", self.first, cwd=self.source)

        git("checkout", "tie-a", cwd=self.source)
        (self.source / "skills/demo/SKILL.md").write_text("# A\n", encoding="utf-8")
        tie_a = commit(
            self.source,
            "tie A",
            timestamp="2024-04-01T00:00:00+00:00",
        )
        git("checkout", "tie-b", cwd=self.source)
        (self.source / "skills/demo/SKILL.md").write_text("# B\n", encoding="utf-8")
        tie_b = commit(
            self.source,
            "tie B",
            timestamp="2024-04-01T00:00:00+00:00",
        )
        resolver = self._mirror()
        result = resolver.resolve_sources(
            [
                inventory_row(branch="tie-a", revision=tie_a),
                inventory_row(branch="tie-b", revision=tie_b),
            ]
        )
        self.assertEqual(len(result.selected), 0)
        self.assertEqual(len(result.conflicts), 2)
        self.assertTrue(all(item.source_selection_status == BRANCH_CONTENT_CONFLICT for item in result.conflicts))
        self.assertTrue(all(item.needs_snapshot for item in result.conflicts))

    def test_inventory_revision_difference_is_stale_and_is_not_selected(self) -> None:
        (self.source / "skills/demo/SKILL.md").write_text("# Demo\nnew\n", encoding="utf-8")
        current = commit(
            self.source,
            "new Skill revision",
            timestamp="2024-05-01T00:00:00+00:00",
        )
        resolver = self._mirror()
        record = resolver.resolve_row(inventory_row(branch="main", revision=self.first))
        self.assertEqual(record.source_selection_status, STALE_INVENTORY)
        self.assertEqual(record.resolved_branch_head, current)
        self.assertIn("inventory_revision_differs_from_branch_head", record.inventory_difference_reasons)
        self.assertIn("inventory_revision_differs_from_skill_path_last_change", record.inventory_difference_reasons)
        result = resolver.resolve_sources([inventory_row(branch="main", revision=self.first)])
        self.assertEqual(result.selected, ())
        self.assertEqual(result.stale_inventory[0].source_selection_status, STALE_INVENTORY)


if __name__ == "__main__":
    unittest.main()
