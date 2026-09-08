from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_batch_review.artifacts as artifacts
from skill_batch_review import html_reporting
from skill_batch_review.evidence_index import INDEX_NAME


class EvidenceIndexMigrationTests(unittest.TestCase):
    def test_legacy_evidence_bootstraps_once_then_refresh_uses_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            task = evidence_root / "batch-1" / "task-1"
            task.mkdir(parents=True)
            (task / "final-result.json").write_text(
                json.dumps({"security_decision": "PASS", "findings": []}),
                encoding="utf-8",
            )
            record = {
                "source_row_id": "row-1",
                "skill_id": "id-1",
                "skill_name": "demo",
                "repo_name": "team/demo",
                "branch": "main",
                "skill_path": "skills/demo",
                "source_revision": "a" * 40,
                "skill_digest": "b" * 64,
                "security_decision": "PASS",
                "quality_score": 90,
                "evidence_ref": str(task),
            }
            output = root / "reports" / "report.html"
            with mock.patch.object(
                artifacts,
                "_sha256_file",
                wraps=artifacts._sha256_file,
            ) as hashed:
                html_reporting.write_html_report(
                    [record],
                    output,
                    batch_id="batch-1",
                    evidence_root=evidence_root,
                )
                self.assertGreater(hashed.call_count, 0)
            self.assertTrue((task / INDEX_NAME).is_file())

            with mock.patch.object(
                artifacts,
                "_sha256_file",
                side_effect=AssertionError("second refresh must use persistent index"),
            ):
                html_reporting.write_html_report(
                    [record],
                    output,
                    batch_id="batch-1",
                    evidence_root=evidence_root,
                )


if __name__ == "__main__":
    unittest.main()
