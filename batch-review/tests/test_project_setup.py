from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


BATCH_REVIEW_DIR = Path(__file__).resolve().parents[1]


def _load_tool(name: str):
    path = BATCH_REVIEW_DIR / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


init_project = _load_tool("init_project")
project_status = _load_tool("project_status")


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.operator = self.root / "operator-state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def _ready_config(self) -> tuple[Path, Path]:
        inventory = self.root / "skills.csv"
        inventory.write_text(
            "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status\n"
            f"id-1,demo,team/demo,main,skills/demo,{'a' * 40},否,新增\n",
            encoding="utf-8",
        )
        ai_skill = self.root / "ai-skill"
        ai_skill.mkdir()
        (ai_skill / "SKILL.md").write_text("---\nname: review\n---\n", encoding="utf-8")
        schema = self.root / "schema.json"
        schema.write_text("{}\n", encoding="utf-8")
        cisco = self.root / "skill-scanner"
        spector = self.root / "skillspector"
        cisco.write_text("tool", encoding="utf-8")
        spector.write_text("tool", encoding="utf-8")
        manifests = self.root / "manifests"
        config = self.root / "review.local.toml"
        config.write_text(
            textwrap.dedent(
                f"""
                [batch]
                inventory_csv = "{inventory}"
                batch_id_prefix = "test"
                included_statuses = ["ACTIVE"]
                [workspace]
                root = "{self.root / 'work'}"
                evidence_root = "{self.root / 'evidence'}"
                candidate_root = "{self.root / 'candidates'}"
                manifest_root = "{manifests}"
                git_download_root = "{self.root / 'downloads'}"
                skills_root = "{self.root / 'skills'}"
                results_root = "{self.root / 'results'}"
                [gerrit]
                ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
                user = "reader"
                host = "gerrit.internal"
                port = 29418
                allowed_repositories = ["team/demo"]
                [status_mapping]
                "新增" = "ACTIVE"
                [quality]
                candidate_threshold = 70
                max_score = 100
                [ai]
                skill_path = "{ai_skill}"
                result_schema_path = "{schema}"
                policy_version = "policy-v1"
                reviewer_model = "intranet-model"
                [scanners.cisco]
                enabled = true
                version = "2.0.13"
                command = ["{cisco}", "scan", "{{skill_root}}", "--format", "json", "--compact", "--output", "{{output_file}}"]
                [scanners.skillspector]
                enabled = true
                version = "2.5.1"
                command = ["{spector}", "scan", "{{skill_root}}", "--no-llm", "--format", "json", "--output", "{{output_file}}"]
                """
            ),
            encoding="utf-8",
        )
        return config, manifests

    def _write_operator(self, config: Path, batch_id: str | None = None) -> None:
        self.operator.write_text(
            json.dumps({"config_path": str(config), "batch_id": batch_id}),
            encoding="utf-8",
        )

    def test_not_initialized_has_one_safe_next_action(self):
        status = project_status.inspect_project(operator_state_path=self.operator)
        self.assertEqual(status.next_action, "INITIALIZE")
        self.assertEqual(status.state, "NOT_INITIALIZED")

    def test_initializer_creates_real_local_config_and_never_overwrites_by_default(self):
        config = self.root / "review.local.toml"
        created_path, created = init_project.initialize(
            profile="github", config_path=config, operator_state_path=self.operator
        )
        self.assertTrue(created)
        self.assertEqual(created_path, config.resolve())
        original = config.read_text(encoding="utf-8")
        self.assertIn("github_skill_summary.csv", original)
        config.write_text("user-owned\n", encoding="utf-8")
        _, created_again = init_project.initialize(
            profile="github", config_path=config, operator_state_path=self.operator
        )
        self.assertFalse(created_again)
        self.assertEqual(config.read_text(encoding="utf-8"), "user-owned\n")

    def test_ready_project_moves_from_plan_to_start(self):
        config, manifests = self._ready_config()
        self._write_operator(config)
        ready = project_status.inspect_project(operator_state_path=self.operator)
        self.assertEqual(ready.next_action, "PLAN")

        batch_id = "test-1"
        state_dir = manifests / batch_id
        state_dir.mkdir(parents=True)
        (state_dir / "per-skill-launcher-state.json").write_text(
            json.dumps({"status": "READY", "current_task_id": None, "items": []}),
            encoding="utf-8",
        )
        self._write_operator(config, batch_id)
        planned = project_status.inspect_project(operator_state_path=self.operator)
        self.assertEqual(planned.next_action, "START")

    def test_waiting_ai_points_to_dedicated_review_skill(self):
        config, manifests = self._ready_config()
        batch_id = "test-ai"
        state_dir = manifests / batch_id
        state_dir.mkdir(parents=True)
        state = {
            "status": "WAITING_FOR_AI",
            "current_task_id": "task-1",
            "items": [
                {
                    "task_id": "task-1",
                    "skill_id": "id-1",
                    "skill_name": "demo",
                    "ai_result_path": str(self.root / "missing-ai-result.json"),
                }
            ],
        }
        (state_dir / "per-skill-launcher-state.json").write_text(json.dumps(state), encoding="utf-8")
        self._write_operator(config, batch_id)
        status = project_status.inspect_project(operator_state_path=self.operator)
        self.assertEqual(status.next_action, "AI_REVIEW")
        self.assertIn("/ask-cc", status.next_instruction)

    def test_duplicate_skill_id_is_reported_before_plan(self):
        config, _ = self._ready_config()
        inventory = self.root / "skills.csv"
        with inventory.open("a", encoding="utf-8") as handle:
            handle.write(f"id-1,demo2,team/demo,main,skills/demo2,{'b' * 40},否,新增\n")
        self._write_operator(config)
        status = project_status.inspect_project(operator_state_path=self.operator)
        self.assertEqual(status.state, "INVENTORY_INVALID")
        self.assertEqual(status.next_action, "EDIT_CONFIG")
        self.assertEqual(status.issues[0]["code"], "SKILL_ID_DUPLICATE")

    def test_ask_cc_skill_is_discoverable_and_uses_read_only_status(self):
        skill_root = BATCH_REVIEW_DIR.parent / ".claude" / "skills" / "ask-cc"
        content = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: ask-cc", content)
        self.assertIn("project_status.py --json", content)
        self.assertIn("Do not run `init`, `plan`, `start`, `advance`", content)
        evals = json.loads((skill_root / "evals" / "evals.json").read_text(encoding="utf-8"))
        self.assertEqual(evals["skill_name"], "ask-cc")
        self.assertEqual(len(evals["evals"]), 3)


if __name__ == "__main__":
    unittest.main()
