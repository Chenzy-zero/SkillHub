"""Durable local state for a batch Skill review.

The batch runner is intentionally able to resume without relying on an
in-memory queue.  :class:`BatchStateStore` keeps one JSON document per batch,
writes it with a same-directory temporary file followed by ``os.replace``,
and appends an audit event for every meaningful state change.

This module is local-only.  It does not contact Gerrit, start a scanner, or
interpret a Skill.  A task payload should contain references to evidence and
outputs rather than the contents of those files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence


STATE_SCHEMA_VERSION = "0.1"


class BatchStateError(ValueError):
    """Base error raised for malformed or unusable local batch state."""


class StateTransitionError(BatchStateError):
    """Raised when a batch or task takes an illegal state transition."""


class StateConflictError(BatchStateError):
    """Raised when an expected current state does not match the stored state."""


class StateNotFoundError(BatchStateError):
    """Raised when a requested task or checkpoint is not present."""


BATCH_STATES = frozenset(
    {
        "CREATED",
        "VALIDATING",
        "READY",
        "RUNNING",
        "WAITING_FOR_AI_REVIEW",
        "WAITING_FOR_MANUAL_REVIEW",
        "PARTIALLY_COMPLETED",
        "COMPLETED",
        "FAILED",
    }
)

# ``FAILED`` is recoverable by moving a task back to ``PENDING``.  A complete
# task is deliberately terminal: changing its inputs must create a new task
# key, and must not mutate an old result.
TASK_STATES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "ERROR", "SKIPPED"})

_BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"VALIDATING", "FAILED"}),
    "VALIDATING": frozenset({"READY", "FAILED"}),
    "READY": frozenset({"RUNNING", "FAILED"}),
    "RUNNING": frozenset(
        {
            "WAITING_FOR_AI_REVIEW",
            "WAITING_FOR_MANUAL_REVIEW",
            "PARTIALLY_COMPLETED",
            "COMPLETED",
            "FAILED",
        }
    ),
    "WAITING_FOR_AI_REVIEW": frozenset(
        {"RUNNING", "WAITING_FOR_MANUAL_REVIEW", "PARTIALLY_COMPLETED", "COMPLETED", "FAILED"}
    ),
    "WAITING_FOR_MANUAL_REVIEW": frozenset(
        {"RUNNING", "PARTIALLY_COMPLETED", "COMPLETED", "FAILED"}
    ),
    "PARTIALLY_COMPLETED": frozenset({"RUNNING", "COMPLETED", "FAILED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset({"VALIDATING", "READY", "RUNNING"}),
}

_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"RUNNING", "SUCCEEDED", "FAILED", "ERROR", "SKIPPED"}),
    "RUNNING": frozenset({"PENDING", "SUCCEEDED", "FAILED", "ERROR", "SKIPPED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset({"PENDING", "RUNNING"}),
    "ERROR": frozenset({"PENDING", "RUNNING"}),
    "SKIPPED": frozenset(),
}

_TASK_KEY_PART_RE = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_\-.])(password|passwd|secret|token|api[_\-.]?key|access[_\-.]?key|private[_\-.]?key|credential|authorization)(?:$|[_\-.])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+|basic\s+)[A-Za-z0-9._~+/=-]+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)


def utc_now() -> str:
    """Return a UTC timestamp in a stable, JSON-friendly form."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_text(value: Any, field: str) -> str:
    if value is None:
        raise BatchStateError(f"{field} must not be empty")
    result = str(value).strip()
    if not result:
        raise BatchStateError(f"{field} must not be empty")
    if _TASK_KEY_PART_RE.search(result):
        raise BatchStateError(f"{field} contains a control character")
    return result


