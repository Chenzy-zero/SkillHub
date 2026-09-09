from __future__ import annotations

import json
from pathlib import Path

from skill_batch_review.dispatch_lease import (
    allocate_dispatch,
    dispatch_snapshot,
    mark_launched,
)


ROOT = Path(__file__).resolve().parents[1]


def _queue(tmp_path: Path, count: int = 6):
    return [
        {
            "task_id": f"task-{index}",
            "handoff": str(tmp_path / f"handoff-{index}.json"),
            "expected_result": str(tmp_path / f"result-{index}.json"),
            "review_size_bytes": 1000 - index,
        }
        for index in range(count)
    ]


def test_initial_pool_fills_five_slots_and_reports_reserved(tmp_path: Path):
    state = tmp_path / "ai-dispatch-state.json"
    allocation = allocate_dispatch(
        state,
        batch_id="batch-1",
        queue_items=_queue(tmp_path),
        max_parallel=5,
    )
    assert len(allocation.new_items) == 5
    assert allocation.reserved_count == 5
    assert allocation.launched_count == 0
    assert allocation.to_dict()["launch_required"] is True
    snapshot = dispatch_snapshot(state)
    assert snapshot["in_flight_count"] == 5
    assert snapshot["reserved_count"] == 5


def test_launch_registration_makes_pool_state_observable(tmp_path: Path):
    state = tmp_path / "ai-dispatch-state.json"
    allocation = allocate_dispatch(
        state,
        batch_id="batch-1",
        queue_items=_queue(tmp_path),
        max_parallel=2,
    )
    task_id = allocation.new_items[0]["task_id"]
    mark_launched(
        state,
        batch_id="batch-1",
        session_id=allocation.session_id,
        task_id=task_id,
    )
    snapshot = dispatch_snapshot(state)
    assert snapshot["launched_count"] == 1
    assert snapshot["reserved_count"] == 1
    by_id = {item["task_id"]: item for item in snapshot["tasks"]}
    assert by_id[task_id]["state"] == "LAUNCHED"
    assert by_id[task_id]["attempt"] == 1


def test_new_coordinator_uses_distinct_attempt_paths(tmp_path: Path):
    state = tmp_path / "ai-dispatch-state.json"
    queue = _queue(tmp_path, 2)
    first = allocate_dispatch(
        state,
        batch_id="batch-1",
        queue_items=queue,
        max_parallel=1,
    )
    first_path = Path(first.new_items[0]["expected_result"])
    second = allocate_dispatch(
        state,
        batch_id="batch-1",
        queue_items=queue,
        max_parallel=1,
    )
    second_path = Path(second.new_items[0]["expected_result"])
    assert first.session_id != second.session_id
    assert first_path != second_path
    assert "attempts" in first_path.parts
    assert "attempts" in second_path.parts
    assert second.new_items[0]["attempt"] == 2
    persisted = json.loads(state.read_text(encoding="utf-8"))
    orphan_paths = {
        Path(str(item["expected_result"]))
        for item in persisted.get("orphan_attempts", [])
        if isinstance(item, dict) and item.get("expected_result")
    }
    assert first_path in orphan_paths
    assert any(item.get("orphan_reason") == "coordinator_replaced" for item in persisted["orphan_attempts"])


def test_auto_review_protocol_requires_resume_and_full_fanout():
    canonical = (ROOT / ".agents" / "skills" / "auto-skill-review" / "SKILL.md").read_text(encoding="utf-8")
    claude = (ROOT / ".claude" / "skills" / "auto-skill-review" / "SKILL.md").read_text(encoding="utf-8")
    for text in (canonical, claude):
        assert "review_pool.py resume --ai-parallel 5" in text
        assert "review_pool.py launched" in text
        assert "review_pool.py complete" in text
        assert "review_pool.py retry" in text
        assert "review_pool.py tick" in text
        assert "Launch all returned items" in text or "Launch all returned items before waiting" in text
        assert "Do not use Git" in text


def test_reviewer_uses_fast_coverage_workflow_without_default_long_rubrics():
    skill = (ROOT / ".agents" / "skills" / "skill-security-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "coverage-first, deep-read-second" in skill
    assert "Glob the package once" in skill
    assert "Do not open `references/security-review.md` or `references/quality-review.md` by default" in skill
    assert "generic words such as `config`" in skill
