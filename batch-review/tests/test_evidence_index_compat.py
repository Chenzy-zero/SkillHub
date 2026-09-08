from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skill_batch_review.artifacts import EvidenceStore
from skill_batch_review.evidence_index import INDEX_NAME


class EvidenceIndexCompatibilityTests(unittest.TestCase):
    def test_legacy_scanner_raw_path_is_path_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            source = root / "raw.json"
            source.write_text("{}\n", encoding="utf-8")
            store = EvidenceStore(evidence, "batch-1", "task-1")
            store.copy_raw_report(source, scanner="cisco")
            index = json.loads((store.task_root / INDEX_NAME).read_text(encoding="utf-8"))
            item = next(entry for entry in index["artifacts"] if entry["task_path"] == "cisco/raw-report.json")
            self.assertEqual(item["type"], "RAW")
            self.assertEqual(item["navigation"], "PATH_ONLY")


if __name__ == "__main__":
    unittest.main()
