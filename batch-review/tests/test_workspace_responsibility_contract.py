from __future__ import annotations

import re
import tempfile
import textwrap
import unittest
from pathlib import Path

from skill_batch_review.config import load_config
from skill_batch_review.path_compat import CANONICAL_AI_SCHEMA, CANONICAL_AI_SKILL


ROOT = Path(__file__).resolve().parents[1]


def _minimal_config(
    root: Path,
    *,
    skill_path: Path,
    schema_path: Path,
) -> Path:
    inventory = root / "inventory.csv"
    inventory.write_text(
        "skill_id,skill_name,repo_name,branch,skill_path,latest_commitid,security_reviewed,status\n"
        f"id-1,demo,team/demo,main,skills/demo,{'a' * 40},否,active\n",
        encoding="utf-8",
    )
    config = root / "review.toml"
    config.write_text(
        textwrap.dedent(
            f"""
            [batch]
            inventory_csv = "{inventory.as_posix()}"
            included_statuses = ["ACTIVE"]
            [workspace]
            root = "{(root / 'work').as_posix()}"
            git_download_root = "{(root / 'downloads').as_posix()}"
            manifest_root = "{(root / 'manifests').as_posix()}"
            skills_root = "{(root / 'skills').as_posix()}"
            evidence_root = "{(root / 'evidence').as_posix()}"
            candidate_root = "{(root / 'candidates').as_posix()}"
            results_root = "{(root / 'results').as_posix()}"
            [gerrit]
            ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
            [status_mapping]
            active = "ACTIVE"
            [quality]
            candidate_threshold = 70
            max_score = 100
            [ai]
            skill_path = "{skill_path.as_posix()}"
            result_schema_path = "{schema_path.as_posix()}"
            [scanners.cisco]
            version = "1"
            command = ["cisco", "scan", "{{skill_root}}", "--format", "json", "--compact", "--output", "{{output_file}}"]
            [scanners.skillspector]
            version = "1"
            command = ["skillspector", "scan", "{{skill_root}}", "--no-llm", "--format", "json", "--output", "{{output_file}}"]
            """
        ),
        encoding="utf-8",
    )
    return config


class WorkspaceResponsibilityContractTests(unittest.TestCase):
    def test_all_templates_use_canonical_agents_policy(self) -> None:
        for name in (
            "review.example.toml",
            "review.company.example.toml",
            "review.github.example.toml",
        ):
            with self.subTest(name=name):
                content = (ROOT / "config" / name).read_text(encoding="utf-8")
                self.assertIn("../.agents/skills/skill-security-review", content)
                self.assertNotIn('skill_path = "../skills/skill-security-review"', content)

    def test_known_legacy_absolute_policy_path_redirects_without_rewriting_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            legacy = (ROOT / "skills" / "skill-security-review").resolve()
            config_path = _minimal_config(
                temp,
                skill_path=legacy,
                schema_path=legacy / "references" / "review-result.schema.json",
            )
            before = config_path.read_text(encoding="utf-8")
            config = load_config(config_path)
            after = config_path.read_text(encoding="utf-8")
            self.assertEqual(config.ai.skill_path, CANONICAL_AI_SKILL)
            self.assertEqual(config.ai.result_schema_path, CANONICAL_AI_SCHEMA)
            self.assertEqual(after, before)

    def test_custom_external_policy_path_is_not_redirected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            custom = temp / "custom-policy"
            custom.mkdir()
            (custom / "SKILL.md").write_text("---\nname: custom\n---\n", encoding="utf-8")
            references = custom / "references"
            references.mkdir()
            schema = references / "review-result.schema.json"
            schema.write_text("{}\n", encoding="utf-8")
            config = load_config(
                _minimal_config(temp, skill_path=custom, schema_path=schema)
            )
            self.assertEqual(config.ai.skill_path, custom.resolve())
            self.assertEqual(config.ai.result_schema_path, schema.resolve())

    def test_readme_local_markdown_links_exist(self) -> None:
        for document in (ROOT / "README.md", ROOT / "AGENTS.md"):
            content = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
                if "://" in target or target.startswith("#"):
                    continue
                clean = target.split("#", 1)[0]
                self.assertTrue((document.parent / clean).exists(), f"{document.name}: {target}")

    def test_workspace_roles_and_unique_human_entry_are_documented(self) -> None:
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        for role in ("Runtime", "State", "Archive", "Evidence", "Reports"):
            self.assertIn(role, content)
        for path in (
            "git_download",
            "manifests",
            "skills",
            "restricted-evidence",
            "private-candidates",
            "results/<batch-id>",
        ):
            self.assertIn(path, content)
        self.assertIn("results/<batch-id>/skill-security-review-report.html", content)
        self.assertIn("Ledger", content)
        self.assertIn("Standard report exports", content)

    def test_regression_suite_contains_every_refactor_boundary(self) -> None:
        required = {
            "test_per_skill_phase_state.py",
            "test_live_report.py",
            "test_run_skill_batch_launcher.py",
            "test_dispatch_lease.py",
            "test_completion_import.py",
            "test_evidence_index.py",
            "test_evidence_index_migration.py",
            "test_project_setup.py",
        }
        actual = {path.name for path in (ROOT / "tests").glob("test_*.py")}
        self.assertTrue(required <= actual, sorted(required - actual))


if __name__ == "__main__":
    unittest.main()
