from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from skill_batch_review.config import load_config
from skill_batch_review.inventory import parse_inventory_csv
from skill_batch_review.orchestrator import (
    cleanup_repository_workspace,
    finalize_repository,
    plan_repositories,
    prepare_repository,
)
from skill_batch_review.scanners import (
    CiscoSkillScannerAdapter,
    CommandExecution,
    SkillSpectorAdapter,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / ".claude/skills/skill-security-review/references/review-result.schema.json"


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
            started_at="2026-08-31T08:00:00Z",
            finished_at="2026-08-31T08:00:01Z",
            duration_seconds=1.0,
        )


def valid_ai_result(task_id: str, revision: str, digest: str) -> dict:
    dimensions = [
        ("PURPOSE_AND_TRIGGER", 18, 20),
        ("INSTRUCTION_CLARITY", 22, 25),
        ("SCOPE_AND_PERMISSION_FIT", 13, 15),
        ("ROBUSTNESS_AND_BOUNDARIES", 18, 20),
        ("MAINTAINABILITY_AND_VERIFIABILITY", 19, 20),
    ]
    return {
        "schema_version": "1.0",
        "review_id": task_id,
        "policy_version": "policy-1",
        "reviewed_at": "2026-08-31T08:10:00Z",
        "reviewer": {"kind": "AI", "model": "intranet-model"},
        "subject": {
            "skill_name": "sample",
            "repo_name": "team/repo",
            "branch": "main",
            "skill_path": "skills/sample",
            "inventory_revision": revision,
            "source_revision": revision,
            "skill_digest_sha256": digest,
        },
        "input_coverage": {
            "package_complete": True,
            "manifest_status": "COMPLETE",
            "files_expected": 2,
            "files_reviewed": 2,
            "unreadable_or_skipped_files": [],
            "static_reports": [
                {
                    "scanner": "CISCO_AI_SKILL_SCANNER",
                    "status": "COMPLETED",
                    "tool_version": "1.0",
                    "rules_or_config_version": "config-1",
                    "scanned_digest_sha256": digest,
                    "report_path": "cisco.json",
                },
                {
                    "scanner": "NVIDIA_SKILLSPECTOR",
                    "status": "COMPLETED",
                    "tool_version": "1.0",
                    "rules_or_config_version": "config-1",
                    "scanned_digest_sha256": digest,
                    "report_path": "skillspector.json",
                },
            ],
            "digest_consistent": True,
            "traceability_complete": True,
            "limitations": [],
        },
        "security_review": {
            "verdict": "PASS",
            "max_severity": "NONE",
            "summary": "No issue found.",
            "findings": [],
        },
        "quality_review": {
            "basis": "STATIC_PACKAGE_REVIEW",
            "verdict": "PASS",
            "score": 90,
            "summary": "Clear and maintainable.",
            "dimensions": [
                {
                    "name": name,
                    "anchor": "STRONG",
                    "score": score,
                    "max_score": maximum,
                    "reason": "Evidence reviewed.",
                }
                for name, score, maximum in dimensions
            ],
            "findings": [],
        },
        "overall": {
            "disposition": "APPROVE_CANDIDATE",
            "private_candidate_eligible": True,
            "reasons": ["All required reviews completed."],
        },
    }


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

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
[gerrit]
ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
[status_mapping]
active = "ACTIVE"
inactive = "INACTIVE"
[quality]
candidate_threshold = 70
[ai]
skill_path = "{REPO_ROOT / '.claude/skills/skill-security-review'}"
result_schema_path = "{SCHEMA}"
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

    def make_source_repository(self) -> tuple[Path, str]:
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        skill = source / "skills/sample"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
        (skill / "guide.txt").write_text("read only\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "add skill"], check=True, capture_output=True)
        revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return source, revision

    @staticmethod
    def inventory(revision: str):
        csv = (
            "skill_name,repo_name,branch,skill_path,lasted_commited,security_reviewed,status\n"
            f"sample,team/repo,main,skills/sample,{revision},否,active\n"
            f"old,team/old,main,skills/old,{revision},是,inactive\n"
        )
        return parse_inventory_csv(csv, status_mapping={"active": "ACTIVE", "inactive": "INACTIVE"})

    def test_repository_plan_excludes_unapproved_lifecycle_status(self) -> None:
        _, revision = self.make_source_repository()
        plans = plan_repositories(self.inventory(revision), included_statuses=("ACTIVE",))
        by_repo = {plan.repository: plan for plan in plans}
        self.assertEqual(len(by_repo["team/repo"].included_rows), 1)
        self.assertEqual(len(by_repo["team/old"].included_rows), 0)
        self.assertEqual(len(by_repo["team/old"].excluded_rows), 1)

    def test_two_phase_repository_flow_exports_then_cleans_local_candidate(self) -> None:
        source, revision = self.make_source_repository()
        config = self.config()
        rows = plan_repositories(
            self.inventory(revision), included_statuses=config.batch.included_statuses
        )[1].included_rows

        def sync(_repository: str, destination: Path) -> Path:
            subprocess.run(
                ["git", "clone", "--mirror", str(source), str(destination)],
                check=True,
                capture_output=True,
            )
            return destination

        runner = ScannerRunner()
        prepared = prepare_repository(
            config,
            batch_id="batch-1",
            repository="team/repo",
            rows=rows,
            mirror_sync=sync,
            adapters={
                "cisco": CiscoSkillScannerAdapter(runner=runner, tool_version="1.0"),
                "skillspector": SkillSpectorAdapter(runner=runner, tool_version="1.0"),
            },
        )
        self.assertEqual(len(prepared.tasks), 1)
        task = prepared.tasks[0]
        self.assertTrue(task.handoff_path.is_file())
        self.assertTrue(task.snapshot.snapshot_path.is_dir())

        ai_results = self.root / "ai-results"
        ai_results.mkdir()
        (ai_results / f"{task.task_id}.json").write_text(
            json.dumps(
                valid_ai_result(task.task_id, task.snapshot.source_revision, task.snapshot.skill_digest)
            ),
            encoding="utf-8",
        )
        results = finalize_repository(
            config,
            batch_id="batch-1",
            repository_index=prepared.index_path,
            ai_results_dir=ai_results,
        )
        self.assertEqual(results[0]["security_decision"], "PASS")
        self.assertEqual(results[0]["candidate_status"], "EXPORTED_LOCAL")
        self.assertTrue(Path(results[0]["candidate"]["package_path"]).is_dir())
        self.assertTrue(
            cleanup_repository_workspace(
                config,
                batch_id="batch-1",
                repository="team/repo",
                repository_index=prepared.index_path,
            )
        )
        self.assertFalse(prepared.mirror_path.exists())
        self.assertTrue(Path(results[0]["candidate"]["package_path"]).is_dir())

    def test_same_root_name_and_identical_content_reuses_only_approved_result(self) -> None:
        source, revision = self.make_source_repository()
        config = self.config()
        runner = ScannerRunner()
        adapters = {
            "cisco": CiscoSkillScannerAdapter(runner=runner, tool_version="1.0"),
            "skillspector": SkillSpectorAdapter(runner=runner, tool_version="1.0"),
        }

        def sync(_repository: str, destination: Path) -> Path:
            subprocess.run(
                ["git", "clone", "--mirror", str(source), str(destination)],
                check=True,
                capture_output=True,
            )
            return destination

        first_rows = plan_repositories(
            self.inventory(revision), included_statuses=config.batch.included_statuses
        )[1].included_rows
        first = prepare_repository(
            config,
            batch_id="batch-source",
            repository="team/repo",
            rows=first_rows,
            mirror_sync=sync,
            adapters=adapters,
        )
        self.assertEqual(runner.call_count, 2)
        ai_results = self.root / "ai-results-source"
        ai_results.mkdir()
        task = first.tasks[0]
        (ai_results / f"{task.task_id}.json").write_text(
            json.dumps(valid_ai_result(task.task_id, revision, task.snapshot.skill_digest)),
            encoding="utf-8",
        )
        finalize_repository(
            config,
            batch_id="batch-source",
            repository_index=first.index_path,
            ai_results_dir=ai_results,
        )

        copy_csv = (
            "skill_name,repo_name,branch,skill_path,lasted_commited,security_reviewed,status\n"
            f"renamed-display,team/repo-copy,main,skills/sample,{revision},否,active\n"
        )
        copy_inventory = parse_inventory_csv(copy_csv, status_mapping={"active": "ACTIVE"})
        copy_rows = plan_repositories(
            copy_inventory, included_statuses=config.batch.included_statuses
        )[0].included_rows
        second = prepare_repository(
            config,
            batch_id="batch-reuse",
            repository="team/repo-copy",
            rows=copy_rows,
            mirror_sync=sync,
            adapters=adapters,
        )
        self.assertEqual(len(second.tasks), 0)
        self.assertEqual(len(second.reused_tasks), 1)
        self.assertEqual(runner.call_count, 2, "reused content must not run either scanner")
        reused_results = finalize_repository(
            config,
            batch_id="batch-reuse",
            repository_index=second.index_path,
            ai_results_dir=self.root / "unused-ai-results",
        )
        self.assertEqual(reused_results[0]["reuse_status"], "RESULT_REUSED")
        self.assertEqual(reused_results[0]["reused_from_task_id"], task.task_id)
        self.assertEqual(reused_results[0]["security_decision"], "PASS")
        self.assertEqual(reused_results[0]["candidate_status"], "EXPORTED_LOCAL")

        changed_policy_config = replace(
            config, ai=replace(config.ai, policy_version="policy-2")
        )
        policy_csv = (
            "skill_name,repo_name,branch,skill_path,lasted_commited,security_reviewed,status\n"
            f"sample,team/repo-policy,main,skills/sample,{revision},否,active\n"
        )
        policy_inventory = parse_inventory_csv(policy_csv, status_mapping={"active": "ACTIVE"})
        policy_rows = plan_repositories(
            policy_inventory, included_statuses=changed_policy_config.batch.included_statuses
        )[0].included_rows
        policy_changed = prepare_repository(
            changed_policy_config,
            batch_id="batch-policy-changed",
            repository="team/repo-policy",
            rows=policy_rows,
            mirror_sync=sync,
            adapters=adapters,
        )
        self.assertEqual(len(policy_changed.reused_tasks), 0)
        self.assertEqual(len(policy_changed.tasks), 1)
        self.assertEqual(runner.call_count, 4, "changed policy must force a full review")

        (source / "skills/sample/guide.txt").write_text("changed bytes\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "-m", "change skill"],
            check=True,
            capture_output=True,
        )
        changed_revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        changed_csv = (
            "skill_name,repo_name,branch,skill_path,lasted_commited,security_reviewed,status\n"
            f"sample,team/repo-changed,main,skills/sample,{changed_revision},否,active\n"
        )
        changed_inventory = parse_inventory_csv(changed_csv, status_mapping={"active": "ACTIVE"})
        changed_rows = plan_repositories(
            changed_inventory, included_statuses=config.batch.included_statuses
        )[0].included_rows
        changed = prepare_repository(
            config,
            batch_id="batch-changed",
            repository="team/repo-changed",
            rows=changed_rows,
            mirror_sync=sync,
            adapters=adapters,
        )
        self.assertEqual(len(changed.reused_tasks), 0)
        self.assertEqual(len(changed.tasks), 1)
        self.assertEqual(runner.call_count, 6, "same root name with changed content must scan")


if __name__ == "__main__":
    unittest.main()
