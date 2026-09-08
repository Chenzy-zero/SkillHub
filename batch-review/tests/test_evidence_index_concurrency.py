from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import skill_batch_review.artifacts as artifacts
from skill_batch_review.evidence_index import INDEX_NAME


def test_parallel_evidence_writes_keep_all_index_entries(tmp_path):
    store = artifacts.EvidenceStore(
        tmp_path / "evidence",
        "batch-1",
        "task-1",
        candidate_root=tmp_path / "candidates",
    )

    def write_one(index: int):
        return store.write_json(
            f"scanners/demo-{index}/normalized-result.json",
            {"index": index, "status": "COMPLETED"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        references = list(pool.map(write_one, range(24)))

    index = json.loads((store.task_root / INDEX_NAME).read_text(encoding="utf-8"))
    entries = {item["task_path"]: item for item in index["artifacts"]}
    assert len(entries) == 24
    for number, reference in enumerate(references):
        path = f"scanners/demo-{number}/normalized-result.json"
        assert path in entries
        assert entries[path]["sha256"] == reference.sha256
        assert entries[path]["size_bytes"] == reference.size_bytes
