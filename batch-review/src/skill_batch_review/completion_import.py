"""Trusted single-task AI completion import for batch-wide review.

A reviewer only writes its expected result.  This module validates and finalizes
one explicitly completed task, refreshes durable reports, and leaves every other
Task untouched.  It is intentionally idempotent for already-completed tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import batch_launcher
from .config import ReviewConfig
from .live_report import write_live_batch_report
from .per_skill import finalize_skill, write_skill_result_tables


class CompletionImportError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CompletionImportResult:
    batch_id: str
    task_id: str
    status: str
    batch_status: str
    remaining_ai: int
    report_html: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "status": self.status,
            "batch_status": self.batch_status,
            "remaining_ai": self.remaining_ai,
            "report_html": self.report_html,
        }


def _find_item(state: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for item in state.get("items", []):
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return item
    raise CompletionImportError(f"unknown AI task: {task_id}")


def import_completed_task(
    config: ReviewConfig,
    *,
    batch_id: str,
    task_id: str,
) -> CompletionImportResult:
    """Finalize exactly one completion event and refresh the current projection."""

    state = batch_launcher._load_state(config, batch_id)
    if state.get("ai_queue_mode") != batch_launcher._AI_QUEUE_MODE:
        raise CompletionImportError(
            "completion-driven import is only supported for batch_wide_v2 batches"
        )
    item = _find_item(state, task_id)
    if item.get("status") == "COMPLETE":
        return CompletionImportResult(
            batch_id=batch_id,
            task_id=task_id,
            status="ALREADY_IMPORTED",
            batch_status=str(state.get("status") or "UNKNOWN"),
            remaining_ai=len(batch_launcher._waiting_items(state)),
            report_html=str(state.get("result_html") or state.get("interim_report_html") or "") or None,
        )
    if item.get("status") != "WAITING_FOR_AI":
        raise CompletionImportError(
            f"task {task_id} is not waiting for AI: {item.get('status')!r}"
        )
    result_path = Path(str(item.get("ai_result_path") or ""))
    if not item.get("ai_result_path") or not result_path.is_file():
        raise CompletionImportError(
            f"AI_RESULT_MISSING: task {task_id} has no expected result file"
        )
    index_path = Path(str(item.get("index_path") or ""))
    if not item.get("index_path"):
        raise CompletionImportError(f"task {task_id} has no trusted index path")

    item["last_import_attempt_at"] = _utc_now()
    try:
        finalize_skill(
            config,
            index_path=index_path,
            ai_result_path=result_path,
        )
    except Exception as exc:
        # Preserve the failed file for diagnosis.  The Task stays WAITING and
        # can be retried after the operator replaces/fixes that exact result.
        item["ai_import_status"] = "FAILED"
        item["last_import_error"] = str(exc)
        batch_launcher._save(config, state)
        raise CompletionImportError(
            f"AI_IMPORT_FAILED[{task_id}]: {exc}"
        ) from exc

    item["status"] = "COMPLETE"
    item["ai_import_status"] = "COMPLETED"
    item["ai_imported_at"] = _utc_now()
    item["workspace_cleaned"] = True
    item.pop("last_import_error", None)
    document = batch_launcher._inventory(config)
    csv_path, json_path = write_skill_result_tables(config, document, batch_id=batch_id)
    live = write_live_batch_report(config, document, batch_id=batch_id)
    state["result_csv"] = str(csv_path)
    state["result_json"] = str(json_path)
    state["result_html"] = str(live.paths.html)
    state["report_status"] = live.report_status

    remaining = batch_launcher._waiting_items(state)
    if remaining:
        batch_launcher._activate_batch_queue(config, state, remaining)
    else:
        state["current_task_id"] = None
        state["status"] = "READY"
        batch_launcher._save(config, state)
        # Static preparation is already complete for batch_wide_v2.  With no
        # remaining AI tasks this call deterministically promotes the report to
        # FINAL/COMPLETE without downloading or scanning again.
        batch_launcher._prepare_next(config, state)

    return CompletionImportResult(
        batch_id=batch_id,
        task_id=task_id,
        status="IMPORTED",
        batch_status=str(state.get("status") or "UNKNOWN"),
        remaining_ai=len(batch_launcher._waiting_items(state)),
        report_html=str(state.get("result_html") or live.paths.html),
    )


__all__ = [
    "CompletionImportError",
    "CompletionImportResult",
    "import_completed_task",
]
