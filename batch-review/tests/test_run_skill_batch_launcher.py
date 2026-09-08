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
        self._write_inventory(
            [("id-one", "demo", "team/demo", "main", "skills/demo", "a")]
        )
        self.manifests = self.root / "manifests"
        self.config = self.root / "review.toml"
        self.config.write_text(
            textwrap.dedent(
                f"""
                [batch]
                inventory_csv = "{self.inventory.as_posix()}"
                included_statuses = ["ACTIVE"]
                [workspace]
                root = "{(self.root / 'work').as_posix()}"
                evidence_root = "{(self.root / 'evidence').as_posix()}"
                candidate_root = "{(self.root / 'candidates').as_posix()}"
                manifest_root = "{self.manifests.as_posix()}"
                git_download_root = "{(self.root / 'git_download').as_posix()}"
                skills_root = "{(self.root / 'skills').as_posix()}"
                results_root = "{(self.root / 'results').as_posix()}"
                [gerrit]
                ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
                [status_mapping]
                active = "ACTIVE"
                [quality]
                candidate_threshold = 70
                [ai]
                skill_path = "{(self.root / 'ai-skill').as_posix()}"
                result_schema_path = "{(self.root / 'schema.json').as_posix()}"
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

    def _write_inventory(self, rows):
        lines = [
            "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status,product_line,user_name,user_email"
        ]
        for index, (skill_id, name, repo, branch, path, hex_char) in enumerate(rows, 1):
            lines.append(
                f"{skill_id},{name},{repo},{branch},{path},{hex_char * 40},否,active,product,User{index},user{index}@example.com"
            )
        self.inventory.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_launcher(self, *args):
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *args],
            cwd=BATCH_REVIEW_DIR,
            text=True,
            capture_output=True,
            check=False,
        )

    def _fake_live_report(self, batch_id="batch"):
        output = self.root / "live" / batch_id
        output.mkdir(parents=True, exist_ok=True)
        html = output / "report.html"
        csv_path = output / "current.csv"
        json_path = output / "current.json"
        for path in (html, csv_path, json_path):
            path.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(
            report_status="INTERIM",
            paths=SimpleNamespace(html=html),
            current_csv=csv_path,
            current_json=json_path,
        )

    def _repository_download(self, rows, *, revision_char="c", sizes=None):
        revision = revision_char * 40
        downloads = {}
        sizes = sizes or {}
        for row in rows:
            task_root = self.root / "downloads" / row.trace_values["skill_id"]
            skill_root = task_root / row.skill_name
            skill_root.mkdir(parents=True, exist_ok=True)
            content = b"# test\n"
            (skill_root / "SKILL.md").write_bytes(content)
            size = sizes.get(row.trace_values["skill_id"], len(content))
            entry = PackageEntry("SKILL.md", "file", "100644", size, "d" * 64)
            snapshot = SnapshotResult(
                row.repo_name,
                revision,
                row.skill_path,
                skill_root,
                (entry,),
                "e" * 64,
                package_size_bytes=size,
            )
            downloads[row.source_row_id] = PartialDownload(
                task_root,
                task_root.parent,
                revision,
                snapshot=snapshot,
                transport="whole_repository_archive",
            )
        return SimpleNamespace(
            revision=revision,
            transport="whole_repository_archive",
            skills=downloads,
        )

    def _fake_prepare(self, calls):
        def prepare(_config, *, batch_id, row, downloaded):
            calls.append((row.repo_name, row.trace_values["skill_id"]))
            return SimpleNamespace(
                task_id=launcher_module.skill_task_id(row),
                skill_id=row.trace_values["skill_id"],
                snapshot=downloaded.snapshot,
                index_path=self.root / f"{row.trace_values['skill_id']}.json",
                handoff_path=self.root / f"{row.trace_values['skill_id']}-handoff.json",
                requires_ai=True,
            )
        return prepare

    def test_plan_uses_batch_wide_queue_for_new_batches(self):
        result = self.run_launcher("plan", "--config", str(self.config), "--batch-id", "skills-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            (self.manifests / "skills-1/per-skill-launcher-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "READY")
        self.assertEqual(state["workflow_version"], "repository_archive_v1")
        self.assertEqual(state["ai_policy_version"], "policy-1")
        self.assertEqual(state["ai_queue_mode"], "batch_wide_v2")
        self.assertEqual(state["static_phase_status"], "PENDING")
        self.assertEqual(state["items"][0]["status"], "PENDING")

    def test_report_command_backfills_html_for_completed_batch(self):
        config = load_config(self.config)
        state = launcher_module._new_state(config, "completed-without-report")
        state["status"] = "COMPLETE"
        state["items"][0]["status"] = "COMPLETE"
        launcher_module._save(config, state)
        table_paths = (self.root / "result.csv", self.root / "result.json")
        html_path = self.root / "report.html"
        with (
            mock.patch.object(launcher_module, "write_skill_result_tables", return_value=table_paths),
            mock.patch.object(launcher_module, "write_skill_html_report", return_value=html_path),
        ):
            code = launcher_module._cmd_report(
                SimpleNamespace(config=self.config, batch_id="completed-without-report")
            )
        self.assertEqual(code, 0)
        stored = launcher_module._load_state(config, "completed-without-report")
        self.assertEqual(stored["result_html"], str(html_path))

    def test_started_legacy_batch_cannot_resume_without_frozen_workflow(self):
        config = load_config(self.config)
        state = launcher_module._new_state(config, "legacy-started")
        state.pop("workflow_version")
        state["status"] = "WAITING_FOR_AI"
        state["current_task_id"] = "legacy-task"
        state["items"][0].update({"status": "WAITING_FOR_AI", "task_id": "legacy-task"})
        launcher_module._save(config, state)
        with self.assertRaisesRegex(launcher_module.LauncherError, "旧批次已经开始执行"):
            launcher_module._load_state(config, "legacy-started")

    def test_existing_repository_queue_mode_is_preserved(self):
        config = load_config(self.config)
        state = launcher_module._new_state(config, "repository-v1")
        state["ai_queue_mode"] = "repository_batch_v1"
        launcher_module._save(config, state)
        loaded = launcher_module._load_state(config, "repository-v1")
        self.assertEqual(loaded["ai_queue_mode"], "repository_batch_v1")

    def test_inventory_change_blocks_existing_batch(self):
        config = load_config(self.config)
        state = launcher_module._new_state(config, "inventory-changed")
        launcher_module._save(config, state)
        with self.inventory.open("a", encoding="utf-8") as handle:
            handle.write(
                f"id-two,two,team/demo,main,skills/two,{'b' * 40},否,active,product,Bob,bob@example.com\n"
            )
        with self.assertRaisesRegex(launcher_module.LauncherError, "CSV 内容发生变化"):
            launcher_module._load_state(config, "inventory-changed")

    def test_start_requires_explicit_execution(self):
        result = self.run_launcher("start", "--config", str(self.config), "--batch-id", "skills-2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--execute", result.stderr)

    def test_batch_wide_static_prepares_all_repositories_before_ai_queue(self):
        self._write_inventory(
            [
                ("id-one", "one", "team/one", "main", "skills/one", "a"),
                ("id-two", "two", "team/one", "main", "skills/two", "b"),
                ("id-three", "three", "team/two", "release", "skills/three", "c"),
            ]
        )
        config = load_config(self.config)
        state = launcher_module._new_state(config, "batch-wide")
        document = launcher_module._inventory(config)
        prepare_calls = []
        download_calls = []

        def fake_download(_config, *, batch_id, rows):
            download_calls.append((rows[0].repo_name, rows[0].branch, len(rows)))
            sizes = {"id-one": 10, "id-two": 100, "id-three": 50}
            return self._repository_download(
                rows,
                revision_char="c" if rows[0].repo_name == "team/one" else "d",
                sizes=sizes,
            )

        live = self._fake_live_report("batch-wide")
        with (
            mock.patch.object(launcher_module, "download_repository_skills", side_effect=fake_download),
            mock.patch.object(launcher_module, "prepare_skill", side_effect=self._fake_prepare(prepare_calls)),
            mock.patch.object(launcher_module, "cleanup_repository_download", return_value=True) as cleanup,
            mock.patch.object(launcher_module, "write_skill_result_tables", return_value=(self.root / "r.csv", self.root / "r.json")),
            mock.patch.object(launcher_module, "write_live_batch_report", return_value=live) as live_report,
        ):
            launcher_module._prepare_next(config, state)

        self.assertEqual(
            download_calls,
            [("team/one", "main", 2), ("team/two", "release", 1)],
        )
        self.assertEqual(
            prepare_calls,
            [("team/one", "id-one"), ("team/one", "id-two"), ("team/two", "id-three")],
        )
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(live_report.call_count, 1)
        self.assertIsNone(state.get("active_repository"))
        self.assertEqual(state["static_phase_status"], "COMPLETED")
        self.assertEqual(state["status"], "WAITING_FOR_AI")
        self.assertTrue(all(item["workspace_cleaned"] for item in state["items"]))
        self.assertTrue(all(item["status"] == "WAITING_FOR_AI" for item in state["items"]))

        queue = json.loads(
            (self.manifests / "batch-wide/ai-review-queue.json").read_text(encoding="utf-8")
        )
        self.assertEqual(queue["queue_mode"], "batch_wide_v2")
        self.assertEqual(queue["scope"], "BATCH")
        self.assertEqual(queue["scheduling"], "rolling_largest_first")
        self.assertEqual(queue["max_parallel"], 5)
        self.assertEqual(
            [item["review_size_bytes"] for item in queue["items"]],
            [100, 50, 10],
        )
        self.assertTrue(
            all(
                set(item) == {"task_id", "handoff", "expected_result", "review_size_bytes"}
                for item in queue["items"]
            )
        )
        self.assertEqual(len({item["task_id"] for item in queue["items"]}), 3)

    def test_batch_queue_excludes_existing_results_on_resume(self):
        self._write_inventory(
            [
                ("id-one", "one", "team/one", "main", "skills/one", "a"),
                ("id-two", "two", "team/two", "main", "skills/two", "b"),
            ]
        )
        config = load_config(self.config)
        state = launcher_module._new_state(config, "resume")
        for index, item in enumerate(state["items"], 1):
            item.update(
                {
                    "task_id": f"task-{index}",
                    "status": "WAITING_FOR_AI",
                    "handoff_path": str(self.root / f"handoff-{index}.json"),
                    "ai_result_path": str(self.root / f"ai-{index}.json"),
                    "review_size_bytes": index * 10,
                }
            )
        Path(state["items"][0]["ai_result_path"]).write_text("{}\n", encoding="utf-8")

        launcher_module._activate_batch_queue(config, state, state["items"])
        queue = json.loads(
            (self.manifests / "resume/ai-review-queue.json").read_text(encoding="utf-8")
        )
        self.assertEqual([item["task_id"] for item in queue["items"]], ["task-2"])
        self.assertEqual(state["ai_queue_pending_count"], 1)
        self.assertEqual(state["ai_result_ready_count"], 1)

    def test_batch_advance_imports_ready_results_across_repositories_without_cleanup_gate(self):
        self._write_inventory(
            [
                ("id-one", "one", "team/one", "main", "skills/one", "a"),
                ("id-two", "two", "team/two", "release", "skills/two", "b"),
            ]
        )
        config = load_config(self.config)
        state = launcher_module._new_state(config, "finish-wide")
        state["static_phase_status"] = "COMPLETED"
        for index, item in enumerate(state["items"], 1):
            item.update(
                {
                    "task_id": f"task-{index}",
                    "status": "WAITING_FOR_AI",
                    "index_path": str(self.root / f"index-{index}.json"),
                    "ai_result_path": str(self.root / f"ai-{index}.json"),
                    "handoff_path": str(self.root / f"handoff-{index}.json"),
                    "workspace_cleaned": True,
                }
            )
            Path(item["ai_result_path"]).write_text("{}\n", encoding="utf-8")
        live = self._fake_live_report("finish-wide")
        with (
            mock.patch.object(launcher_module, "finalize_skill") as finalize,
            mock.patch.object(launcher_module, "write_skill_result_tables", return_value=(self.root / "results.csv", self.root / "results.json")),
            mock.patch.object(launcher_module, "write_live_batch_report", return_value=live),
        ):
            launcher_module._finish_current(config, state, confirm_cleanup=False)

        self.assertEqual(finalize.call_count, 2)
        self.assertEqual([item["status"] for item in state["items"]], ["COMPLETE", "COMPLETE"])
        self.assertIsNone(state["current_task_id"])
        self.assertEqual(state["status"], "READY")


if __name__ == "__main__":
    unittest.main()