def _redact_for_state(value: Any, *, key: str | None = None) -> Any:
    """Keep event/state payloads useful without persisting obvious secrets."""

    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_for_state(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_for_state(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_for_state(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE_RE.sub("[REDACTED]", value)
    return value


def make_task_key(*parts: Any) -> str:
    """Build a stable, non-secret task key from ordered identity parts.

    The key is a SHA-256 digest of a canonical JSON array.  Ordered parts are
    intentional: scanner name, version, policy and mode are distinct task
    inputs even when their values happen to look similar.  Mapping values are
    sorted by JSON serialization to make callers' dictionary order irrelevant.
    """

    if not parts:
        raise BatchStateError("task key requires at least one identity part")
    canonical = json.dumps(
        list(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Write JSON atomically, retaining the destination on failure."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        # Directory fsync is supported on the Unix environments where this
        # local runner is expected to operate; ignore platforms that reject it.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise StateNotFoundError(f"state file does not exist: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchStateError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchStateError("state file root must be a JSON object")
    return value


def _validate_state(document: Mapping[str, Any]) -> None:
    if document.get("schema_version") != STATE_SCHEMA_VERSION:
        raise BatchStateError("unsupported or missing state schema_version")
    if not isinstance(document.get("batch_id"), str) or not document["batch_id"]:
        raise BatchStateError("state batch_id is missing")
    batch_status = document.get("status")
    if batch_status not in BATCH_STATES:
        raise BatchStateError(f"invalid batch status: {batch_status!r}")
    for field in ("tasks", "checkpoints"):
        if not isinstance(document.get(field), dict):
            raise BatchStateError(f"state {field} must be an object")
    if not isinstance(document.get("events"), list):
        raise BatchStateError("state events must be an array")
    if not isinstance(document.get("revision"), int) or document["revision"] < 0:
        raise BatchStateError("state revision must be a non-negative integer")
    for key, task in document["tasks"].items():
        if not isinstance(key, str) or not isinstance(task, dict):
            raise BatchStateError("state tasks must map strings to objects")
        status = task.get("status")
        if status not in TASK_STATES:
            raise BatchStateError(f"invalid task status for {key}: {status!r}")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A serializable indication of the last durable stage output."""

    task_key: str
    stage: str
    status: str
    output_refs: tuple[str, ...] = ()
    resume_after: str | None = None
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.status not in TASK_STATES:
            raise BatchStateError(f"invalid checkpoint status: {self.status!r}")
        return {
            "task_key": _validate_text(self.task_key, "checkpoint.task_key"),
            "stage": _validate_text(self.stage, "checkpoint.stage"),
            "status": self.status,
            "output_refs": list(self.output_refs),
            "resume_after": self.resume_after,
            "metadata": _redact_for_state(dict(self.metadata or {})),
        }


class BatchStateStore:
    """A small transactional JSON state store for one batch.

    The store is designed for one local writer.  Each mutating method reads
    the current file and atomically replaces it, so a process interruption
    cannot leave a half-written JSON document.  A caller that needs multiple
    writers should put an external lock around calls to this class.
    """

    def __init__(self, path: Path, *, batch_id: str | None = None) -> None:
        self.path = Path(path)
        self._batch_id = _validate_text(batch_id, "batch_id") if batch_id else None

    @classmethod
    def create(
        cls,
        path: Path,
        batch_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        initial_status: str = "CREATED",
        overwrite: bool = False,
    ) -> "BatchStateStore":
        """Create a state file, failing instead of overwriting by default."""

        if initial_status not in BATCH_STATES:
            raise BatchStateError(f"invalid initial batch status: {initial_status!r}")
        store = cls(path, batch_id=batch_id)
        if store.path.exists() and not overwrite:
            raise BatchStateError(f"state file already exists: {store.path}")
        timestamp = utc_now()
        document: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "batch_id": store._batch_id,
            "status": initial_status,
            "created_at": timestamp,
            "updated_at": timestamp,
            "revision": 0,
            "metadata": _redact_for_state(dict(metadata or {})),
            "tasks": {},
            "checkpoints": {},
            "events": [],
        }
        _validate_state(document)
        _atomic_write_json(store.path, document)
        return store

    @classmethod
    def open(cls, path: Path, *, batch_id: str | None = None) -> "BatchStateStore":
        store = cls(path, batch_id=batch_id)
        document = _load_json(store.path)
        _validate_state(document)
        if store._batch_id is not None and document["batch_id"] != store._batch_id:
            raise StateConflictError(
                f"state batch_id {document['batch_id']!r} does not match {store._batch_id!r}"
            )
        store._batch_id = document["batch_id"]
        return store

    load = open

    def read(self) -> dict[str, Any]:
        document = _load_json(self.path)
        _validate_state(document)
        if self._batch_id is not None and document["batch_id"] != self._batch_id:
            raise StateConflictError("state batch_id changed unexpectedly")
        return deepcopy(document)

    def _commit(self, document: MutableMapping[str, Any]) -> None:
        document["revision"] = int(document.get("revision", 0)) + 1
        document["updated_at"] = utc_now()
        _validate_state(document)
        _atomic_write_json(self.path, document)

    @staticmethod
    def _append_event(
        document: MutableMapping[str, Any],
        event_type: str,
        *,
        task_key: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_type = _validate_text(event_type, "event_type")
        events = document["events"]
        sequence = len(events) + 1
        event = {
            "sequence": sequence,
            "event_type": event_type,
            "task_key": task_key,
            "occurred_at": utc_now(),
            "payload": _redact_for_state(dict(payload or {})),
        }
        event["event_id"] = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        events.append(event)
        return event

    def append_event(
        self,
        event_type: str,
        *,
        task_key: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one audit event and durably persist it."""

        document = self.read()
        if task_key is not None:
            task_key = _validate_text(task_key, "task_key")
        event = self._append_event(document, event_type, task_key=task_key, payload=payload)
        self._commit(document)
        return deepcopy(event)

    def transition_batch(
        self,
        new_status: str,
        *,
        expected_status: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Move the batch through the documented state machine.

        Repeating the current state is idempotent and does not append a
        duplicate event.  An ``expected_status`` protects a caller from
        accidentally updating a state changed by a resumed process.
        """

        if new_status not in BATCH_STATES:
            raise StateTransitionError(f"invalid batch status: {new_status!r}")
        document = self.read()
        current = document["status"]
        if expected_status is not None and current != expected_status:
            raise StateConflictError(
                f"expected batch status {expected_status!r}, found {current!r}"
            )
        if new_status == current:
            return deepcopy(document)
        if new_status not in _BATCH_TRANSITIONS[current]:
            raise StateTransitionError(f"illegal batch transition {current} -> {new_status}")
        document["status"] = new_status
        self._append_event(
            document,
            "BATCH_STATUS_CHANGED",
            payload={"from": current, "to": new_status, "reason": reason},
        )
        self._commit(document)
        return deepcopy(document)

    def get_task(self, task_key: str) -> dict[str, Any]:
        task_key = _validate_text(task_key, "task_key")
        task = self.read()["tasks"].get(task_key)
        if task is None:
            raise StateNotFoundError(f"task does not exist: {task_key}")
        return deepcopy(task)

    def upsert_task(
        self,
        task_key: str,
        *,
        status: str = "PENDING",
        stage: str | None = None,
        payload: Mapping[str, Any] | None = None,
        attempt: int | None = None,
        expected_status: str | None = None,
        event_type: str = "TASK_STATUS_CHANGED",
    ) -> dict[str, Any]:
        """Create or transition a task, preserving its history and identity.

        The task key is the caller's idempotency boundary.  A repeated write
        with the same status and equivalent fields returns the existing task
        without appending another event.  A status change is validated against
        the task transition graph.
        """

        task_key = _validate_text(task_key, "task_key")
        if status not in TASK_STATES:
            raise StateTransitionError(f"invalid task status: {status!r}")
        if expected_status is not None and expected_status not in TASK_STATES:
            raise StateTransitionError(f"invalid expected task status: {expected_status!r}")
        document = self.read()
        tasks = document["tasks"]
        existing = tasks.get(task_key)
        if existing is None:
            if expected_status is not None:
                raise StateConflictError(f"task does not exist: {task_key}")
            current = None
            normalized_attempt = 0 if attempt is None else attempt
            if isinstance(normalized_attempt, bool) or not isinstance(normalized_attempt, int):
                raise BatchStateError("task attempt must be a non-negative integer")
            if normalized_attempt < 0:
                raise BatchStateError("task attempt must be a non-negative integer")
            task: dict[str, Any] = {
                "task_key": task_key,
                "status": status,
                "stage": _validate_text(stage, "task.stage") if stage is not None else None,
                "attempt": normalized_attempt,
                "payload": _redact_for_state(dict(payload or {})),
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            tasks[task_key] = task
            self._append_event(
                document,
                event_type,
                task_key=task_key,
                payload={"from": None, "to": status, "stage": task["stage"]},
            )
            self._commit(document)
            return deepcopy(task)

        current = existing.get("status")
        if current not in TASK_STATES:
            raise BatchStateError(f"invalid stored task status: {current!r}")
        if expected_status is not None and current != expected_status:
            raise StateConflictError(
                f"expected task status {expected_status!r}, found {current!r}"
            )
        normalized_payload = _redact_for_state(dict(payload or {}))
        normalized_stage = _validate_text(stage, "task.stage") if stage is not None else existing.get("stage")
        normalized_attempt = existing.get("attempt", 0) if attempt is None else attempt
        if isinstance(normalized_attempt, bool) or not isinstance(normalized_attempt, int) or normalized_attempt < 0:
            raise BatchStateError("task attempt must be a non-negative integer")
        if (
            status == current
            and normalized_stage == existing.get("stage")
            and normalized_payload == existing.get("payload", {})
            and normalized_attempt == existing.get("attempt", 0)
        ):
            return deepcopy(existing)
        if status != current and status not in _TASK_TRANSITIONS[current]:
            raise StateTransitionError(f"illegal task transition {current} -> {status}")
        existing["status"] = status
        existing["stage"] = normalized_stage
        existing["attempt"] = normalized_attempt
        existing["payload"] = normalized_payload
        existing["updated_at"] = utc_now()
        self._append_event(
            document,
            event_type,
            task_key=task_key,
            payload={"from": current, "to": status, "stage": normalized_stage},
        )
        self._commit(document)
        return deepcopy(existing)

    # A descriptive alias used by orchestrators.
    transition_task = upsert_task

    def save_checkpoint(
        self,
        checkpoint: Checkpoint | None = None,
        *,
        task_key: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        output_refs: Sequence[str] = (),
        resume_after: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist stage completion information used for resume.

        The keyword form is convenient for runners; a :class:`Checkpoint`
        instance is accepted when a caller already has a typed value.
        Rewriting an identical checkpoint is idempotent and does not append a
        duplicate event.
        """

        if checkpoint is not None:
            if any(value is not None for value in (task_key, stage, status)):
                raise BatchStateError("checkpoint object cannot be combined with checkpoint fields")
            value = checkpoint.to_dict()
        else:
            if task_key is None or stage is None or status is None:
                raise BatchStateError("task_key, stage and status are required for a checkpoint")
            value = Checkpoint(
                task_key=task_key,
                stage=stage,
                status=status,
                output_refs=tuple(output_refs),
                resume_after=resume_after,
                metadata=metadata,
            ).to_dict()
        document = self.read()
        key = value["task_key"]
        checkpoints = document["checkpoints"]
        previous = checkpoints.get(key)
        if previous == value:
            return deepcopy(previous)
        checkpoints[key] = value
        self._append_event(
            document,
            "CHECKPOINT_SAVED",
            task_key=key,
            payload={
                "stage": value["stage"],
                "status": value["status"],
                "output_refs": value["output_refs"],
                "resume_after": value["resume_after"],
            },
        )
        self._commit(document)
        return deepcopy(value)

    def get_checkpoint(self, task_key: str) -> dict[str, Any]:
        task_key = _validate_text(task_key, "task_key")
        checkpoint = self.read()["checkpoints"].get(task_key)
        if checkpoint is None:
            raise StateNotFoundError(f"checkpoint does not exist: {task_key}")
        return deepcopy(checkpoint)

    def resumable_tasks(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Return deterministic tasks that can be resumed or retried."""

        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise BatchStateError("max_attempts must be a positive integer")
        tasks = self.read()["tasks"].values()
        result = []
        for task in tasks:
            if task["status"] in {"PENDING", "RUNNING"}:
                result.append(task)
            elif task["status"] in {"FAILED", "ERROR"} and task.get("attempt", 0) < max_attempts:
                result.append(task)
        return sorted((deepcopy(task) for task in result), key=lambda item: item["task_key"])

    # Names that read naturally at call sites.
    recovery_candidates = resumable_tasks
    resume_tasks = resumable_tasks


__all__ = [
    "BATCH_STATES",
    "BatchStateError",
    "BatchStateStore",
    "Checkpoint",
    "STATE_SCHEMA_VERSION",
    "StateConflictError",
    "StateNotFoundError",
    "StateTransitionError",
    "TASK_STATES",
    "make_task_key",
    "utc_now",
]
