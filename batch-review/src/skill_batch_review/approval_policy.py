"""Automatic approval policy built on normalized review evidence.

The legacy policy is still used for normalization/completeness/quality checks,
but static scanner REVIEW/BLOCK recommendations are treated as signals rather
than final authority. Final security outcome is determined by the deterministic
security score plus hard-block rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import review_policy as legacy
from .risk_scoring import PASS_THRESHOLD, calculate_security_score


@dataclass(frozen=True, slots=True)
class ApprovalPolicyResult:
    security_decision: str
    quality_decision: str
    quality_score: int | None
    quality_threshold: int
    quality_eligible: bool
    candidate_eligible: bool
    findings: tuple[dict[str, Any], ...]
    security_findings: tuple[dict[str, Any], ...]
    quality_findings: tuple[dict[str, Any], ...]
    security_score: int
    security_risk_deduction: int
    security_risk_level: str
    security_hard_block: bool
    security_scoring_rules_version: str
    security_deductions: tuple[dict[str, Any], ...]
    reasons: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    incomplete_reasons: tuple[str, ...] = ()

    @property
    def security_conclusion(self) -> str:
        return self.security_decision

    def to_dict(self) -> dict[str, Any]:
        return {
            "security_decision": self.security_decision,
            "security_conclusion": self.security_decision,
            "quality_decision": self.quality_decision,
            "quality_score": self.quality_score,
            "quality_threshold": self.quality_threshold,
            "quality_eligible": self.quality_eligible,
            "candidate_eligible": self.candidate_eligible,
            "security_score": self.security_score,
            "security_risk_deduction": self.security_risk_deduction,
            "security_risk_level": self.security_risk_level,
            "security_hard_block": self.security_hard_block,
            "security_scoring_rules_version": self.security_scoring_rules_version,
            "security_deductions": [dict(item) for item in self.security_deductions],
            "findings": [dict(item) for item in self.findings],
            "security_findings": [dict(item) for item in self.security_findings],
            "quality_findings": [dict(item) for item in self.quality_findings],
            "reasons": list(self.reasons),
            "blocking_reasons": list(self.blocking_reasons),
            "review_reasons": list(self.review_reasons),
            "incomplete_reasons": list(self.incomplete_reasons),
        }


def _structural_reject_reasons(reasons: tuple[str, ...]) -> list[str]:
    markers = (
        "branch content conflict",
        "special content",
    )
    return [reason for reason in reasons if any(marker in reason for marker in markers)]


def evaluate_policy(*args: Any, **kwargs: Any) -> ApprovalPolicyResult:
    base = legacy.evaluate_policy(*args, **kwargs)
    ai_review = kwargs.get("ai_review")
    if ai_review is None and len(args) >= 2:
        ai_review = args[1]
    ai_security = ai_review.get("security_review") if isinstance(ai_review, Mapping) else None
    ai_verdict = ai_security.get("verdict") if isinstance(ai_security, Mapping) else None

    score = calculate_security_score(base.security_findings, ai_security_verdict=ai_verdict)
    structural_rejects = _structural_reject_reasons(base.review_reasons)

    if score.hard_block or structural_rejects:
        security_decision = legacy.SECURITY_BLOCK
    elif base.incomplete_reasons:
        security_decision = legacy.SECURITY_INCOMPLETE
    elif score.security_score < PASS_THRESHOLD:
        security_decision = legacy.SECURITY_BLOCK
    else:
        security_decision = legacy.SECURITY_PASS

    quality_eligible = base.quality_eligible
    candidate_eligible = (
        security_decision == legacy.SECURITY_PASS
        and quality_eligible
        and not base.incomplete_reasons
        and not structural_rejects
    )

    blocking_reasons: list[str] = []
    if score.hard_block:
        blocking_reasons.append("security hard-block rule triggered")
    if score.security_score < PASS_THRESHOLD:
        blocking_reasons.append(
            f"security score {score.security_score} is below threshold {PASS_THRESHOLD}"
        )
    blocking_reasons.extend(structural_rejects)
    reasons = [*blocking_reasons, *base.incomplete_reasons]
    if not quality_eligible:
        reasons.append("quality score is below the candidate threshold")

    return ApprovalPolicyResult(
        security_decision=security_decision,
        quality_decision=base.quality_decision,
        quality_score=base.quality_score,
        quality_threshold=base.quality_threshold,
        quality_eligible=quality_eligible,
        candidate_eligible=candidate_eligible,
        findings=base.findings,
        security_findings=base.security_findings,
        quality_findings=base.quality_findings,
        security_score=score.security_score,
        security_risk_deduction=score.risk_deduction,
        security_risk_level=score.risk_level,
        security_hard_block=score.hard_block,
        security_scoring_rules_version=score.rules_version,
        security_deductions=score.deductions,
        reasons=tuple(reasons),
        blocking_reasons=tuple(blocking_reasons),
        review_reasons=(),
        incomplete_reasons=base.incomplete_reasons,
    )


__all__ = ["ApprovalPolicyResult", "evaluate_policy"]
