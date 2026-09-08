"""Durable dispatch leases for completion-driven AI review scheduling.

The coordinator never mutates the Batch State directly.  This module maintains a
separate trusted control record under the batch manifest root so a completion
event can free exactly one reviewer slot and allocate the next missing task.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class DispatchLeaseError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DispatchLeaseError(f"cannot read dispatch lease state: {exc}") from exc
    if not isinstance(value, dict):
        raise DispatchLeaseError("dispatch lease state must be a JSON object")
    return value


def _task(item: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(item.get("task_id") or "").strip()
    handoff = str(item.get("handoff") or "").strip()
    expected = str(item.get("expected_result") or "").strip()
    if not task_id or not handoff or not expected:
        raise DispatchLeaseError("queue item is missing task_id/handoff/expected_result")
    return {
        "task_id": task_id,
        "handoff": handoff,
        "expected_result": expected,
        "review_size_bytes": int(item.get("review_size_bytes") or 0),
    }


@dataclass(frozen=True, slots=True)
class DispatchAllocation:
    session_id: str
    max_parallel: int
    new_items: tuple[Mapping[str, Any], ...]
    in_flight: tuple[str, ...]
    queued_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_session": self.session_id,
            "max_parallel": self.max_parallel,
            "scheduling": "rolling_largest_first",
            "new_count": len(self.new_items),
            "in_flight_count": len(self.in_flight),
            "queued_count": self.queued_count,
            "items": [
                {
                    "task_id": item["task_id"],
                    "handoff": item["handoff"],
                    "expected_result": item["expected_result"],
                }
                for item in self.new_items
            ],
        }


def allocate_dispatch(
    state_path: Path,
    *,
    batch_id: str,
    queue_items: Sequence[Mapping[str, Any]],
    max_parallel: int,
    session_id: str | None = None,
    completed_task_id: str | None = None,
) -> DispatchAllocation:
    """Reconcile leases and allocate only newly free reviewer slots.

    Calling without ``session_id`` starts a new coordinator session and releases
    stale leases from an interrupted coordinator.  Within one live session a
    lease is released only by the explicit completion event for that task.  The
    queue may stop listing a task as soon as its expected-result file appears,
    but that alone is not treated as a completion event.
    """

    if max_parallel < 1:
        raise DispatchLeaseError("max_parallel must be >= 1")
    normalized = [_task(item) for item in queue_items]
    by_id = {item["task_id"]: item for item in normalized}
    if len(by_id) != len(normalized):
        raise DispatchLeaseError("AI queue contains duplicate task_id values")

    prior = _load(state_path)
    if session_id is None:
        session_id = secrets.token_urlsafe(18)
        in_flight: dict[str, dict[str, Any]] = {}
        created_at = _utc_now()
    else:
        if prior is None:
            raise DispatchLeaseError("DISPATCH_SESSION_MISSING: start a new dispatch checkpoint")
        if prior.get("batch_id") != batch_id:
            raise DispatchLeaseError("dispatch lease batch_id does not match current batch")
        if prior.get("session_id") != session_id:
            raise DispatchLeaseError("DISPATCH_SESSION_MISMATCH: completion belongs to another coordinator session")
        raw = prior.get("in_flight")
        if not isinstance(raw, Mapping):
            raise DispatchLeaseError("dispatch lease state has invalid in_flight data")
        in_flight = {
            str(task_id): dict(item)
            for task_id, item in raw.items()
            if isinstance(item, Mapping)
        }
        created_at = str(prior.get("created_at") or _utc_now())

    if completed_task_id:
        completed = str(completed_task_id).strip()
        if completed not in in_flight:
            raise DispatchLeaseError(
                f"COMPLETION_NOT_IN_FLIGHT: task {completed!r} is not leased by this session"
            )
        in_flight.pop(completed, None)

    capacity = max(0, max_parallel - len(in_flight))
    candidates = [item for item in normalized if item["task_id"] not in in_flight]
    candidates.sort(key=lambda item: (-item["review_size_bytes"], item["task_id"]))
    allocated = candidates[:capacity]
    now = _utc_now()
    for item in allocated:
        in_flight[item["task_id"]] = {
            "task_id": item["task_id"],
            "handoff": item["handoff"],
            "expected_result": item["expected_result"],
            "review_size_bytes": item["review_size_bytes"],
            "leased_at": now,
        }

    payload = {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "session_id": session_id,
        "created_at": created_at,
        "updated_at": now,
        "max_parallel": max_parallel,
        "in_flight": in_flight,
    }
    _atomic_json(state_path, payload)
    return DispatchAllocation(
        session_id=session_id,
        max_parallel=max_parallel,
        new_items=tuple(allocated),
        in_flight=tuple(sorted(in_flight)),
        queued_count=max(0, len(normalized) - sum(task_id in by_id for task_id in in_flight)),
    )


def clear_dispatch_state(state_path: Path) -> None:
    state_path.unlink(missing_ok=True)


__all__ = [
    "DispatchAllocation",
    "DispatchLeaseError",
    "allocate_dispatch",
    "clear_dispatch_state",
]
