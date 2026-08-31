import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from skill_batch_review.artifacts import (
    ArtifactIntegrityError,
    ArtifactPathError,
    ArtifactWriteError,
    CandidateIntegrityError,
    CandidateNotEligibleError,
    DigestMismatchError,
    EvidenceStore,
    export_private_candidate,
)
from skill_batch_review.snapshot import PackageEntry, calculate_skill_digest


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.evidence_root = self.root / "restricted-evidence"
        self.candidate_root = self.root / "private-candidates"
        self.source_root = self.root / "skill-snapshot"
        self.source_root.mkdir()
        (self.source_root / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (self.source_root / "scripts").mkdir()
        script = self.source_root / "scripts" / "run.sh"
        script.write_text("#!/bin/sh\nprintf safe\n", encoding="utf-8")
        script.chmod(0o755)
        self.revision = "a" * 40
        self.entries = (
            PackageEntry(
                "SKILL.md",
                "file",
                "100644",
                (self.source_root / "SKILL.md").stat().st_size,
                sha256=hashlib.sha256((self.source_root / "SKILL.md").read_bytes()).hexdigest(),
            ),
            PackageEntry(
                "scripts/run.sh",
                "file",
                "100755",
                script.stat().st_size,
                sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
            ),
        )
        self.digest = calculate_skill_digest(self.entries)
        self.snapshot = SimpleNamespace(
            snapshot_path=self.source_root,
            source_revision=self.revision,
            skill_digest=self.digest,
            entries=self.entries,
            coverage_complete=True,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_evidence_writes_json_and_text_atomically_and_rejects_traversal(self) -> None:
        store = EvidenceStore(self.evidence_root, "batch-1", "task-1")
        json_ref = store.write_json("normalized/security-result.json", {"verdict": "PASS", "分数": 90})
        text_ref = store.write_text("summary/review-summary.md", "安全通过\n")

        self.assertEqual(json.loads(json_ref.path.read_text(encoding="utf-8"))["verdict"], "PASS")
        self.assertEqual(text_ref.path.read_text(encoding="utf-8"), "安全通过\n")
        self.assertEqual(json_ref.size_bytes, json_ref.path.stat().st_size)
        with self.assertRaises(ArtifactPathError):
            store.write_text("../escape.txt", "no")
        with self.assertRaises(ArtifactPathError):
            store.write_text("nested/../../escape.txt", "no")

        # Evidence paths are immutable across retries: an identical retry is
        # idempotent, while replacing a different result is refused.
        same_ref = store.write_text("summary/review-summary.md", "安全通过\n")
        self.assertEqual(same_ref.sha256, text_ref.sha256)
        with self.assertRaises(ArtifactWriteError):
            store.write_text("summary/review-summary.md", "changed\n")

    def test_raw_report_is_copied_to_restricted_evidence_and_not_candidate(self) -> None:
        report = self.root / "scanner-output.json"
        report.write_bytes(b'{"findings": []}\n')
        store = EvidenceStore(self.evidence_root, "batch-1", "task-1", candidate_root=self.candidate_root)
        report_ref = store.copy_raw_report(report, scanner="cisco")
        self.assertEqual(report_ref.path.relative_to(store.task_root).as_posix(), "cisco/raw-report.json")
        self.assertEqual(report_ref.path.read_bytes(), report.read_bytes())

        result = export_private_candidate(
            self.snapshot,
            candidate_root=self.candidate_root,
            repository="team/repo",
            skill_path="skills/demo",
            source_revision=self.revision,
            skill_digest=self.digest,
            eligible=True,
            security_decision="PASS",
            quality_score=90,
            evidence_ref=str(report_ref.path),
        )
        self.assertTrue(result.candidate_path.is_dir())
        self.assertTrue((result.package_path / "SKILL.md").exists())
        self.assertFalse((result.candidate_path / "cisco" / "raw-report.json").exists())
        self.assertFalse((result.candidate_path / "scanner-output.json").exists())
        summary = json.loads(result.review_summary_path.read_text(encoding="utf-8"))
        self.assertTrue(summary["private_candidate_eligible"])
        self.assertEqual(summary["skill_digest"], self.digest)
        self.assertEqual(result.verified_digest, self.digest)

    def test_raw_report_symlink_is_rejected(self) -> None:
        report = self.root / "report.json"
        report.write_text("{}", encoding="utf-8")
        link = self.root / "report-link.json"
        link.symlink_to(report)
        with self.assertRaises(ArtifactIntegrityError):
            EvidenceStore(self.evidence_root, "batch-1", "task-1").copy_raw_report(
                link, scanner="cisco"
            )

    def test_binary_coverage_label_does_not_break_candidate_digest(self) -> None:
        binary_entries = (
            PackageEntry(
                "SKILL.md",
                "binary",
                "100644",
                self.entries[0].size,
                sha256=self.entries[0].sha256,
            ),
            self.entries[1],
        )
        snapshot = SimpleNamespace(
            snapshot_path=self.source_root,
            source_revision=self.revision,
            skill_digest=calculate_skill_digest(binary_entries),
            entries=binary_entries,
            coverage_complete=True,
        )
        result = export_private_candidate(
            snapshot,
            candidate_root=self.candidate_root,
            repository="team/repo",
            skill_path="skills/demo",
            source_revision=self.revision,
            skill_digest=snapshot.skill_digest,
            eligible=True,
            security_decision="PASS",
        )
        self.assertEqual(result.verified_digest, snapshot.skill_digest)

    def test_non_eligible_candidate_is_rejected(self) -> None:
        with self.assertRaises(CandidateNotEligibleError):
            export_private_candidate(
                self.snapshot,
                candidate_root=self.candidate_root,
                repository="team/repo",
                skill_path="skills/demo",
                source_revision=self.revision,
                skill_digest=self.digest,
                eligible=False,
            )
        self.assertFalse(any(self.candidate_root.rglob("source-manifest.json")))

    def test_digest_mismatch_is_rejected_before_export(self) -> None:
        with self.assertRaises(DigestMismatchError):
            export_private_candidate(
                self.snapshot,
                candidate_root=self.candidate_root,
                repository="team/repo",
                skill_path="skills/demo",
                source_revision=self.revision,
                skill_digest="b" * 64,
                eligible=True,
            )
        self.assertFalse(any(self.candidate_root.rglob("source-manifest.json")))

    def test_digest_change_during_export_is_rejected_and_no_final_candidate_is_left(self) -> None:
        # The manifest still describes the original bytes, but the source
        # snapshot has changed after it was created.
        (self.source_root / "SKILL.md").write_text("# changed\n", encoding="utf-8")
        with self.assertRaises(DigestMismatchError):
            export_private_candidate(
                self.snapshot,
                candidate_root=self.candidate_root,
                repository="team/repo",
                skill_path="skills/demo",
                source_revision=self.revision,
                skill_digest=self.digest,
                eligible=True,
            )
        self.assertFalse(any(self.candidate_root.rglob("source-manifest.json")))

    def test_candidate_and_evidence_roots_must_not_overlap(self) -> None:
        with self.assertRaises(ArtifactPathError):
            export_private_candidate(
                self.snapshot,
                candidate_root=self.candidate_root,
                evidence_root=self.candidate_root / "evidence",
                repository="team/repo",
                skill_path="skills/demo",
                source_revision=self.revision,
                skill_digest=self.digest,
                eligible=True,
            )

    def test_candidate_source_identity_rejects_traversal(self) -> None:
        with self.assertRaises(CandidateIntegrityError):
            export_private_candidate(
                self.snapshot,
                candidate_root=self.candidate_root,
                repository="team/repo",
                skill_path="../outside",
                source_revision=self.revision,
                skill_digest=self.digest,
                eligible=True,
            )


if __name__ == "__main__":
    unittest.main()
