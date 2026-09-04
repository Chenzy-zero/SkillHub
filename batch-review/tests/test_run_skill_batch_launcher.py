from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skill_batch_review.config import load_config
from skill_batch_review.per_skill import PartialDownload
from skill_batch_review.snapshot import PackageEntry, SnapshotResult


BATCH_REVIEW_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = BATCH_REVIEW_DIR / "tools" / "run_skill_batch.py"


def _load_launcher_module():
    spec = importlib.util.spec_from_file_location("test_run_skill_batch_module", LAUNCHER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {LAUNCHER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher_module = _load_launcher_module()


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

    def test_repository_is_downloaded_once_while_skills_advance_sequentially(self):
        self.inventory.write_text(
            "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,product_line,user_name,user_email\n"
            f"id-one,one,team/demo,main,skills/one,{'a' * 40},否,active,product,Alice,alice@example.com\n"
            f"id-two,two,team/demo,main,skills/two,{'b' * 40},否,active,product,Bob,bob@example.com\n",
            encoding="utf-8",
        )
        config = load_config(self.config)
        state = launcher_module._new_state(config, "repo-sequential")
        document = launcher_module._inventory(config)
        revision = "c" * 40
        downloads = {}
        for row in document.rows:
            task_root = self.root / "downloads" / row.trace_values["skill_id"]
            skill_root = task_root / row.skill_name
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text("# test\n", encoding="utf-8")
            entry = PackageEntry("SKILL.md", "file", "100644", 7, "d" * 64)
            snapshot = SnapshotResult(
                row.repo_name,
                revision,
                row.skill_path,
                skill_root,
                (entry,),
                "e" * 64,
            )
            downloads[row.source_row_id] = PartialDownload(
                task_root,
                task_root.parent,
                revision,
                snapshot=snapshot,
                transport="whole_repository_archive",
            )
        repository_download = SimpleNamespace(
            revision=revision,
            transport="whole_repository_archive",
            skills=downloads,
        )
        prepare_calls = []

        def fake_prepare(_config, *, batch_id, row, downloaded):
            prepare_calls.append(row.trace_values["skill_id"])
            return SimpleNamespace(
                task_id=launcher_module.skill_task_id(row),
                skill_id=row.trace_values["skill_id"],
                snapshot=downloaded.snapshot,
                index_path=self.root / f"{row.trace_values['skill_id']}.json",
                handoff_path=self.root / f"{row.trace_values['skill_id']}-handoff.json",
                requires_ai=True,
            )

        with (
            mock.patch.object(launcher_module, "download_repository_skills", return_value=repository_download) as download,
            mock.patch.object(launcher_module, "prepare_skill", side_effect=fake_prepare),
            mock.patch.object(launcher_module, "cleanup_repository_download", return_value=True) as cleanup,
        ):
            launcher_module._prepare_next(config, state)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(prepare_calls, ["id-one"])
            first = state["items"][0]
            first["status"] = "COMPLETE"
            state["current_task_id"] = None
            state["status"] = "READY"

            launcher_module._prepare_next(config, state)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(prepare_calls, ["id-one", "id-two"])
            second = state["items"][1]
            second["status"] = "COMPLETE"
            state["current_task_id"] = None
            state["status"] = "READY"

            launcher_module._prepare_next(config, state)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(cleanup.call_count, 1)
            self.assertEqual(state["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
