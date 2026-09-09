"""Deterministic final review decision and Chinese status labels.

New reviews are fully automatic: APPROVED, REJECTED or INCOMPLETE. Historical
REVIEW_REQUIRED inputs remain readable but are deterministically rejected rather
than requiring a human approval step.
"""

from __future__ import annotations

from typing import Any, Mapping

from .risk_scoring import PASS_THRESHOLD

OVERALL_APPROVED = "APPROVED"
OVERALL_REJECTED = "REJECTED"
OVERALL_MANUAL_REVIEW = "MANUAL_REVIEW"  # legacy input compatibility only
OVERALL_INCOMPLETE = "INCOMPLETE"
OVERALL_PENDING = "PENDING"

OVERALL_LABEL_ZH = {
    OVERALL_APPROVED: "通过",
    OVERALL_REJECTED: "不通过",
    OVERALL_MANUAL_REVIEW: "历史人工复核项",
    OVERALL_INCOMPLETE: "审查未完成",
    OVERALL_PENDING: "审查中",
}

SECURITY_LABEL_ZH = {
    "PASS": "安全评分通过",
    "BLOCKED": "安全评分不通过",
    "REVIEW_REQUIRED": "历史风险复核项",
    "INCOMPLETE": "安全审查未完成",
    "": "安全结论未形成",
}

QUALITY_LABEL_ZH = {
    "PASS": "质量通过",
    "FAIL": "质量不通过",
    "INCOMPLETE": "质量审查未完成",
    "": "质量结论未形成",
}

SEVERITY_LABEL_ZH = {
    "CRITICAL": "严重",
    "HIGH": "高",
    "MEDIUM": "中",
    "LOW": "低",
    "INFO": "提示",
    "NONE": "无",
    "UNKNOWN": "未知",
    "": "未标注",
}

_REASON_LABELS_ZH = {
    "REVIEW_IN_PROGRESS": "审查流程尚未完成",
    "REVIEW_INCOMPLETE": "审查输入或执行不完整",
    "SECURITY_HARD_BLOCK": "AI 已确认严重安全风险，触发强制阻断",
    "SECURITY_SCORE_BELOW_THRESHOLD": f"安全评分低于 {PASS_THRESHOLD} 分门禁",
    "LEGACY_SECURITY_REVIEW_REJECTED": "历史风险复核状态按自动审批规则判定为不通过",
    "QUALITY_REJECTED": "质量门禁未通过",
    "QUALITY_INCOMPLETE": "质量结论不完整",
    "CANDIDATE_NOT_ELIGIBLE": "未满足候选发布条件",
    "APPROVED": "安全评分与质量门禁均通过",
}


def _key(value: Any) -> str:
    return str(value or "").strip().upper()


def canonical_security_decision(value: Any) -> str:
    key = _key(value)
    if key in {"BLOCK", "BLOCKED", "REJECT", "REJECTED", "DO_NOT_INSTALL"}:
        return "BLOCKED"
    if key in {"REVIEW", "REVIEW_REQUIRED", "MANUAL_REVIEW"}:
        return "REVIEW_REQUIRED"
    if key in {"INCOMPLETE", "ERROR", "TIMEOUT", "MISSING", "INVALID", "UNKNOWN"}:
        return "INCOMPLETE"
    if key in {"PASS", "PASSED", "CLEAN", "OK"}:
        return "PASS"
    return ""


def canonical_quality_decision(value: Any) -> str:
    key = _key(value)
    if key in {"PASS", "PASSED"}:
        return "PASS"
    if key in {"FAIL", "FAILED", "REJECT", "REJECTED"}:
        return "FAIL"
    if key in {"INCOMPLETE", "ERROR", "TIMEOUT", "MISSING", "INVALID", "UNKNOWN"}:
        return "INCOMPLETE"
    return ""


def _score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 100:
        return value
    return None


def derive_overall_decision(
    *,
    final_status: Any,
    security_decision: Any,
    quality_decision: Any,
    candidate_eligible: bool | None = None,
    security_score: Any = None,
    security_hard_block: bool = False,
) -> str:
    final = _key(final_status)
    security = canonical_security_decision(security_decision)
    quality = canonical_quality_decision(quality_decision)
    score = _score(security_score)

    if final in {"", "PENDING", "RUNNING"}:
        return OVERALL_PENDING
    if final == "INCOMPLETE" or final != "COMPLETED":
        return OVERALL_INCOMPLETE

    if security_hard_block:
        return OVERALL_REJECTED
    if security == "INCOMPLETE" or not security:
        return OVERALL_INCOMPLETE

    if score is not None:
        if score < PASS_THRESHOLD:
            return OVERALL_REJECTED
    elif security in {"BLOCKED", "REVIEW_REQUIRED"}:
        # Historical records without a score are still automatic: no manual state.
        return OVERALL_REJECTED

    if quality == "INCOMPLETE" or not quality:
        return OVERALL_INCOMPLETE
    if quality == "FAIL":
        return OVERALL_REJECTED

    if quality == "PASS":
        if candidate_eligible is False:
            return OVERALL_REJECTED
        return OVERALL_APPROVED
    return OVERALL_INCOMPLETE


