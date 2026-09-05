from __future__ import annotations

import json
import csv
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from skill_batch_review.config import load_config
from skill_batch_review.inventory import parse_inventory_csv
from skill_batch_review.git_source import GitCommandError, GitResult
from skill_batch_review.per_skill import (
    PartialDownload,
    PerSkillError,
    cleanup_repository_download,
    cleanup_skill_download,
    download_repository_skills,
    finalize_skill,
    partial_fetch_skill_repository,
    prepare_skill,
    write_skill_result_tables,
)
from skill_batch_review.scanners import (
    CiscoSkillScannerAdapter,
    CommandExecution,
    SkillSpectorAdapter,
)
from test_orchestrator import REPO_ROOT, SCHEMA, valid_ai_result


class ScannerRunner:
    def __init__(self):
        self.call_count = 0

    def run(self, argv, *, timeout_seconds, cwd=None, env=None):
        self.call_count += 1
        command = tuple(argv)
        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"findings": []}), encoding="utf-8")
        return CommandExecution(
            argv=command,
            returncode=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            started_at="2026-09-03T08:00:00Z",
            finished_at="2026-09-03T08:00:01Z",
            duration_seconds=1.0,
        )


class PerSkillWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "test@example.com"], check=True)
        skill = self.source / "skills/sample"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
        (skill / "guide.txt").write_text("read only\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-m", "skill"], check=True, capture_output=True)
        self.revision = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tearDown(self):
        self.temporary.cleanup()

    def config(self):
        path = self.root / "review.toml"
        path.write_text(
            f'''
[batch]
inventory_csv = "inventory.csv"
included_statuses = ["ACTIVE"]
[workspace]
root = "work"
evidence_root = "evidence"
candidate_root = "candidates"
manifest_root = "manifests"
git_download_root = "git_download"
skills_root = "skills-output"
results_root = "results"
[gerrit]
ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
[status_mapping]
active = "ACTIVE"
[quality]
candidate_threshold = 70
[ai]
skill_path = "{(REPO_ROOT / 'batch-review/skills/skill-security-review').as_posix()}"
result_schema_path = "{SCHEMA.as_posix()}"
policy_version = "policy-1"
reviewer_model = "intranet-model"
[scanners.cisco]
version = "1.0"
command = ["skill-scanner", "scan", "{{skill_root}}", "--format", "json", "--compact", "--output", "{{output_file}}"]
[scanners.skillspector]
version = "1.0"
command = ["skillspector", "scan", "{{skill_root}}", "--no-llm", "--format", "json", "--output", "{{output_file}}"]
''',
            encoding="utf-8",
        )
        return load_config(path)

    def inventory(self):
        text = (
            "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,product_line,user_name,user_email\n"
            f"id-one,sample,team/one,main,skills/sample,{self.revision},否,active,product-a,Alice,alice@example.com\n"
            f"id-two,sample,team/two,main,skills/sample,{self.revision},否,active,product-b,Bob,bob@example.com\n"
        )
        return parse_inventory_csv(text, status_mapping={"active": "ACTIVE"})

    def downloader(self, config, *, batch_id, row, task_id):
        task_root = config.workspace.git_download_root / batch_id / task_id
        task_root.mkdir(parents=True)
        return PartialDownload(task_root, self.source, self.revision)

    def test_archives_each_skill_and_reuses_approved_content(self):
        config = self.config()
        inventory = self.inventory()
        runner = ScannerRunner()
        adapters = {
            "cisco": CiscoSkillScannerAdapter(runner=runner, tool_version="1.0"),
            "skillspector": SkillSpectorAdapter(runner=runner, tool_version="1.0"),
        }
        first = prepare_skill(
            config,
            batch_id="batch-1",
            row=inventory.rows[0],
            downloader=self.downloader,
            adapters=adapters,
        )
        self.assertTrue(first.requires_ai)
        self.assertEqual(runner.call_count, 2)
        self.assertTrue((config.workspace.skills_root / "id-one/sample/SKILL.md").is_file())
        self.assertFalse((config.workspace.skills_root / "id-one/sample/.git").exists())
        ai_path = self.root / "ai.json"
        ai_path.write_text(
            json.dumps(valid_ai_result(first.task_id, self.revision, first.snapshot.skill_digest)),
            encoding="utf-8",
        )
        first_result = finalize_skill(config, index_path=first.index_path, ai_result_path=ai_path)
        self.assertEqual(first_result["security_decision"], "PASS")
        self.assertTrue((config.workspace.skills_root / "id-one/review-result.json").is_file())
        first_csv, _ = write_skill_result_tables(config, inventory, batch_id="batch-1")
        with first_csv.open("r", encoding="utf-8", newline="") as handle:
            partial_rows = list(csv.DictReader(handle))
        self.assertEqual(partial_rows[0]["review_status"], "COMPLETED")
        self.assertEqual(partial_rows[1]["review_status"], "PENDING")
        cleanup_skill_download(config, batch_id="batch-1", task_id=first.task_id)

        second = prepare_skill(
            config,
            batch_id="batch-1",
            row=inventory.rows[1],
            downloader=self.downloader,
            adapters=adapters,
        )
        self.assertFalse(second.requires_ai)
        self.assertEqual(second.result["reuse_status"], "RESULT_REUSED")
        self.assertEqual(second.result["reused_from_skill_id"], "id-one")
        self.assertEqual(second.result["content_id"], first_result["content_id"])
        self.assertEqual(runner.call_count, 2)
        self.assertTrue((config.workspace.skills_root / "id-two/sample/SKILL.md").is_file())
        self.assertTrue((config.workspace.skills_root / "id-two/review-result.json").is_file())

        csv_path, json_path = write_skill_result_tables(config, inventory, batch_id="batch-1")
        self.assertIn("reused_from_skill_id", csv_path.read_text(encoding="utf-8"))
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["security_reviewed"] for row in rows], ["是", "是"])
        self.assertEqual(rows[1]["reuse_status"], "RESULT_REUSED")
        aggregate = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(aggregate["result_count"], 2)
        self.assertEqual({item["content_id"] for item in aggregate["skills"]}, {first_result["content_id"]})

    def test_partial_fetch_refuses_server_that_ignores_blob_filter(self):
        class UnsupportedRunner:
            def checked(self, args, *, cwd=None, timeout=None):
                if args[0] == "ls-remote":
                    return GitResult(tuple(args), 0, f"{self.revision}\trefs/heads/main\n", "")
                if args[0] == "archive":
                    raise GitCommandError(GitResult(tuple(args), 1, "", "archive disabled"))
                return GitResult(tuple(args), 0, "", "")

            def run(self, args, *, cwd=None, timeout=None, check=True, input_text=None):
                return GitResult(
                    tuple(args),
                    0,
                    "",
                    "warning: filtering not recognized by server, ignoring",
                )

        config = self.config()
        row = self.inventory().rows[0]
        runner = UnsupportedRunner()
        runner.revision = self.revision
        with self.assertRaisesRegex(PerSkillError, "SKILL_ONLY_DOWNLOAD_UNSUPPORTED"):
            partial_fetch_skill_repository(
                config,
                batch_id="unsupported",
                row=row,
                task_id="skill-test",
                runner=runner,
            )
        self.assertFalse((config.workspace.git_download_root / "unsupported/skill-test").exists())

    def test_remote_archive_downloads_only_skill_without_git_directory(self):
        class ArchiveRunner:
            def checked(self, args, *, cwd=None, timeout=None):
                if args[0] == "ls-remote":
                    return GitResult(tuple(args), 0, f"{self.revision}\trefs/heads/main\n", "")
                if args[0] == "archive":
                    output = Path(next(item.split("=", 1)[1] for item in args if item.startswith("--output=")))
                    content = b"---\nname: sample\n---\n"
                    with tarfile.open(output, "w") as archive:
                        info = tarfile.TarInfo("skills/sample/SKILL.md")
                        info.size = len(content)
                        info.mode = 0o644
                        archive.addfile(info, io.BytesIO(content))
                    return GitResult(tuple(args), 0, "", "")
                raise AssertionError(f"unexpected Git command: {args}")

        config = self.config()
        row = self.inventory().rows[0]
        runner = ArchiveRunner()
        runner.revision = self.revision
        result = partial_fetch_skill_repository(
            config,
            batch_id="archive",
            row=row,
            task_id="skill-archive",
            runner=runner,
        )
        self.assertEqual(result.transport, "remote_archive")
        self.assertIsNotNone(result.snapshot)
        self.assertTrue((result.task_root / "sample/SKILL.md").is_file())
        self.assertFalse((result.task_root / ".transport.git").exists())
        self.assertFalse((result.task_root / ".skill-archive.tar").exists())
        cleanup_skill_download(config, batch_id="archive", task_id="skill-archive")

    def test_repository_archive_is_downloaded_once_for_all_listed_skills(self):
        inventory = parse_inventory_csv(
            (
                "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status\n"
                f"id-one,first,team/one,main,skills/first,{'a' * 40},否,active\n"
                f"id-two,second,team/one,main,skills/second,{'b' * 40},否,active\n"
            ),
            status_mapping={"active": "ACTIVE"},
        )

        class RepositoryArchiveRunner:
            def __init__(self, revision):
                self.revision = revision
                self.archive_calls = 0

            def checked(self, args, *, cwd=None, timeout=None):
                if args[0] == "ls-remote":
                    return GitResult(tuple(args), 0, f"{self.revision}\trefs/heads/main\n", "")
                if args[0] == "archive":
                    self.archive_calls += 1
                    output = Path(next(item.split("=", 1)[1] for item in args if item.startswith("--output=")))
                    with tarfile.open(output, "w") as archive:
                        for name in ("first", "second"):
                            content = f"---\nname: {name}\n---\n".encode()
                            info = tarfile.TarInfo(f"skills/{name}/SKILL.md")
                            info.size = len(content)
                            info.mode = 0o644
                            archive.addfile(info, io.BytesIO(content))
                    return GitResult(tuple(args), 0, "", "")
                raise AssertionError(f"unexpected Git command: {args}")

        config = self.config()
        runner = RepositoryArchiveRunner(self.revision)
        result = download_repository_skills(
            config,
            batch_id="repository-batch",
            rows=inventory.rows,
            runner=runner,
        )
        self.assertEqual(runner.archive_calls, 1)
        self.assertEqual(result.revision, self.revision)
        self.assertEqual(result.transport, "whole_repository_archive")
        self.assertEqual(set(result.skills), {row.source_row_id for row in inventory.rows})
        for row in inventory.rows:
            snapshot = result.skills[row.source_row_id].snapshot
            self.assertIsNotNone(snapshot)
            self.assertTrue((snapshot.snapshot_path / "SKILL.md").is_file())
            self.assertEqual(snapshot.source_revision, self.revision)
        self.assertFalse((result.workspace_root / ".repository-archive.tar").exists())
        self.assertTrue(
            cleanup_repository_download(
                config,
                batch_id="repository-batch",
                repository="team/one",
                branch="main",
            )
        )

    def test_repository_archive_failure_has_stable_error_code(self):
        inventory = parse_inventory_csv(
            (
                "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status\n"
                f"id-one,first,team/one,main,skills/first,{'a' * 40},否,active\n"
            ),
            status_mapping={"active": "ACTIVE"},
        )

        class FailingArchiveRunner:
            def checked(self, args, *, cwd=None, timeout=None):
                if args[0] == "ls-remote":
                    return GitResult(tuple(args), 0, f"{self_revision}\trefs/heads/main\n", "")
                raise GitCommandError(GitResult(tuple(args), 1, "", "archive denied"))

        self_revision = self.revision
        with self.assertRaisesRegex(PerSkillError, "REPOSITORY_ARCHIVE_UNAVAILABLE"):
            download_repository_skills(
                self.config(),
                batch_id="repository-failure",
                rows=inventory.rows,
                runner=FailingArchiveRunner(),
            )

    def test_partial_fetch_recovers_same_task_left_by_failed_windows_cleanup(self):
        class SuccessfulRunner:
            def checked(self, args, *, cwd=None, timeout=None):
                if args[0] == "ls-remote":
                    return GitResult(tuple(args), 0, f"{self.revision}\trefs/heads/main\n", "")
                if args[0] == "archive":
                    raise GitCommandError(GitResult(tuple(args), 1, "", "archive disabled"))
                if args[0] == "init":
                    self.assert_repository_was_removed = not Path(args[-1]).exists()
                    return GitResult(tuple(args), 0, "", "")
                if args[0] == "rev-parse":
                    return GitResult(tuple(args), 0, self.revision + "\n", "")
                return GitResult(tuple(args), 0, "", "")

            def run(self, args, *, cwd=None, timeout=None, check=True, input_text=None):
                return GitResult(tuple(args), 0, "", "")

        config = self.config()
        row = self.inventory().rows[0]
        task_id = "skill-stale-retry"
        stale = config.workspace.git_download_root / "retry" / task_id / ".transport.git"
        pack = stale / "objects/pack/stale.idx"
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b"stale")
        pack.chmod(0o400)
        runner = SuccessfulRunner()
        runner.revision = self.revision

        result = partial_fetch_skill_repository(
            config,
            batch_id="retry",
            row=row,
            task_id=task_id,
            runner=runner,
        )
        self.assertTrue(runner.assert_repository_was_removed)
        self.assertEqual(result.revision, self.revision)
        cleanup_skill_download(config, batch_id="retry", task_id=task_id)

    def test_cleanup_removes_read_only_git_object(self):
        config = self.config()
        target = config.workspace.git_download_root / "batch-1/skill-readonly"
        pack = target / ".transport.git/objects/pack/example.idx"
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b"git-index")
        pack.chmod(0o400)
        try:
            self.assertTrue(
                cleanup_skill_download(
                    config,
                    batch_id="batch-1",
                    task_id="skill-readonly",
                )
            )
            self.assertFalse(target.exists())
        finally:
            if pack.exists():
                pack.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
