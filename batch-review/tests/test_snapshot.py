import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from skill_batch_review.snapshot import (
    GitSourceError,
    PackageEntry,
    SnapshotError,
    SnapshotLimits,
    UnsafePathError,
    calculate_skill_digest,
    canonical_manifest_json,
    export_skill_snapshot,
)


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "snapshot-tests@example.invalid")
        self.git("config", "user.name", "snapshot-tests")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if completed.returncode:
            raise RuntimeError(f"stdout={completed.stdout!r} stderr={completed.stderr!r}")
        return completed.stdout.strip()

    def commit(self, message: str = "snapshot fixture") -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def make_basic_skill(self) -> None:
        skill = self.repo / "skills" / "demo"
        (skill / "scripts").mkdir(parents=True)
        (skill / "nested").mkdir()
        (skill / "SKILL.md").write_text("# committed skill\n", encoding="utf-8")
        (skill / ".hidden").write_text("hidden\n", encoding="utf-8")
        script = skill / "scripts" / "run.sh"
        script.write_text("#!/bin/sh\necho should-not-run\n", encoding="utf-8")
        script.chmod(0o755)
        (skill / "nested" / "SKILL.md").write_text("# nested\n", encoding="utf-8")
        (skill / "asset.bin").write_bytes(b"\x00\x01binary\xff")
        (skill / "safe-link").symlink_to("SKILL.md")
        (skill / "outside-link").symlink_to("/etc/passwd")

    def test_exports_git_bytes_without_checkout_and_writes_manifest(self) -> None:
        self.make_basic_skill()
        revision = self.commit()

        # A dirty worktree must not affect a snapshot of the explicit commit.
        (self.repo / "skills" / "demo" / "SKILL.md").write_text(
            "# worktree content\n", encoding="utf-8"
        )
        destination = self.root / "snapshot"
        manifest_path = self.root / "evidence" / "package-manifest.json"
        result = export_skill_snapshot(
            self.repo, revision, "skills/demo", destination, manifest_path=manifest_path
        )

        self.assertEqual(result.source_revision, revision)
        self.assertEqual(result.skill_path, "skills/demo")
        self.assertEqual(
            (destination / "SKILL.md").read_text(encoding="utf-8"),
            "# committed skill\n",
        )
        self.assertTrue((destination / ".hidden").exists())
        self.assertEqual((destination / "scripts" / "run.sh").stat().st_mode & 0o777, 0o755)
        self.assertFalse((destination / "safe-link").exists())
        self.assertFalse((destination / "outside-link").exists())

        by_path = {entry.relative_path: entry for entry in result.entries}
        self.assertEqual(by_path["asset.bin"].file_type, "binary")
        self.assertEqual(by_path["safe-link"].file_type, "symlink")
        self.assertEqual(by_path["safe-link"].symlink_target, "SKILL.md")
        self.assertIsNone(by_path["safe-link"].sha256)
        self.assertEqual(by_path["outside-link"].symlink_target, "/etc/passwd")
        self.assertEqual(result.skill_digest, calculate_skill_digest(result.entries))
        self.assertFalse(result.coverage_complete)
        issue_codes = {issue.code for issue in result.coverage_issues}
        self.assertTrue({"BINARY_FILE", "NESTED_SKILL_MD", "SYMLINK_NOT_FOLLOWED"} <= issue_codes)
        self.assertIn("SYMLINK_OUTSIDE_ROOT", issue_codes)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["skill_digest"], result.skill_digest)
        self.assertFalse(manifest["coverage_complete"])
        self.assertEqual(manifest["entries"], [entry.to_dict() for entry in result.entries])

    def test_digest_is_sorted_and_only_uses_path_type_mode_and_hash_or_target(self) -> None:
        self.make_basic_skill()
        revision = self.commit()
        result = export_skill_snapshot(self.repo, revision, "skills/demo", self.root / "snapshot")

        canonical = json.loads(canonical_manifest_json(result.entries).decode("utf-8"))
        self.assertEqual(
            [item["relative_path"] for item in canonical],
            sorted((item["relative_path"] for item in canonical), key=lambda value: value.encode()),
        )
        self.assertTrue(all(set(item) == {"relative_path", "type", "mode", "hash_or_target"} for item in canonical))
        self.assertEqual(
            result.skill_digest,
            hashlib.sha256(canonical_manifest_json(result.entries)).hexdigest(),
        )

    def test_file_classification_label_does_not_change_content_identity(self) -> None:
        common = {
            "relative_path": "asset.dat",
            "mode": "100644",
            "size": 3,
            "sha256": "a" * 64,
            "git_object_id": "b" * 40,
        }
        text_entry = PackageEntry(file_type="file", **common)
        binary_entry = PackageEntry(file_type="binary", **common)
        lfs_entry = PackageEntry(file_type="lfs_pointer", **common)
        self.assertEqual(calculate_skill_digest([text_entry]), calculate_skill_digest([binary_entry]))
        self.assertEqual(calculate_skill_digest([text_entry]), calculate_skill_digest([lfs_entry]))

    def test_detects_lfs_pointer_and_submodule_as_incomplete(self) -> None:
        skill = self.repo / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        (skill / "large.bin").write_bytes(
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:" + b"a" * 64 + b"\n"
            b"size 999\n"
        )
        self.commit("base")
        # Create a real commit object in this temporary repository so Git's
        # normal commit validation accepts the synthetic submodule entry.
        submodule_commit = self.git("commit-tree", self.git("rev-parse", "HEAD^{tree}"), "-m", "vendor")
        self.git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000," + submodule_commit + ",skills/demo/vendor",
        )
        # Do not run ``git add -A`` here: it would stage the missing working
        # tree path as a deletion and erase the synthetic submodule entry.
        self.git("commit", "-qm", "submodule entry")
        revision = self.git("rev-parse", "HEAD")

        result = export_skill_snapshot(self.repo, revision, "skills/demo", self.root / "snapshot")
        by_path = {entry.relative_path: entry for entry in result.entries}
        self.assertEqual(by_path["large.bin"].file_type, "lfs_pointer")
        self.assertEqual(by_path["vendor"].file_type, "submodule")
        self.assertEqual(by_path["vendor"].size, 0)
        issue_codes = {issue.code for issue in result.coverage_issues}
        self.assertIn("LFS_POINTER", issue_codes)
        self.assertIn("SUBMODULE_NOT_INCLUDED", issue_codes)
        self.assertFalse(result.coverage_complete)
        self.assertFalse((self.root / "snapshot" / "vendor").exists())

    def test_limits_record_incomplete_entries_without_hashing_them(self) -> None:
        skill = self.repo / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("12345", encoding="utf-8")
        (skill / "other.txt").write_text("67890", encoding="utf-8")
        revision = self.commit()

        result = export_skill_snapshot(
            self.repo,
            revision,
            "skills/demo",
            self.root / "snapshot",
            limits=SnapshotLimits(max_file_size_bytes=4, max_package_size_bytes=100),
        )
        self.assertFalse(result.coverage_complete)
        self.assertTrue({"FILE_TOO_LARGE", "SKILL_MD_UNAVAILABLE"} <= {i.code for i in result.coverage_issues})
        self.assertTrue(all(entry.sha256 is None for entry in result.entries))
        self.assertFalse((self.root / "snapshot" / "SKILL.md").exists())
        self.assertRegex(result.skill_digest, r"^[0-9a-f]{64}$")

    def test_package_limit_and_missing_skill_are_not_silent(self) -> None:
        skill = self.repo / "not-a-skill"
        skill.mkdir()
        (skill / "README.txt").write_text("1234", encoding="utf-8")
        revision = self.commit()
        result = export_skill_snapshot(
            self.repo,
            revision,
            "not-a-skill",
            self.root / "snapshot",
            limits=SnapshotLimits(max_file_size_bytes=100, max_package_size_bytes=2),
        )
        issue_codes = {issue.code for issue in result.coverage_issues}
        self.assertIn("PACKAGE_TOO_LARGE", issue_codes)
        self.assertIn("PACKAGE_LIMIT_EXCEEDED", issue_codes)
        self.assertIn("MISSING_SKILL_MD", issue_codes)
        self.assertFalse(result.coverage_complete)

    def test_rejects_refs_and_unsafe_skill_paths(self) -> None:
        self.make_basic_skill()
        revision = self.commit()
        with self.assertRaises(GitSourceError):
            export_skill_snapshot(self.repo, "HEAD", "skills/demo", self.root / "bad")
        with self.assertRaises((ValueError, UnsafePathError)):
            export_skill_snapshot(self.repo, revision, "../outside", self.root / "bad2")
        self.assertFalse((self.root / "bad").exists())

    def test_literal_pathspec_handles_metacharacters(self) -> None:
        skill = self.repo / "skills" / "[literal]"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# literal\n", encoding="utf-8")
        revision = self.commit()
        result = export_skill_snapshot(
            self.repo, revision, "skills/[literal]", self.root / "snapshot"
        )
        self.assertEqual((self.root / "snapshot" / "SKILL.md").read_text(), "# literal\n")
        self.assertTrue(result.coverage_complete)
if __name__ == "__main__":
    unittest.main()
