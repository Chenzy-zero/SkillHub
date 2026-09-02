from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "discover_git_skills.py"
SPEC = importlib.util.spec_from_file_location("discover_git_skills", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DiscoverGitSkillsTests(unittest.TestCase):
    def test_discovers_only_committed_skill_anchors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            skill = root / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: demo-security\ndescription: test\n---\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "add skill"], check=True)
            (root / "uncommitted" / "SKILL.md").parent.mkdir()
            (root / "uncommitted" / "SKILL.md").write_text("name: ignored", encoding="utf-8")

            revision, rows = MODULE.discover(
                root, repo_name="owner/project", branch="main", revision="HEAD"
            )
            self.assertEqual(len(revision), 40)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["skill_name"], "demo-security")
            self.assertEqual(rows[0]["skill_path"], "skills/demo")
            self.assertEqual(rows[0]["latest_commitid"], revision)

            output = root / "inventory.csv"
            MODULE.write_csv(output, rows)
            with output.open(encoding="utf-8", newline="") as handle:
                imported = list(csv.DictReader(handle))
            self.assertEqual(imported, rows)


if __name__ == "__main__":
    unittest.main()
