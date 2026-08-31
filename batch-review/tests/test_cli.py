import json
import tempfile
import unittest
from pathlib import Path

from skill_batch_review.cli import main


class CliTests(unittest.TestCase):
    def test_init_batch_uses_inventory_document_and_stays_plan_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "inventory.csv"
            csv_path.write_text(
                "skill_name,repo_name,branch,skill_path,lasted_commited,security_reviewed,status\n"
                "demo,team/repo,refs/heads/main,skills/demo,"
                + "a" * 40
                + ",否,active\n"
                "demo,team/repo,refs/heads/main,skills/demo,"
                + "a" * 40
                + ",否,active\n",
                encoding="utf-8",
            )
            config_path = root / "review.toml"
            config_path.write_text(
                """
[batch]
inventory_csv = "inventory.csv"
included_statuses = ["ACTIVE"]
[workspace]
root = "work"
evidence_root = "evidence"
candidate_root = "candidates"
manifest_root = "manifests"
[gerrit]
ssh_url_template = "ssh://{user}@{host}:{port}/{repo_name}.git"
[status_mapping]
active = "ACTIVE"
[quality]
candidate_threshold = 70
[scanners.cisco]
command = ["skill-scanner", "scan", "{skill_root}", "--format", "json", "--compact", "--output", "{output_file}"]
[scanners.skillspector]
command = ["skillspector", "scan", "{skill_root}", "--no-llm", "--format", "json", "--output", "{output_file}"]
""",
                encoding="utf-8",
            )
            output = root / "manifest.json"
            exit_code = main(
                [
                    "init-batch",
                    str(config_path),
                    "--batch-id",
                    "test-batch",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(manifest["network_accessed"])
            self.assertFalse(manifest["scanners_executed"])
            self.assertEqual(manifest["source_row_count"], 2)
            self.assertEqual(manifest["execution_record_count"], 1)
            self.assertEqual(manifest["exact_duplicate_count"], 1)
            self.assertRegex(manifest["inventory_csv_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(manifest["records"][0]["source_row_numbers"], [2, 3])

            repository_plan = root / "repository-plan.json"
            exit_code = main(
                [
                    "plan-repositories",
                    str(config_path),
                    "--output",
                    str(repository_plan),
                ]
            )
            self.assertEqual(exit_code, 0)
            plan = json.loads(repository_plan.read_text(encoding="utf-8"))
            self.assertFalse(plan["network_accessed"])
            self.assertFalse(plan["scanners_executed"])
            self.assertEqual(plan["repositories_to_prepare"], ["team/repo"])


if __name__ == "__main__":
    unittest.main()
