"""Trusted terminal failure handling for isolated AI reviewers.

A reviewer timeout/failure must never be mistaken for a security PASS. This
module converts a WAITING_FOR_AI Skill into an explicit AI FAILED / FINAL
INCOMPLETE durable result while preserving the already completed static review.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import batch_launcher
from .artifacts import EvidenceStore
from .config import ReviewConfig
from .live_report import write_live_batch_report
from .models import AIReviewStatus, FinalReviewStatus, StaticReviewStatus
from .per_skill import write_skill_result_tables
from .review_state import ReviewPhaseState, build_current_result


class AIFailureError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _find_item(state: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for item in state.get("items", []):
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return item
    raise AIFailureError(f"unknown AI task: {task_id}")


def _finalize_failed_skill(
    config: ReviewConfig,
    *,
    index_path: Path,
    failure_code: str,
    failure_reason: str,
) -> Mapping[str, Any]:
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIFailureError(f"cannot read Skill index: {exc}") from exc
    if not isinstance(index, dict) or index.get("status") != "WAITING_FOR_AI":
        raise AIFailureError("Skill is not waiting for an AI result")
    source = index.get("source")
    if not isinstance(source, Mapping):
        raise AIFailureError("Skill index has no trusted source metadata")
    skill_id = str(source.get("skill_id") or "").strip()
    batch_id = str(index.get("batch_id") or "").strip()
    task_id = str(index.get("task_id") or "").strip()
    if not skill_id or not batch_id or not task_id:
        raise AIFailureError("Skill index is missing skill_id/batch_id/task_id")

    current_path = config.workspace.skills_root / skill_id / "current-result.json"
    result_path = config.workspace.skills_root / skill_id / "review-result.json"
    if not current_path.is_file():
        raise AIFailureError("Skill has no durable current-result.json")
    try:
        prior = json.loads(current_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIFailureError(f"cannot read current Skill result: {exc}") from exc
    if not isinstance(prior, Mapping) or prior.get("source_row_id") != source.get("source_row_id"):
        raise AIFailureError("current Skill result does not match trusted source metadata")

    phase = ReviewPhaseState(
        StaticReviewStatus.COMPLETED,
        AIReviewStatus.FAILED,
        FinalReviewStatus.INCOMPLETE,
    )
    reason = f"{failure_code}: {failure_reason}".strip()
    current = build_current_result(
        source,
        phase=phase,
        static_reports=prior.get("static_reports") if isinstance(prior.get("static_reports"), list) else (),
        findings=prior.get("findings") if isinstance(prior.get("findings"), list) else (),
        static_security_decision=str(prior.get("static_security_decision") or ""),
        security_decision="INCOMPLETE",
        quality_decision="INCOMPLETE",
        quality_score=None,
        ai_review_summary={
            "status": "FAILED",
            "failure_code": failure_code,
            "failure_reason": failure_reason,
        },
        evidence_ref=str(prior.get("evidence_ref") or ""),
        review_policy_version=str(prior.get("review_policy_version") or config.ai.policy_version),
        reviewed_at=_utc_now(),
        failure_reason=reason,
    )
    final = {**current, "result_kind": "FINAL"}
    _atomic_json(current_path, current)
    _atomic_json(result_path, final)

    evidence = EvidenceStore(
        config.workspace.evidence_root,
        batch_id,
        task_id,
        candidate_root=config.workspace.candidate_root,
    )
    evidence.write_json(
        "final-result.json",
        {
            **final,
            "status": "INCOMPLETE",
            "candidate_eligible": False,
            "review_fingerprint": index.get("review_fingerprint"),
            "ai_failure_code": failure_code,
        },
    )
    _atomic_json(
        index_path,
        {
            **index,
            **phase.to_dict(),
            "phase_schema_version": "1.0",
            "status": "INCOMPLETE",
            "current_result_path": str(current_path),
            "result_path": str(result_path),
            "ai_failure_code": failure_code,
            "ai_failure_reason": failure_reason,
            "ai_failed_at": _utc_now(),
        },
    )
    return final


@dataclass(frozen=True, slots=True)
class AIFailureResult:
    batch_id: str
    task_id: str
    failure_code: str
    batch_status: str
    remaining_ai: int
    report_html: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "status": "FAILED",
            "failure_code": self.failure_code,
            "batch_status": self.batch_status,
            "remaining_ai": self.remaining_ai,
            "report_html": self.report_html,
        }


def fail_waiting_ai_task(
    config: ReviewConfig,
    *,
    batch_id: str,
    task_id: str,
    failure_code: str,
    failure_reason: str,
) -> AIFailureResult:
    """Fail exactly one WAITING_FOR_AI task and continue the batch safely."""

    state = batch_launcher._load_state(config, batch_id)
    if state.get("ai_queue_mode") != batch_launcher._AI_QUEUE_MODE:
        raise AIFailureError("AI failure handling is only supported for batch_wide_v2 batches")
    item = _find_item(state, task_id)
    if item.get("status") == "COMPLETE":
        return AIFailureResult(
            batch_id=batch_id,
            task_id=task_id,
            failure_code=str(item.get("ai_failure_code") or failure_code),
            batch_status=str(state.get("status") or "UNKNOWN"),
            remaining_ai=len(batch_launcher._waiting_items(state)),
            report_html=str(state.get("result_html") or state.get("interim_report_html") or "") or None,
        )
    if item.get("status") != "WAITING_FOR_AI":
        raise AIFailureError(f"task {task_id} is not waiting for AI: {item.get('status')!r}")
    index_value = item.get("index_path")
    if not index_value:
        raise AIFailureError(f"task {task_id} has no trusted index path")

    _finalize_failed_skill(
        config,
        index_path=Path(str(index_value)),
        failure_code=failure_code,
        failure_reason=failure_reason,
    )
    item["status"] = "COMPLETE"
    item["ai_import_status"] = "FAILED"
    item["ai_failure_code"] = failure_code
    item["ai_failure_reason"] = failure_reason
    item["ai_failed_at"] = _utc_now()

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
        batch_launcher._prepare_next(config, state)

    return AIFailureResult(
        batch_id=batch_id,
        task_id=task_id,
        failure_code=failure_code,
        batch_status=str(state.get("status") or "UNKNOWN"),
        remaining_ai=len(batch_launcher._waiting_items(state)),
        report_html=str(state.get("result_html") or live.paths.html),
    )


__all__ = ["AIFailureError", "AIFailureResult", "fail_waiting_ai_task"]
