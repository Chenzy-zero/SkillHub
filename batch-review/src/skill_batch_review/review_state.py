"""Phase-aware state helpers for one Skill review.

The batch launcher historically used one coarse ``status`` value for download,
static scanning, AI review, and trusted finalization.  This module introduces
orthogonal phase state without changing the legacy launcher protocol yet.

It is deliberately small and deterministic so both the per-Skill pipeline and
reporting layer can consume the same transition rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import AIReviewStatus, FinalReviewStatus, StaticReviewStatus


_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


@dataclass(frozen=True, slots=True)
class ReviewPhaseState:
    static_status: StaticReviewStatus
    ai_status: AIReviewStatus
    final_status: FinalReviewStatus

    def __post_init__(self) -> None:
        _validate_transition(self.static_status, self.ai_status, self.final_status)

    def to_dict(self) -> dict[str, str]:
        return {
            "static_status": self.static_status.value,
            "ai_status": self.ai_status.value,
            "final_status": self.final_status.value,
        }


def _validate_transition(
    static_status: StaticReviewStatus,
    ai_status: AIReviewStatus,
    final_status: FinalReviewStatus,
) -> None:
    if static_status in {StaticReviewStatus.PENDING, StaticReviewStatus.RUNNING}:
        if ai_status not in {AIReviewStatus.NOT_REQUIRED, AIReviewStatus.PENDING}:
            raise ValueError("AI review cannot start before static preparation completes")
        if final_status is not FinalReviewStatus.PENDING:
            raise ValueError("final result cannot be formed before static preparation completes")

    if static_status is StaticReviewStatus.INCOMPLETE:
        if ai_status is not AIReviewStatus.NOT_REQUIRED:
            raise ValueError("incomplete static preparation must not dispatch AI review")
        if final_status is not FinalReviewStatus.INCOMPLETE:
            raise ValueError("incomplete static preparation must yield an incomplete final state")

    if ai_status in {
        AIReviewStatus.DISPATCHED,
        AIReviewStatus.COMPLETED,
        AIReviewStatus.FAILED,
    } and static_status is not StaticReviewStatus.COMPLETED:
        raise ValueError("AI phase requires completed static preparation")

    if final_status is FinalReviewStatus.COMPLETED:
        if static_status is not StaticReviewStatus.COMPLETED:
            raise ValueError("completed final state requires completed static preparation")
        if ai_status not in {AIReviewStatus.COMPLETED, AIReviewStatus.NOT_REQUIRED}:
            raise ValueError("completed final state requires completed or unnecessary AI review")

    if final_status is FinalReviewStatus.INCOMPLETE:
        allowed = (
            static_status is StaticReviewStatus.INCOMPLETE
            or (
                static_status is StaticReviewStatus.COMPLETED
                and ai_status is AIReviewStatus.FAILED
            )
        )
        if not allowed:
            raise ValueError("incomplete final state requires an incomplete static or failed AI phase")


def finding_summary(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a normalized count/max-severity summary for report projection."""

    counts = {severity: 0 for severity in _SEVERITIES}
    normalized: list[dict[str, Any]] = []
    for finding in findings:
        item = dict(finding)
        severity = str(item.get("severity") or "INFO").upper()
        if severity not in counts:
            severity = "INFO"
        item["severity"] = severity
        counts[severity] += 1
        normalized.append(item)
    maximum = next((severity for severity in _SEVERITIES if counts[severity]), "NONE")
    return {
        "findings": normalized,
        "finding_counts": counts,
        "finding_count": sum(counts.values()),
        "max_severity": maximum,
    }


def build_current_result(
    source: Mapping[str, Any],
    *,
    phase: ReviewPhaseState,
    static_reports: Sequence[Mapping[str, Any]] = (),
    findings: Sequence[Mapping[str, Any]] = (),
    static_security_decision: str = "",
    security_decision: str = "",
    quality_decision: str = "",
    quality_score: int | None = None,
    ai_review_summary: Mapping[str, Any] | None = None,
    evidence_ref: str = "",
    review_policy_version: str = "",
    reviewed_at: str | None = None,
    failure_reason: str = "",
) -> dict[str, Any]:
    """Build the durable current-result projection for one Skill.

    A pending final phase is intentionally prohibited from carrying a final
    security or quality decision.  Static-only findings live beside the
    explicit ``static_security_decision`` until trusted finalization occurs.
    """

    if phase.final_status is FinalReviewStatus.PENDING:
        if security_decision or quality_decision or quality_score is not None:
            raise ValueError("pending final state must not expose a final decision or quality score")
    elif phase.final_status is FinalReviewStatus.COMPLETED and not security_decision:
        raise ValueError("completed final state requires a security decision")

    payload = {
        **dict(source),
        "schema_version": "1.0",
        "result_kind": "CURRENT",
        **phase.to_dict(),
        "review_status": (
            "COMPLETED"
            if phase.final_status is FinalReviewStatus.COMPLETED
            else "INCOMPLETE"
            if phase.final_status is FinalReviewStatus.INCOMPLETE
            else "IN_PROGRESS"
        ),
        "static_security_decision": static_security_decision,
        "security_decision": security_decision,
        "quality_decision": quality_decision,
        "quality_score": quality_score,
        "static_reports": [dict(report) for report in static_reports],
        "ai_review_summary": dict(ai_review_summary or {}),
        **finding_summary(findings),
        "evidence_ref": evidence_ref,
        "review_policy_version": review_policy_version,
        "reviewed_at": reviewed_at,
        "failure_reason": failure_reason,
    }
    return payload


def static_waiting_for_ai(
    source: Mapping[str, Any],
    *,
    static_reports: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    static_security_decision: str,
    evidence_ref: str,
    review_policy_version: str,
) -> dict[str, Any]:
    """Build the common post-static/pre-AI projection."""

    return build_current_result(
        source,
        phase=ReviewPhaseState(
            StaticReviewStatus.COMPLETED,
            AIReviewStatus.PENDING,
            FinalReviewStatus.PENDING,
        ),
        static_reports=static_reports,
        findings=findings,
        static_security_decision=static_security_decision,
        evidence_ref=evidence_ref,
        review_policy_version=review_policy_version,
        ai_review_summary={"status": AIReviewStatus.PENDING.value},
    )


__all__ = [
    "ReviewPhaseState",
    "build_current_result",
    "finding_summary",
    "static_waiting_for_ai",
]
