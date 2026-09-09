"""Deterministic security score for combined static + AI review evidence.

The score starts at 100 and deducts risk points. Static scanners are treated as
signals, not final authorities: scanner-only findings have lower source weights,
while independent AI findings and cross-source corroboration carry more weight.
Repeated findings from the same behavior family decay instead of accumulating
linearly.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SCORING_RULES_VERSION = "security-score-v1"
START_SCORE = 100
PASS_THRESHOLD = 60

SEVERITY_DEDUCTION = {
    "INFO": 0.0,
    "LOW": 3.0,
    "MEDIUM": 10.0,
    "HIGH": 25.0,
    "CRITICAL": 50.0,
    "UNKNOWN": 10.0,
}
CONFIDENCE_FACTOR = {
    "LOW": 0.25,
    "MEDIUM": 0.60,
    "HIGH": 1.00,
    "UNKNOWN": 0.50,
}
SOURCE_FACTOR = {
    "CISCO_ONLY": 0.45,
    "SKILLSPECTOR_ONLY": 0.55,
    "AI_ONLY": 1.00,
    "STATIC_BOTH": 1.00,
    "AI_PLUS_STATIC": 1.40,
    "ALL_THREE": 1.70,
    "OTHER": 0.50,
}
REPETITION_FACTORS = (1.0, 0.5, 0.25)
REPETITION_TAIL_FACTOR = 0.10


@dataclass(frozen=True, slots=True)
class SecurityScoreResult:
    security_score: int
    risk_deduction: int
    risk_level: str
    hard_block: bool
    rules_version: str
    deductions: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "security_score": self.security_score,
            "security_risk_deduction": self.risk_deduction,
            "security_risk_level": self.risk_level,
            "security_hard_block": self.hard_block,
            "security_scoring_rules_version": self.rules_version,
            "security_deductions": [dict(item) for item in self.deductions],
        }


def scoring_rules() -> dict[str, Any]:
    return {
        "version": SCORING_RULES_VERSION,
        "start_score": START_SCORE,
        "pass_threshold": PASS_THRESHOLD,
        "severity_deduction": dict(SEVERITY_DEDUCTION),
        "confidence_factor": dict(CONFIDENCE_FACTOR),
        "source_factor": dict(SOURCE_FACTOR),
        "repetition_factor": {
            "first": 1.0,
            "second": 0.5,
            "third": 0.25,
            "fourth_and_later": REPETITION_TAIL_FACTOR,
        },
        "hard_block": [
            "AI security verdict BLOCK",
            "AI CRITICAL finding with HIGH confidence",
        ],
        "final_gate": "complete AND security_score >= 60 AND quality_score >= 70 AND no hard_block",
    }


def _key(value: Any) -> str:
    return str(value or "").strip().upper()


def _sources(finding: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    raw = finding.get("source_scanners")
    if isinstance(raw, list):
        result.update(_key(value) for value in raw if value)
    source = _key(finding.get("source_scanner"))
    if source:
        result.add(source)
    refs = finding.get("source_references")
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, Mapping):
                value = _key(ref.get("source"))
                if value:
                    result.add(value)
    aliases = {
        "CISCO": "CISCO_AI_SKILL_SCANNER",
        "SKILLSPECTOR": "NVIDIA_SKILLSPECTOR",
        "AI": "AI_REVIEW",
    }
    return {aliases.get(value, value) for value in result}


def _source_class(sources: set[str]) -> str:
    cisco = "CISCO_AI_SKILL_SCANNER" in sources
    inspector = "NVIDIA_SKILLSPECTOR" in sources
    ai = "AI_REVIEW" in sources
    if ai and cisco and inspector:
        return "ALL_THREE"
    if ai and (cisco or inspector):
        return "AI_PLUS_STATIC"
    if cisco and inspector:
        return "STATIC_BOTH"
    if ai:
        return "AI_ONLY"
    if cisco:
        return "CISCO_ONLY"
    if inspector:
        return "SKILLSPECTOR_ONLY"
    return "OTHER"


def _cluster_key(finding: Mapping[str, Any]) -> tuple[str, str]:
    category = _key(finding.get("category")) or "OTHER"
    path = str(finding.get("file_path") or finding.get("path") or "").replace("\\", "/").strip()
    return category, path


def _rule_family(finding: Mapping[str, Any]) -> str:
    category = _key(finding.get("category")) or "OTHER"
    rule = str(finding.get("source_rule_id") or "").strip()
    return f"{category}|{rule or '*'}"


def _cluster_id(key: tuple[str, str], sources: set[str]) -> str:
    value = json.dumps([*key, sorted(sources)], ensure_ascii=False, separators=(",", ":"))
    return "risk-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _risk_level(score: int) -> str:
    if score >= 90:
        return "LOW"
    if score >= 75:
        return "MEDIUM"
    if score >= PASS_THRESHOLD:
        return "HIGH"
    return "CRITICAL"


def _repetition_factor(index: int) -> float:
    if index < len(REPETITION_FACTORS):
        return REPETITION_FACTORS[index]
    return REPETITION_TAIL_FACTOR


def calculate_security_score(
    findings: Iterable[Mapping[str, Any]],
    *,
    ai_security_verdict: Any = None,
) -> SecurityScoreResult:
    security_findings = [
        finding for finding in findings
        if str(finding.get("domain") or "SECURITY").upper() in {"SECURITY", "MIXED"}
    ]
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for finding in security_findings:
        grouped.setdefault(_cluster_key(finding), []).append(finding)

    provisional: list[dict[str, Any]] = []
    hard_block = _key(ai_security_verdict) in {"BLOCK", "BLOCKED", "REJECT", "REJECTED"}

    for cluster_key, items in grouped.items():
        severity_order = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "UNKNOWN": 2, "HIGH": 3, "CRITICAL": 4}
        dominant = max(items, key=lambda item: severity_order.get(_key(item.get("severity")), 2))
        severity = _key(dominant.get("severity")) or "UNKNOWN"
        confidence = max(
            (_key(item.get("confidence")) or "UNKNOWN" for item in items),
            key=lambda value: CONFIDENCE_FACTOR.get(value, 0.5),
        )
        sources: set[str] = set()
        rule_ids: set[str] = set()
        for item in items:
            sources.update(_sources(item))
            if item.get("source_rule_id"):
                rule_ids.add(str(item.get("source_rule_id")))
        source_class = _source_class(sources)
        base = SEVERITY_DEDUCTION.get(severity, SEVERITY_DEDUCTION["UNKNOWN"])
        confidence_factor = CONFIDENCE_FACTOR.get(confidence, CONFIDENCE_FACTOR["UNKNOWN"])
        source_factor = SOURCE_FACTOR[source_class]
        raw = base * confidence_factor * source_factor
        ai_present = "AI_REVIEW" in sources
        if ai_present and severity == "CRITICAL" and confidence == "HIGH":
            hard_block = True
        if ai_present and len(sources) > 1:
            confirmation = "CORROBORATED"
        elif ai_present:
            confirmation = "AI_CONFIRMED"
        elif len(sources) > 1:
            confirmation = "STATIC_CORROBORATED"
        else:
            confirmation = "SCANNER_ONLY"
        provisional.append(
            {
                "cluster_id": _cluster_id(cluster_key, sources),
                "category": cluster_key[0],
                "path": cluster_key[1],
                "severity": severity,
                "confidence": confidence,
                "sources": sorted(sources),
                "source_class": source_class,
                "confirmation_status": confirmation,
                "rule_ids": sorted(rule_ids),
                "family": _rule_family(dominant),
                "base_deduction": base,
                "confidence_factor": confidence_factor,
                "source_factor": source_factor,
                "raw_deduction": raw,
            }
        )

    families: dict[str, list[dict[str, Any]]] = {}
    for item in provisional:
        families.setdefault(str(item["family"]), []).append(item)

    deductions: list[dict[str, Any]] = []
    for family_items in families.values():
        family_items.sort(key=lambda item: float(item["raw_deduction"]), reverse=True)
        for index, item in enumerate(family_items):
            repeat_factor = _repetition_factor(index)
            deduction = round(float(item["raw_deduction"]) * repeat_factor, 2)
            deductions.append(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"family", "raw_deduction"}
                }
                | {
                    "repetition_index": index + 1,
                    "repetition_factor": repeat_factor,
                    "deduction": deduction,
                }
            )

    deductions.sort(key=lambda item: float(item["deduction"]), reverse=True)
    total = min(100, int(math.ceil(sum(float(item["deduction"]) for item in deductions))))
    if hard_block:
        total = 100
    score = max(0, START_SCORE - total)
    return SecurityScoreResult(
        security_score=score,
        risk_deduction=total,
        risk_level=_risk_level(score),
        hard_block=hard_block,
        rules_version=SCORING_RULES_VERSION,
        deductions=tuple(deductions),
    )


__all__ = [
    "PASS_THRESHOLD",
    "SCORING_RULES_VERSION",
    "SecurityScoreResult",
    "calculate_security_score",
    "scoring_rules",
]
