"""Durable dispatch leases for completion-driven AI review scheduling.

The coordinator never mutates the Batch State directly. This module maintains a
separate trusted control record under the batch manifest root so a completion
event can free exactly one reviewer slot and allocate the next missing task.

A watchdog may also release a reviewer lease that has exceeded its bounded
runtime. Releasing a lease here never changes the review verdict by itself; the
trusted caller must separately persist the corresponding AI failure before
requesting a refill.
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


def _parse_utc(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise DispatchLeaseError("dispatch lease is missing leased_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DispatchLeaseError(f"dispatch lease has invalid leased_at: {text!r}") from exc
    if parsed.tzinfo is None:
        raise DispatchLeaseError("dispatch lease leased_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


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


def _validated_live_state(
    state_path: Path, *, batch_id: str, session_id: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    prior = _load(state_path)
    if prior is None:
        raise DispatchLeaseError("DISPATCH_SESSION_MISSING: start a new dispatch checkpoint")
    if prior.get("batch_id") != batch_id:
        raise DispatchLeaseError("dispatch lease batch_id does not match current batch")
    if prior.get("session_id") != session_id:
        raise DispatchLeaseError("DISPATCH_SESSION_MISMATCH: event belongs to another coordinator session")
    raw = prior.get("in_flight")
    if not isinstance(raw, Mapping):
        raise DispatchLeaseError("dispatch lease state has invalid in_flight data")
    in_flight = {
        str(task_id): dict(item)
        for task_id, item in raw.items()
        if isinstance(item, Mapping)
    }
    return prior, in_flight


def _save_live_state(
    state_path: Path,
    prior: Mapping[str, Any],
    in_flight: Mapping[str, Mapping[str, Any]],
) -> None:
    payload = {
        **dict(prior),
        "updated_at": _utc_now(),
        "in_flight": {task_id: dict(item) for task_id, item in in_flight.items()},
    }
    _atomic_json(state_path, payload)


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


def release_lease(
    state_path: Path,
    *,
    batch_id: str,
    session_id: str,
    task_id: str,
) -> None:
    """Release exactly one explicitly failed/cancelled reviewer lease."""

    prior, in_flight = _validated_live_state(
        state_path,
        batch_id=batch_id,
        session_id=session_id,
    )
    task = str(task_id).strip()
    if task not in in_flight:
        raise DispatchLeaseError(f"TASK_NOT_IN_FLIGHT: task {task!r} is not leased by this session")
    in_flight.pop(task, None)
    _save_live_state(state_path, prior, in_flight)


def expire_stale_leases(
    state_path: Path,
    *,
    batch_id: str,
    session_id: str,
    lease_timeout_seconds: int,
    exclude_task_ids: Sequence[str] = (),
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Release leases older than the configured watchdog timeout.

    The returned task IDs are only lease events. The caller must persist each
    corresponding Skill as an AI failure before asking the scheduler to refill
    those slots.
    """

    if lease_timeout_seconds < 1:
        raise DispatchLeaseError("lease_timeout_seconds must be >= 1")
    prior, in_flight = _validated_live_state(
        state_path,
        batch_id=batch_id,
        session_id=session_id,
    )
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    excluded = {str(task_id).strip() for task_id in exclude_task_ids if str(task_id).strip()}
    expired: list[str] = []
    for task_id, lease in list(in_flight.items()):
        if task_id in excluded:
            continue
        leased_at = _parse_utc(lease.get("leased_at"))
        age = (instant - leased_at).total_seconds()
        if age >= lease_timeout_seconds:
            expired.append(task_id)
            in_flight.pop(task_id, None)
    if expired:
        _save_live_state(state_path, prior, in_flight)
    return tuple(sorted(expired))


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
    stale leases from an interrupted coordinator. Within one live session a
    lease is normally released by the explicit completion event for that task;
    watchdog/failure control paths may release a lease first and then call this
    function again to refill the newly free slot.
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
    "expire_stale_leases",
    "release_lease",
]
