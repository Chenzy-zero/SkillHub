import tempfile
import unittest
from pathlib import Path

from skill_batch_review.config import ConfigError, load_config


CONFIG = '''
[batch]
inventory_csv = "input/skills.csv"
batch_id_prefix = "baseline"
included_statuses = ["ACTIVE"]

[workspace]
root = "work"
evidence_root = "evidence"
candidate_root = "candidates"
manifest_root = "manifests"

[gerrit]
ssh_url_template = "ssh://{user}@{host}:{port}/{repo_name}.git"
user = "reviewer"
host = "gerrit.intra"
port = 29418
allowed_repositories = ["team/repo"]

[status_mapping]
"有效" = "ACTIVE"
"待审查" = "PENDING"

[quality]
candidate_threshold = 70
max_score = 100

[ai]
skill_path = ".claude/skills/skill-security-review"
result_schema_path = ".claude/skills/skill-security-review/references/review-result.schema.json"
policy_version = "policy-1"
reviewer_model = "intranet-model"

[scanners.cisco]
version = "1.2.3"
command = ["skill-scanner", "scan", "{skill_root}", "--format", "json", "--compact", "--output", "{output_file}"]

[scanners.skillspector]
command = "skillspector scan {skill_root} --no-llm --format json --output {output_file}"

[retry]
max_attempts = 3
backoff_seconds = 5
max_backoff_seconds = 30

[concurrency]
repositories = 2
skills_per_repository = 3
ai_reviews = 2
'''


class ConfigTests(unittest.TestCase):
    def write_config(self, content: str = CONFIG) -> Path:
        temp_dir = Path(self.tempdir.name)
        path = temp_dir / "review.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_load_resolves_paths_and_renders_shell_free_commands(self) -> None:
        config = load_config(self.write_config())
        base = Path(self.tempdir.name)
        self.assertEqual(config.batch.inventory_csv, (base / "input/skills.csv").resolve())
        self.assertEqual(config.batch.included_statuses, ("ACTIVE",))
        self.assertEqual(config.workspace.root, (base / "work").resolve())
        self.assertEqual(
            config.gerrit.repository_url("team/repo", branch="main"),
            "ssh://reviewer@gerrit.intra:29418/team/repo.git",
        )
        self.assertEqual(config.status_mapping.normalize("有效"), "ACTIVE")
        scanner = config.scanner("cisco")
        self.assertEqual(
            scanner.render(skill_root="/tmp/skill", output_file="/tmp/out.json"),
            (
                "skill-scanner", "scan", "/tmp/skill", "--format", "json",
                "--compact", "--output", "/tmp/out.json",
            ),
        )
        self.assertEqual(config.concurrency.repositories, 2)

    def test_unknown_status_and_repository_are_not_accepted(self) -> None:
        config = load_config(self.write_config())
        with self.assertRaises(ConfigError):
            config.status_mapping.normalize("未知")
        with self.assertRaises(ConfigError):
            config.gerrit.repository_url("other/repo")

    def test_repository_path_cannot_escape_or_change_transport(self) -> None:
        config = load_config(self.write_config(CONFIG.replace(
            'allowed_repositories = ["team/repo"]', 'allowed_repositories = []'
        )))
        for value in ("../repo", "/repo", "team//repo", "team\\repo", "ssh://other/repo"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                config.gerrit.repository_url(value)

    def test_missing_repo_placeholder_is_rejected(self) -> None:
        invalid = CONFIG.replace("/{repo_name}.git", "/repo.git")
        with self.assertRaisesRegex(ConfigError, r"must contain \{repo_name\}"):
            load_config(self.write_config(invalid))

    def test_unknown_top_level_section_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown top-level"):
            load_config(self.write_config(CONFIG + "\n[future]\nvalue = true\n"))

    def test_included_status_must_exist_in_status_mapping(self) -> None:
        invalid = CONFIG.replace('included_statuses = ["ACTIVE"]', 'included_statuses = ["DELETED"]')
        with self.assertRaisesRegex(ConfigError, "not produced by status_mapping"):
            load_config(self.write_config(invalid))

    def test_scanner_commands_cannot_enable_unapproved_modes(self) -> None:
        invalid = CONFIG.replace(
            '"--compact", "--output"',
            '"--compact", "--use-llm", "--output"',
        )
        with self.assertRaisesRegex(ConfigError, "approved local static"):
            load_config(self.write_config(invalid))

    def test_evidence_and_candidates_cannot_be_cleaned_with_workspace(self) -> None:
        invalid = CONFIG.replace('evidence_root = "evidence"', 'evidence_root = "work/evidence"')
        with self.assertRaisesRegex(ConfigError, "outside workspace.root"):
            load_config(self.write_config(invalid))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
