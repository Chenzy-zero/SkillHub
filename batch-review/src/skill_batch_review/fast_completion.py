"""Fast-path AI completion import for the live reviewer pool.

The ordinary compatibility importer remains available, but the reviewer-pool
control plane uses this module to amortize work across every result that is
already durable on disk.  It loads Batch State once, finalizes all ready attempts,
updates the queue once, and deliberately defers CSV/JSON/HTML projection rebuilds
until the batch reaches its normal final-report boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import batch_launcher
from .config import ReviewConfig
from .per_skill import finalize_skill


class FastCompletionError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _find_item(state: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for item in state.get("items", []):
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return item
    raise FastCompletionError(f"unknown AI task: {task_id}")


@dataclass(frozen=True, slots=True)
class FastImportResult:
    batch_id: str
    imported: tuple[str, ...]
    failed: Mapping[str, str]
    remaining_ai: int
    batch_status: str
    report_html: str | None
    projection_deferred: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "imported": list(self.imported),
            "failed": dict(self.failed),
            "remaining_ai": self.remaining_ai,
            "batch_status": self.batch_status,
            "report_html": self.report_html,
            "projection_deferred": self.projection_deferred,
        }


def _candidate_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    # Keep caller order while removing duplicates. Callers normally put newer
    # attempts last; reverse here so the newest durable attempt is tried first.
    unique = tuple(dict.fromkeys(Path(path) for path in paths if Path(path).is_file()))
    return tuple(reversed(unique))


def _commit_progress(config: ReviewConfig, state: dict[str, Any], imported_count: int) -> None:
    if imported_count:
        state["report_projection_dirty"] = True
        state["report_projection_dirty_at"] = _utc_now()
        state["report_projection_dirty_count"] = int(state.get("report_projection_dirty_count") or 0) + imported_count

    remaining = batch_launcher._waiting_items(state)
    if remaining:
        # This rewrites only the compact AI queue + Batch State. It intentionally
        # does not rebuild CSV/JSON/HTML.
        batch_launcher._activate_batch_queue(config, state, remaining)
        return

    # Normal finalization already rebuilds the complete projection once. Keep
    # that single authoritative boundary instead of doing it after every event.
    state["current_task_id"] = None
    state["status"] = "READY"
    batch_launcher._save(config, state)
    batch_launcher._prepare_next(config, state)


def import_ready_attempts(
    config: ReviewConfig,
    *,
    batch_id: str,
    candidates: Mapping[str, Sequence[Path]],
) -> FastImportResult:
    """Import every currently-ready candidate in one Batch State transaction.

    A malformed result for one task does not block other ready tasks. The newest
    valid attempt for each task wins. Full report projection is deferred while
    any AI work remains.
    """

    state = batch_launcher._load_state(config, batch_id)
    if state.get("ai_queue_mode") != batch_launcher._AI_QUEUE_MODE:
        raise FastCompletionError("fast completion import requires batch_wide_v2")

    imported: list[str] = []
    failed: dict[str, str] = {}
    touched = False

    for task_id, raw_paths in candidates.items():
        task = str(task_id).strip()
        if not task:
            continue
        item = _find_item(state, task)
        if item.get("status") == "COMPLETE":
            continue
        if item.get("status") != "WAITING_FOR_AI":
            failed[task] = f"task is not waiting for AI: {item.get('status')!r}"
            continue
        index_value = str(item.get("index_path") or "").strip()
        if not index_value:
            failed[task] = "task has no trusted index path"
            continue

        paths = _candidate_paths(raw_paths)
        if not paths:
            continue

        for path in paths:
            touched = True
            item["last_import_attempt_at"] = _utc_now()
            item["last_ai_attempt_result"] = str(path)
            try:
                finalize_skill(config, index_path=Path(index_value), ai_result_path=path)
            except Exception as exc:
                item["ai_import_status"] = "FAILED"
                item["last_import_error"] = str(exc)
                failed[task] = str(exc)
                continue

            item["status"] = "COMPLETE"
            item["ai_import_status"] = "COMPLETED"
            item["ai_imported_at"] = _utc_now()
            item["workspace_cleaned"] = True
            item.pop("last_import_error", None)
            failed.pop(task, None)
            imported.append(task)
            break

    if imported:
        _commit_progress(config, state, len(imported))
    elif touched:
        # Preserve diagnostics even when every candidate was malformed, but do
        # not perform any report projection work.
        batch_launcher._save(config, state)

    remaining = len(batch_launcher._waiting_items(state))
    projection_deferred = bool(remaining)
    return FastImportResult(
        batch_id=batch_id,
        imported=tuple(dict.fromkeys(imported)),
        failed=failed,
        remaining_ai=remaining,
        batch_status=str(state.get("status") or "UNKNOWN"),
        report_html=str(state.get("result_html") or state.get("interim_report_html") or "") or None,
        projection_deferred=projection_deferred,
    )


__all__ = ["FastCompletionError", "FastImportResult", "import_ready_attempts"]
