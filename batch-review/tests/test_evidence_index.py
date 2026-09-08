from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_batch_review.artifacts as artifacts
from skill_batch_review import html_reporting
from skill_batch_review.evidence_index import INDEX_NAME, safe_evidence_bundle


_REPORT_RE = re.compile(
    r'<script type="application/json" id="report-data">(.*?)</script>',
    re.DOTALL,
)


class EvidenceIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence_root = self.root / "evidence"
        self.candidates = self.root / "candidates"
        self.store = artifacts.EvidenceStore(
            self.evidence_root,
            "batch-1",
            "task-1",
            candidate_root=self.candidates,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_store_updates_atomic_index_with_existing_digest_and_metadata(self) -> None:
        final = self.store.write_json(
            "final-result.json",
            {"security_decision": "PASS", "findings": []},
        )
        raw_source = self.root / "raw.json"
        raw_source.write_text('{"scanner":"raw"}\n', encoding="utf-8")
        raw = self.store.copy_raw_report(
            raw_source,
            "scanners/cisco/raw-report.json",
        )

        index_path = self.store.task_root / INDEX_NAME
        self.assertTrue(index_path.is_file())
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["schema_version"], "1.0")
        self.assertEqual(index["batch_id"], "batch-1")
        self.assertEqual(index["task_id"], "task-1")
        entries = {item["task_path"]: item for item in index["artifacts"]}
        self.assertEqual(entries["final-result.json"]["type"], "DERIVED")
        self.assertEqual(entries["final-result.json"]["navigation"], "OPEN")
        self.assertEqual(entries["final-result.json"]["sha256"], final.sha256)
        self.assertEqual(entries["final-result.json"]["size_bytes"], final.size_bytes)
        self.assertEqual(entries["scanners/cisco/raw-report.json"]["type"], "RAW")
        self.assertEqual(entries["scanners/cisco/raw-report.json"]["navigation"], "PATH_ONLY")
        self.assertEqual(entries["scanners/cisco/raw-report.json"]["sha256"], raw.sha256)

        for relative, entry in entries.items():
            path = self.store.task_root.joinpath(*relative.split("/"))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(entry["sha256"], digest)
            self.assertEqual(entry["size_bytes"], path.stat().st_size)

    def test_report_bundle_uses_index_without_rehashing_historical_evidence(self) -> None:
        self.store.write_json(
            "final-result.json",
            {"security_decision": "PASS", "findings": [{"severity": "LOW", "title": "demo"}]},
        )
        self.store.write_json(
            "scanners/cisco/normalized-result.json",
            {"status": "COMPLETED"},
        )

        with mock.patch.object(
            artifacts,
            "_sha256_file",
            side_effect=AssertionError("report refresh must not hash historical evidence"),
        ):
            bundle = safe_evidence_bundle(self.store.task_root, self.evidence_root)

        self.assertIsNotNone(bundle["document"])
        self.assertEqual(
            {item["task_path"] for item in bundle["artifacts"]},
            {"final-result.json", "scanners/cisco/normalized-result.json"},
        )

    def test_missing_or_symlinked_evidence_is_not_exposed_from_index(self) -> None:
        self.store.write_json("final-result.json", {"security_decision": "PASS"})
        normalized = self.store.write_json(
            "scanners/cisco/normalized-result.json",
            {"status": "COMPLETED"},
        )
        normalized.path.unlink()
        bundle = safe_evidence_bundle(self.store.task_root, self.evidence_root)
        self.assertEqual(
            [item["task_path"] for item in bundle["artifacts"]],
            ["final-result.json"],
        )

        final = self.store.task_root / "final-result.json"
        outside = self.root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        final.unlink()
        try:
            final.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        bundle = safe_evidence_bundle(self.store.task_root, self.evidence_root)
        self.assertEqual(bundle["artifacts"], [])
        self.assertIsNone(bundle["document"])

    def test_html_links_only_derived_and_normalized_evidence(self) -> None:
        self.store.write_json(
            "final-result.json",
            {"security_decision": "PASS", "findings": []},
        )
        self.store.write_json(
            "source-metadata.json",
            {"repo_name": "team/demo"},
        )
        self.store.write_json(
            "scanners/cisco/normalized-result.json",
            {"status": "COMPLETED"},
        )
        raw_source = self.root / "raw.json"
        raw_source.write_text("{}\n", encoding="utf-8")
        self.store.copy_raw_report(raw_source, "scanners/cisco/raw-report.json")

        output = self.root / "results" / "batch-1" / "report.html"
        html_reporting.write_html_report(
            [
                {
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
                    "evidence_ref": str(self.store.task_root),
                }
            ],
            output,
            batch_id="batch-1",
            evidence_root=self.evidence_root,
        )
        html = output.read_text(encoding="utf-8")
        match = _REPORT_RE.search(html)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        entries = {
            item["task_path"]: item
            for item in payload["skills"][0]["evidence_artifacts"]
        }
        self.assertIn("href", entries["final-result.json"])
        self.assertIn("href", entries["scanners/cisco/normalized-result.json"])
        self.assertNotIn("href", entries["source-metadata.json"])
        self.assertNotIn("href", entries["scanners/cisco/raw-report.json"])
        self.assertNotIn(str(self.evidence_root.resolve()), html)
        self.assertIn("打开受控派生/规范化证据", html)


if __name__ == "__main__":
    unittest.main()
