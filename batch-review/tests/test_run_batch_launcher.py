"""Tests for the operator-facing repository-at-a-time launcher."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


BATCH_REVIEW_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = BATCH_REVIEW_DIR / "tools" / "run_batch.py"


class RunBatchLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory.csv"
        self.inventory.write_text(
            "skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status\n"
            f"demo,team/demo,main,skills/demo,{'a' * 40},否,active\n",
            encoding="utf-8",
        )
        self.manifests = self.root / "manifests"
        self.config = self.root / "review.toml"
        self.config.write_text(
            textwrap.dedent(
                f"""
                [batch]
                inventory_csv = "{self.inventory.as_posix()}"
                batch_id_prefix = "test"
                included_statuses = ["ACTIVE"]

                [workspace]
                root = "{(self.root / 'work').as_posix()}"
                evidence_root = "{(self.root / 'evidence').as_posix()}"
                candidate_root = "{(self.root / 'candidates').as_posix()}"
                manifest_root = "{self.manifests.as_posix()}"

                [gerrit]
                ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
                user = "git"
                host = "gerrit.example.com"
                port = 29418
                allowed_repositories = []

                [status_mapping]
                "active" = "ACTIVE"

                [quality]
                candidate_threshold = 70
                max_score = 100

                [ai]
                skill_path = "{(self.root / 'ai-skill').as_posix()}"
                result_schema_path = "{(self.root / 'schema.json').as_posix()}"
                policy_version = "policy-v1"
                reviewer_model = "intranet-model"

                [scanners.cisco]
                enabled = true
                version = "2.0.13"
                command = ["skill-scanner", "scan", "{{skill_root}}", "--format", "json", "--compact", "--output", "{{output_file}}"]

                [scanners.skillspector]
                enabled = true
                version = "2.5.1"
                command = ["skillspector", "scan", "{{skill_root}}", "--no-llm", "--format", "json", "--output", "{{output_file}}"]

                [retry]
                max_attempts = 3
                backoff_seconds = 1
                max_backoff_seconds = 2

                [concurrency]
                repositories = 1
                skills_per_repository = 1
                ai_reviews = 1
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_launcher(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *arguments],
            cwd=BATCH_REVIEW_DIR.parent,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_plan_writes_a_bound_local_state_without_execution(self):
        result = self.run_launcher(
            "plan", "--config", str(self.config), "--batch-id", "plan-001"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state_path = self.manifests / "plan-001" / "launcher-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["repositories"][0]["name"], "team/demo")
        self.assertEqual(state["repositories"][0]["status"], "PENDING")
        self.assertEqual(state["source_row_count"], 1)
        self.assertEqual(state["config_path"], str(self.config.resolve()))

    def test_start_requires_explicit_execute_confirmation(self):
        result = self.run_launcher(
            "start", "--config", str(self.config), "--batch-id", "run-001"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--execute", result.stderr)
        self.assertFalse((self.manifests / "run-001").exists())

    def test_status_reports_repository_counts(self):
        planned = self.run_launcher(
            "plan", "--config", str(self.config), "--batch-id", "status-001"
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        result = self.run_launcher(
            "status", "--config", str(self.config), "--batch-id", "status-001"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "READY")
        self.assertEqual(value["repository_status_counts"], {"PENDING": 1})


if __name__ == "__main__":
    unittest.main()
