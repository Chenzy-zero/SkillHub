"""Durable, observable leases for completion-driven AI reviewer attempts.

Each dispatch uses an attempt-specific result path. A new coordinator session can
therefore abandon old leases and safely redispatch the same task without a late
old reviewer overwriting the new attempt.
"""

from __future__ import annotations

import hashlib
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
        raise DispatchLeaseError("dispatch lease is missing timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DispatchLeaseError(f"dispatch lease has invalid timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise DispatchLeaseError("dispatch lease timestamp must be timezone-aware")
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


def load_dispatch_state(path: Path) -> dict[str, Any] | None:
    value = _load(path)
    return dict(value) if value is not None else None


def _task(item: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(item.get("task_id") or "").strip()
    handoff = str(item.get("handoff") or "").strip()
    expected = str(item.get("expected_result") or "").strip()
    if not task_id or not handoff or not expected:
        raise DispatchLeaseError("queue item is missing task_id/handoff/expected_result")
    return {
        "task_id": task_id,
        "handoff": handoff,
        "base_expected_result": expected,
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
    return prior, {
        str(task_id): dict(item)
        for task_id, item in raw.items()
        if isinstance(item, Mapping)
    }


def _save_live_state(
    state_path: Path, prior: Mapping[str, Any], in_flight: Mapping[str, Mapping[str, Any]]
) -> None:
    _atomic_json(
        state_path,
        {
            **dict(prior),
            "schema_version": "1.1",
            "updated_at": _utc_now(),
            "in_flight": {task_id: dict(item) for task_id, item in in_flight.items()},
        },
    )


def _task_slug(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _attempt_path(base_expected_result: str, task_id: str, attempt: int) -> Path:
    base = Path(base_expected_result)
    nonce = secrets.token_hex(4)
    path = base.parent / "attempts" / _task_slug(task_id) / f"attempt-{attempt:03d}-{nonce}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _lease_state(lease: Mapping[str, Any]) -> str:
    value = str(lease.get("lease_state") or "").strip().upper()
    return value if value in {"RESERVED", "LAUNCHED"} else "LEGACY"


@dataclass(frozen=True, slots=True)
class DispatchAllocation:
    session_id: str
    max_parallel: int
    new_items: tuple[Mapping[str, Any], ...]
    in_flight: tuple[str, ...]
    queued_count: int
    reserved_count: int
    launched_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_session": self.session_id,
            "max_parallel": self.max_parallel,
            "scheduling": "rolling_largest_first",
            "new_count": len(self.new_items),
            "in_flight_count": len(self.in_flight),
            "reserved_count": self.reserved_count,
            "launched_count": self.launched_count,
            "free_slots": max(0, self.max_parallel - len(self.in_flight)),
            "queued_count": self.queued_count,
            "launch_required": self.reserved_count > 0,
            "items": [
                {
                    "task_id": item["task_id"],
                    "handoff": item["handoff"],
                    "expected_result": item["expected_result"],
                    "attempt": item["attempt"],
                }
                for item in self.new_items
            ],
        }


def mark_launched(
    state_path: Path, *, batch_id: str, session_id: str, task_id: str
) -> None:
    prior, in_flight = _validated_live_state(state_path, batch_id=batch_id, session_id=session_id)
    task = str(task_id).strip()
    lease = in_flight.get(task)
    if lease is None:
        raise DispatchLeaseError(f"TASK_NOT_IN_FLIGHT: task {task!r} is not leased by this session")
    now = _utc_now()
    lease["lease_state"] = "LAUNCHED"
    lease.setdefault("launched_at", now)
    lease["heartbeat_at"] = now
    in_flight[task] = lease
    _save_live_state(state_path, prior, in_flight)


def heartbeat_leases(
    state_path: Path,
    *,
    batch_id: str,
    session_id: str,
    task_ids: Sequence[str],
) -> tuple[str, ...]:
    prior, in_flight = _validated_live_state(state_path, batch_id=batch_id, session_id=session_id)
    tasks = tuple(dict.fromkeys(str(value).strip() for value in task_ids if str(value).strip()))
    missing = [task for task in tasks if task not in in_flight]
    if missing:
        raise DispatchLeaseError(f"TASK_NOT_IN_FLIGHT: {', '.join(missing)}")
    now = _utc_now()
    for task in tasks:
        lease = in_flight[task]
        lease["lease_state"] = "LAUNCHED"
        lease.setdefault("launched_at", now)
        lease["heartbeat_at"] = now
    _save_live_state(state_path, prior, in_flight)
    return tasks


def result_path_for_task(
    state_path: Path, *, batch_id: str, session_id: str, task_id: str
) -> Path:
    _, in_flight = _validated_live_state(state_path, batch_id=batch_id, session_id=session_id)
    lease = in_flight.get(str(task_id).strip())
    if lease is None:
        raise DispatchLeaseError(f"TASK_NOT_IN_FLIGHT: task {task_id!r} is not leased by this session")
    value = str(lease.get("expected_result") or "").strip()
    if not value:
        raise DispatchLeaseError("reviewer lease has no expected_result")
    return Path(value)


def dispatch_snapshot(state_path: Path, *, timeout_seconds: int = 1200) -> dict[str, Any]:
    state = _load(state_path)
    if state is None:
        return {
            "status": "IDLE",
            "session_id": None,
            "max_parallel": 0,
            "in_flight_count": 0,
            "reserved_count": 0,
            "launched_count": 0,
            "stale_count": 0,
            "free_slots": 0,
            "tasks": [],
        }
    now = datetime.now(timezone.utc)
    raw = state.get("in_flight")
    in_flight = raw if isinstance(raw, Mapping) else {}
    tasks: list[dict[str, Any]] = []
    reserved = launched = stale = 0
    for task_id, raw_lease in sorted(in_flight.items()):
        if not isinstance(raw_lease, Mapping):
            continue
        lease = dict(raw_lease)
        state_name = _lease_state(lease)
        stamp = lease.get("heartbeat_at") or lease.get("launched_at") or lease.get("leased_at")
        try:
            age = max(0, int((now - _parse_utc(stamp)).total_seconds()))
        except DispatchLeaseError:
            age = None
        is_stale = age is not None and age >= timeout_seconds
        if state_name == "RESERVED":
            reserved += 1
        else:
            launched += 1
        if is_stale:
            stale += 1
        tasks.append(
            {
                "task_id": str(task_id),
                "state": state_name,
                "attempt": int(lease.get("attempt") or 1),
                "age_seconds": age,
                "stale": is_stale,
                "result_ready": bool(lease.get("expected_result")) and Path(str(lease.get("expected_result"))).is_file(),
            }
        )
    maximum = int(state.get("max_parallel") or 0)
    return {
        "status": "ACTIVE" if tasks else "IDLE",
        "session_id": state.get("session_id"),
        "max_parallel": maximum,
        "in_flight_count": len(tasks),
        "reserved_count": reserved,
        "launched_count": launched,
        "stale_count": stale,
        "free_slots": max(0, maximum - len(tasks)),
        "orphan_attempt_count": len(state.get("orphan_attempts") or []),
        "tasks": tasks,
    }


def release_lease(
    state_path: Path,
    *,
    batch_id: str,
    session_id: str,
    task_id: str,
    orphan_reason: str | None = None,
) -> None:
    prior, in_flight = _validated_live_state(state_path, batch_id=batch_id, session_id=session_id)
    task = str(task_id).strip()
    lease = in_flight.get(task)
    if lease is None:
        raise DispatchLeaseError(f"TASK_NOT_IN_FLIGHT: task {task!r} is not leased by this session")
    in_flight.pop(task, None)
    if orphan_reason:
        orphans = [dict(item) for item in prior.get("orphan_attempts", []) if isinstance(item, Mapping)]
        orphans.append({**lease, "orphaned_at": _utc_now(), "orphan_reason": orphan_reason})
        prior = {**prior, "orphan_attempts": orphans[-200:]}
    _save_live_state(state_path, prior, in_flight)


def allocate_dispatch(
    state_path: Path,
    *,
    batch_id: str,
    queue_items: Sequence[Mapping[str, Any]],
    max_parallel: int,
    session_id: str | None = None,
    completed_task_id: str | None = None,
) -> DispatchAllocation:
    if max_parallel < 1:
        raise DispatchLeaseError("max_parallel must be >= 1")
    normalized = [_task(item) for item in queue_items]
    by_id = {item["task_id"]: item for item in normalized}
    if len(by_id) != len(normalized):
        raise DispatchLeaseError("AI queue contains duplicate task_id values")

    prior = _load(state_path)
    if session_id is None:
        session_id = secrets.token_urlsafe(18)
        created_at = _utc_now()
        in_flight: dict[str, dict[str, Any]] = {}
        counters: dict[str, int] = {}
        orphans: list[dict[str, Any]] = []
        if isinstance(prior, Mapping) and prior.get("batch_id") == batch_id:
            counters = {
                str(key): int(value)
                for key, value in (prior.get("attempt_counters") or {}).items()
                if isinstance(value, int)
            }
            orphans = [dict(item) for item in prior.get("orphan_attempts", []) if isinstance(item, Mapping)]
            old = prior.get("in_flight")
            if isinstance(old, Mapping):
                for lease in old.values():
                    if isinstance(lease, Mapping):
                        orphans.append({**dict(lease), "orphaned_at": _utc_now(), "orphan_reason": "coordinator_replaced"})
        orphans = orphans[-200:]
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
        in_flight = {str(task_id): dict(item) for task_id, item in raw.items() if isinstance(item, Mapping)}
        created_at = str(prior.get("created_at") or _utc_now())
        counters = {
            str(key): int(value)
            for key, value in (prior.get("attempt_counters") or {}).items()
            if isinstance(value, int)
        }
        orphans = [dict(item) for item in prior.get("orphan_attempts", []) if isinstance(item, Mapping)]

    if completed_task_id:
        completed = str(completed_task_id).strip()
        if completed not in in_flight:
            raise DispatchLeaseError(f"COMPLETION_NOT_IN_FLIGHT: task {completed!r} is not leased by this session")
        in_flight.pop(completed, None)

    capacity = max(0, max_parallel - len(in_flight))
    candidates = [item for item in normalized if item["task_id"] not in in_flight]
    candidates.sort(key=lambda item: (-item["review_size_bytes"], item["task_id"]))
    allocated: list[dict[str, Any]] = []
    now = _utc_now()
    for item in candidates[:capacity]:
        task_id = item["task_id"]
        attempt = counters.get(task_id, 0) + 1
        counters[task_id] = attempt
        expected = _attempt_path(item["base_expected_result"], task_id, attempt)
        lease = {
            "task_id": task_id,
            "handoff": item["handoff"],
            "base_expected_result": item["base_expected_result"],
            "expected_result": str(expected),
            "review_size_bytes": item["review_size_bytes"],
            "attempt": attempt,
            "lease_state": "RESERVED",
            "leased_at": now,
        }
        in_flight[task_id] = lease
        allocated.append(lease)

    payload = {
        "schema_version": "1.1",
        "batch_id": batch_id,
        "session_id": session_id,
        "created_at": created_at,
        "updated_at": now,
        "max_parallel": max_parallel,
        "attempt_counters": counters,
        "orphan_attempts": orphans,
        "in_flight": in_flight,
    }
    _atomic_json(state_path, payload)
    reserved = sum(_lease_state(item) == "RESERVED" for item in in_flight.values())
    launched = len(in_flight) - reserved
    return DispatchAllocation(
        session_id=session_id,
        max_parallel=max_parallel,
        new_items=tuple(allocated),
        in_flight=tuple(sorted(in_flight)),
        queued_count=max(0, len(normalized) - len(in_flight)),
        reserved_count=reserved,
        launched_count=launched,
    )


def clear_dispatch_state(state_path: Path) -> None:
    state_path.unlink(missing_ok=True)


__all__ = [
    "DispatchAllocation",
    "DispatchLeaseError",
    "allocate_dispatch",
    "clear_dispatch_state",
    "dispatch_snapshot",
    "heartbeat_leases",
    "load_dispatch_state",
    "mark_launched",
    "release_lease",
    "result_path_for_task",
]
