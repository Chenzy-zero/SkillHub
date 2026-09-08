"""Phase-aware state helpers for one Skill review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import AIReviewStatus, FinalReviewStatus, StaticReviewStatus
from .overall_decision import canonical_security_decision, overall_fields

_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
_BLOCK_DECISIONS = {"BLOCK", "BLOCKED", "DO_NOT_INSTALL", "FAIL", "FAILED"}
_REVIEW_DECISIONS = {"REVIEW", "REVIEW_REQUIRED", "MANUAL_REVIEW"}
_UNCERTAIN_DECISIONS = {"UNKNOWN", "INCOMPLETE", "ERROR", "TIMEOUT", "MISSING", "INVALID"}
_DECISION_ORDER = {"PASS": 0, "REVIEW_REQUIRED": 1, "BLOCK": 2}


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
    if ai_status in {AIReviewStatus.DISPATCHED, AIReviewStatus.COMPLETED, AIReviewStatus.FAILED} and static_status is not StaticReviewStatus.COMPLETED:
        raise ValueError("AI phase requires completed static preparation")
    if final_status is FinalReviewStatus.COMPLETED:
        if static_status is not StaticReviewStatus.COMPLETED:
            raise ValueError("completed final state requires completed static preparation")
        if ai_status not in {AIReviewStatus.COMPLETED, AIReviewStatus.NOT_REQUIRED}:
            raise ValueError("completed final state requires completed or unnecessary AI review")
    if final_status is FinalReviewStatus.INCOMPLETE:
        allowed = static_status is StaticReviewStatus.INCOMPLETE or (
            static_status is StaticReviewStatus.COMPLETED and ai_status is AIReviewStatus.FAILED
        )
        if not allowed:
            raise ValueError("incomplete final state requires an incomplete static or failed AI phase")


def finding_summary(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
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


def _derive_static_security_decision(
    findings: Sequence[Mapping[str, Any]],
    *,
    static_reports: Sequence[Mapping[str, Any]],
    medium_requires_review: bool,
) -> str:
    decisions = {
        str(report.get("decision") or "").strip().upper()
        for report in static_reports
        if isinstance(report, Mapping)
    }
    if decisions & _BLOCK_DECISIONS:
        return "BLOCK"
    if decisions & (_REVIEW_DECISIONS | _UNCERTAIN_DECISIONS):
        return "REVIEW_REQUIRED"

    severities = {
        str(finding.get("severity") or "UNKNOWN").strip().upper()
        for finding in findings
    }
    if "CRITICAL" in severities:
        return "BLOCK"
    if "HIGH" in severities or "UNKNOWN" in severities:
        return "REVIEW_REQUIRED"
    if medium_requires_review and "MEDIUM" in severities:
        return "REVIEW_REQUIRED"
    return "PASS"


def static_security_decision(
    findings: Sequence[Mapping[str, Any]],
    *,
    static_reports: Sequence[Mapping[str, Any]] = (),
    medium_requires_review: bool = True,
) -> str:
    """Return a scanner-only projection without pretending the AI stage is done.

    Scanner-level decisions are preserved because a tool can require review even
    when its normalized findings are LOW/INFO. An uncertain scanner decision is
    conservatively projected as REVIEW_REQUIRED here; true scanner incompleteness
    is handled by the static phase before this helper is called.
    """

    return _derive_static_security_decision(
        findings,
        static_reports=static_reports,
        medium_requires_review=medium_requires_review,
    )


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
    if phase.final_status is FinalReviewStatus.PENDING:
        if security_decision or quality_decision or quality_score is not None:
            raise ValueError("pending final state must not expose a final decision or quality score")
    elif phase.final_status is FinalReviewStatus.COMPLETED and not security_decision:
        raise ValueError("completed final state requires a security decision")

    decisions = overall_fields(
        final_status=phase.final_status.value,
        security_decision=security_decision,
        quality_decision=quality_decision,
        candidate_eligible=None,
    )
    return {
        **dict(source),
        "schema_version": "1.1",
        "result_kind": "CURRENT",
        **phase.to_dict(),
        "review_status": (
            "COMPLETED"
            if phase.final_status is FinalReviewStatus.COMPLETED
            else "INCOMPLETE"
            if phase.final_status is FinalReviewStatus.INCOMPLETE
            else "IN_PROGRESS"
        ),
        "static_security_decision": canonical_security_decision(static_security_decision),
        **decisions,
        "quality_score": quality_score,
        "static_reports": [dict(report) for report in static_reports],
        "ai_review_summary": dict(ai_review_summary or {}),
        **finding_summary(findings),
        "evidence_ref": evidence_ref,
        "review_policy_version": review_policy_version,
        "reviewed_at": reviewed_at,
        "failure_reason": failure_reason,
    }


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

    report_decision = _derive_static_security_decision(
        findings,
        static_reports=static_reports,
        medium_requires_review=True,
    )
    effective_decision = max(
        (static_security_decision, report_decision),
        key=lambda value: _DECISION_ORDER.get(value, 1),
    )
    return build_current_result(
        source,
        phase=ReviewPhaseState(
            StaticReviewStatus.COMPLETED,
            AIReviewStatus.PENDING,
            FinalReviewStatus.PENDING,
        ),
        static_reports=static_reports,
        findings=findings,
        static_security_decision=effective_decision,
        evidence_ref=evidence_ref,
        review_policy_version=review_policy_version,
        ai_review_summary={"status": AIReviewStatus.PENDING.value},
    )


__all__ = [
    "ReviewPhaseState",
    "build_current_result",
    "finding_summary",
    "static_security_decision",
    "static_waiting_for_ai",
]
