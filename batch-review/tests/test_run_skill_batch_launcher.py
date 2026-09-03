from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


BATCH_REVIEW_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = BATCH_REVIEW_DIR / "tools" / "run_skill_batch.py"


class RunSkillBatchLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory.csv"
        self.inventory.write_text(
            "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,product_line,user_name,user_email\n"
            f"id-one,demo,team/demo,main,skills/demo,{'a' * 40},否,active,product,Alice,alice@example.com\n",
            encoding="utf-8",
        )
        self.manifests = self.root / "manifests"
        self.config = self.root / "review.toml"
        self.config.write_text(
            textwrap.dedent(
                f"""
                [batch]
                inventory_csv = "{self.inventory}"
                included_statuses = ["ACTIVE"]
                [workspace]
                root = "{self.root / 'work'}"
                evidence_root = "{self.root / 'evidence'}"
                candidate_root = "{self.root / 'candidates'}"
                manifest_root = "{self.manifests}"
                git_download_root = "{self.root / 'git_download'}"
                skills_root = "{self.root / 'skills'}"
                results_root = "{self.root / 'results'}"
                [gerrit]
                ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
                [status_mapping]
                active = "ACTIVE"
                [quality]
                candidate_threshold = 70
                [ai]
                skill_path = "{self.root / 'ai-skill'}"
                result_schema_path = "{self.root / 'schema.json'}"
                policy_version = "policy-1"
                reviewer_model = "intranet-model"
                [scanners.cisco]
                version = "1.0"
                command = ["skill-scanner", "scan", "{{skill_root}}", "--format", "json", "--compact", "--output", "{{output_file}}"]
                [scanners.skillspector]
                version = "1.0"
                command = ["skillspector", "scan", "{{skill_root}}", "--no-llm", "--format", "json", "--output", "{{output_file}}"]
                """
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_launcher(self, *args):
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *args],
            cwd=BATCH_REVIEW_DIR.parent,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_plan_uses_skill_items_and_never_accesses_network(self):
        result = self.run_launcher("plan", "--config", str(self.config), "--batch-id", "skills-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            (self.manifests / "skills-1/per-skill-launcher-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["items"][0]["skill_id"], "id-one")
        self.assertEqual(state["items"][0]["status"], "PENDING")

    def test_start_requires_explicit_execution(self):
        result = self.run_launcher("start", "--config", str(self.config), "--batch-id", "skills-2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--execute", result.stderr)


if __name__ == "__main__":
    unittest.main()