def overall_reason_codes(
    *,
    overall_decision: Any,
    security_decision: Any,
    quality_decision: Any,
    candidate_eligible: bool | None = None,
    security_score: Any = None,
    security_hard_block: bool = False,
) -> tuple[str, ...]:
    overall = _key(overall_decision)
    security = canonical_security_decision(security_decision)
    quality = canonical_quality_decision(quality_decision)
    score = _score(security_score)

    if overall == OVERALL_PENDING:
        return ("REVIEW_IN_PROGRESS",)
    if overall == OVERALL_INCOMPLETE:
        if quality == "INCOMPLETE":
            return ("QUALITY_INCOMPLETE",)
        return ("REVIEW_INCOMPLETE",)
    if overall == OVERALL_REJECTED:
        reasons: list[str] = []
        if security_hard_block:
            reasons.append("SECURITY_HARD_BLOCK")
        elif score is not None and score < PASS_THRESHOLD:
            reasons.append("SECURITY_SCORE_BELOW_THRESHOLD")
        elif security == "REVIEW_REQUIRED":
            reasons.append("LEGACY_SECURITY_REVIEW_REJECTED")
        elif security == "BLOCKED":
            reasons.append("SECURITY_SCORE_BELOW_THRESHOLD")
        if quality == "FAIL":
            reasons.append("QUALITY_REJECTED")
        if candidate_eligible is False and quality == "PASS" and score is not None and score >= PASS_THRESHOLD:
            reasons.append("CANDIDATE_NOT_ELIGIBLE")
        return tuple(reasons or ["REVIEW_INCOMPLETE"])
    if overall == OVERALL_APPROVED:
        return ("APPROVED",)
    return ("REVIEW_INCOMPLETE",)


def reason_details_zh(codes: tuple[str, ...] | list[str]) -> list[dict[str, str]]:
    return [
        {"code": str(code), "label_zh": _REASON_LABELS_ZH.get(str(code), str(code))}
        for code in codes
    ]


def overall_fields(
    *,
    final_status: Any,
    security_decision: Any,
    quality_decision: Any,
    candidate_eligible: bool | None = None,
    security_score: Any = None,
    security_hard_block: bool = False,
) -> dict[str, Any]:
    security = canonical_security_decision(security_decision)
    quality = canonical_quality_decision(quality_decision)
    score = _score(security_score)
    overall = derive_overall_decision(
        final_status=final_status,
        security_decision=security,
        quality_decision=quality,
        candidate_eligible=candidate_eligible,
        security_score=score,
        security_hard_block=security_hard_block,
    )
    codes = overall_reason_codes(
        overall_decision=overall,
        security_decision=security,
        quality_decision=quality,
        candidate_eligible=candidate_eligible,
        security_score=score,
        security_hard_block=security_hard_block,
    )
    effective_candidate = (
        overall == OVERALL_APPROVED if candidate_eligible is None else candidate_eligible
    )
    return {
        "overall_decision": overall,
        "overall_decision_zh": OVERALL_LABEL_ZH[overall],
        "overall_reason_codes": list(codes),
        "overall_reasons": reason_details_zh(codes),
        "security_decision": security,
        "security_decision_zh": SECURITY_LABEL_ZH.get(security, "安全结论未形成"),
        "quality_decision": quality,
        "quality_decision_zh": QUALITY_LABEL_ZH.get(quality, "质量结论未形成"),
        "candidate_eligible": effective_candidate if _key(final_status) != "PENDING" else None,
    }


def enrich_result(record: Mapping[str, Any]) -> dict[str, Any]:
    candidate = record.get("candidate_eligible")
    if not isinstance(candidate, bool):
        candidate = None
    hard_block = record.get("security_hard_block") is True
    return {
        **dict(record),
        **overall_fields(
            final_status=record.get("final_status") or (
                "COMPLETED" if _key(record.get("review_status")) == "COMPLETED" else
                "INCOMPLETE" if _key(record.get("review_status")) == "INCOMPLETE" else
                "PENDING"
            ),
            security_decision=record.get("security_decision"),
            quality_decision=record.get("quality_decision"),
            candidate_eligible=candidate,
            security_score=record.get("security_score"),
            security_hard_block=hard_block,
        ),
    }


__all__ = [
    "OVERALL_APPROVED",
    "OVERALL_INCOMPLETE",
    "OVERALL_LABEL_ZH",
    "OVERALL_MANUAL_REVIEW",
    "OVERALL_PENDING",
    "OVERALL_REJECTED",
    "QUALITY_LABEL_ZH",
    "SECURITY_LABEL_ZH",
    "SEVERITY_LABEL_ZH",
    "canonical_quality_decision",
    "canonical_security_decision",
    "derive_overall_decision",
    "enrich_result",
    "overall_fields",
    "overall_reason_codes",
    "reason_details_zh",
]
