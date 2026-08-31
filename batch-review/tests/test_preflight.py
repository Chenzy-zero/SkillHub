import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_batch_review.config import load_config
from skill_batch_review.preflight import review_preflight


def config_text(root: Path) -> str:
    return f'''
[batch]
inventory_csv = "{root / 'inventory.csv'}"
included_statuses = ["ACTIVE"]
[workspace]
root = "{root / 'work'}"
evidence_root = "{root / 'evidence'}"
candidate_root = "{root / 'candidates'}"
manifest_root = "{root / 'manifests'}"
[gerrit]
host = "gerrit.intra"
ssh_url_template = "ssh://{{user}}@{{host}}:{{port}}/{{repo_name}}.git"
[status_mapping]
active = "ACTIVE"
[quality]
candidate_threshold = 70
[ai]
skill_path = "{root / 'ai-skill'}"
result_schema_path = "{root / 'ai-skill/schema.json'}"
policy_version = "policy-1"
reviewer_model = "intranet-model"
[scanners.cisco]
version = "1.0"
command = ["skill-scanner", "scan", "{{skill_root}}", "--format", "json", "--compact", "--output", "{{output_file}}"]
[scanners.skillspector]
version = "1.0"
command = ["skillspector", "scan", "{{skill_root}}", "--no-llm", "--format", "json", "--output", "{{output_file}}"]
'''


class PreflightTests(unittest.TestCase):
    def test_ready_when_local_inputs_are_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "inventory.csv").write_text("header\n", encoding="utf-8")
            (root / "ai-skill").mkdir()
            (root / "ai-skill/SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
            (root / "ai-skill/schema.json").write_text("{}", encoding="utf-8")
            config_path = root / "review.toml"
            config_path.write_text(config_text(root), encoding="utf-8")
            with patch("skill_batch_review.preflight.shutil.which", return_value="/bin/tool"):
                self.assertEqual(review_preflight(load_config(config_path)), ())

    def test_placeholders_and_missing_files_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = config_text(root).replace('host = "gerrit.intra"', 'host = "gerrit.example.com"')
            text = text.replace('policy_version = "policy-1"', 'policy_version = "pin-in-deployment"')
            text = text.replace('reviewer_model = "intranet-model"', 'reviewer_model = "set-company-intranet-model-id"')
            config_path = root / "review.toml"
            config_path.write_text(text, encoding="utf-8")
            with patch("skill_batch_review.preflight.shutil.which", return_value=None):
                codes = {issue.code for issue in review_preflight(load_config(config_path))}
            self.assertIn("INVENTORY_MISSING", codes)
            self.assertIn("GERRIT_NOT_CONFIGURED", codes)
            self.assertIn("SCANNER_NOT_FOUND", codes)
            self.assertIn("POLICY_NOT_PINNED", codes)
            self.assertIn("AI_MODEL_NOT_CONFIGURED", codes)


if __name__ == "__main__":
    unittest.main()
