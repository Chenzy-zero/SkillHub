"""Normalize review findings and calculate the batch review gate.

This module is deliberately a pure, standard-library-only policy layer.  It
does not run a scanner, invoke a model, read a Skill directory, or write an
artifact.  The caller supplies the dictionaries returned by
``ScanResult.to_dict()`` and the already schema-validated AI review JSON.

The policy keeps two questions separate:

* security asks whether the reviewed content may proceed; and
* quality reports the score supplied by the validated AI review.

In particular, the quality score is never adjusted for security findings and
never used to soften a security decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


SECURITY_PASS = "PASS"
SECURITY_REVIEW_REQUIRED = "REVIEW_REQUIRED"
SECURITY_BLOCK = "BLOCK"
SECURITY_INCOMPLETE = "INCOMPLETE"

QUALITY_PASS = "PASS"
QUALITY_FAIL = "FAIL"
QUALITY_INCOMPLETE = "INCOMPLETE"

DEFAULT_QUALITY_THRESHOLD = 70

_SEVERITY_ORDER = {
    "UNKNOWN": 0,
    "INFO": 1,
    "LOW": 2,
    "MEDIUM": 3,
    "HIGH": 4,
    "CRITICAL": 5,
}

_SCANNERS = {
    "CISCO_AI_SKILL_SCANNER",
    "NVIDIA_SKILLSPECTOR",
}

_CATEGORY_ALIASES = {
    "PROMPT_INJECTION": "PROMPT_OR_TOOL_INJECTION",
    "PROMPT_ATTACK": "PROMPT_OR_TOOL_INJECTION",
    "TOOL_POISONING": "PROMPT_OR_TOOL_INJECTION",
    "PROMPT_OR_TOOL_INJECTION": "PROMPT_OR_TOOL_INJECTION",
    "COMMAND_EXECUTION": "COMMAND_OR_CODE_EXECUTION",
    "CODE_EXECUTION": "COMMAND_OR_CODE_EXECUTION",
    "SHELL_EXECUTION": "COMMAND_OR_CODE_EXECUTION",
    "COMMAND_OR_CODE_EXECUTION": "COMMAND_OR_CODE_EXECUTION",
    "FILE_ACCESS": "FILES_AND_SECRETS",
    "SECRET_ACCESS": "FILES_AND_SECRETS",
    "SECRETS": "FILES_AND_SECRETS",
    "FILES_AND_SECRETS": "FILES_AND_SECRETS",
    "NETWORK": "NETWORK_OR_DATA_MOVEMENT",
    "DATA_EXFILTRATION": "NETWORK_OR_DATA_MOVEMENT",
    "NETWORK_OR_DATA_MOVEMENT": "NETWORK_OR_DATA_MOVEMENT",
    "DEPENDENCY": "DEPENDENCY_OR_INSTALLATION",
    "INSTALLATION": "DEPENDENCY_OR_INSTALLATION",
    "DEPENDENCY_OR_INSTALLATION": "DEPENDENCY_OR_INSTALLATION",
    "PERSISTENCE": "PERSISTENCE_OR_CONCEALMENT",
    "OBFUSCATION": "PERSISTENCE_OR_CONCEALMENT",
    "PERSISTENCE_OR_CONCEALMENT": "PERSISTENCE_OR_CONCEALMENT",
    "PERMISSION": "PERMISSION_FIT",
    "PERMISSIONS": "PERMISSION_FIT",
    "PERMISSION_FIT": "PERMISSION_FIT",
    "IDENTITY": "IDENTITY_AND_INTENT",
    "IDENTITY_AND_INTENT": "IDENTITY_AND_INTENT",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|authorization)\s*[:=]\s*)[^\s,;]+"
    ),
)
_SPACE_RE = re.compile(r"\s+")
_KEY_RE = re.compile(r"[^A-Z0-9]+")


class ReviewPolicyError(ValueError):
    """Raised when policy input cannot be represented safely."""


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (Mapping, list, tuple)):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            value = str(value)
    result = str(value).strip()
    return result or None


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    """Read aliases without treating an explicit false/zero as absent."""

    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _canonical_key(value: Any) -> str:
    text = _text(value) or ""
    return _KEY_RE.sub("_", text.upper()).strip("_")


def normalize_category(value: Any) -> str:
    """Return a stable category name while retaining unknown categories."""

    key = _canonical_key(value)
    return _CATEGORY_ALIASES.get(key, key or "OTHER")


def normalize_path(value: Any) -> str:
    """Normalize a report path for display and finding fingerprints.

    This is lexical normalization only.  It intentionally does not resolve a
    filesystem path and does not discard ``..`` components: a report that
    points outside the Skill root must remain visible as such.
    """

    text = _text(value)
    if not text:
        return ""
    text = text.replace("\\", "/")
    absolute = text.startswith("/")
    parts: list[str] = []
    for part in text.split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        parts.append(part)
    result = "/".join(parts)
    if absolute:
        result = "/" + result
    return result or ("/" if absolute else ".")


def _redact_evidence(value: Any) -> str:
    text = _text(value) or ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + "<REDACTED>", text)
    return _SPACE_RE.sub(" ", text).strip()


def normalize_evidence(value: Any, *, fallback: Any = None) -> str:
    """Return a short, redacted evidence summary suitable for reports."""

    text = _redact_evidence(value)
    if not text:
        text = _redact_evidence(fallback)
    # A summary is evidence for a human, not a source-code archive.  The full
    # redacted value is still represented by the fingerprint below.
    return text if len(text) <= 500 else text[:497] + "..."


def normalize_severity(value: Any) -> str:
    text = _canonical_key(value)
    aliases = {
        "SEVERE": "CRITICAL",
        "FATAL": "CRITICAL",
        "WARNING": "MEDIUM",
        "WARN": "MEDIUM",
        "MODERATE": "MEDIUM",
        "INFORMATIONAL": "INFO",
        "NONE": "INFO",
        "UNSPECIFIED": "UNKNOWN",
        "UNKNOWN": "UNKNOWN",
    }
    result = aliases.get(text, text)
    return result if result in _SEVERITY_ORDER and result != "" else "UNKNOWN"


def _normalize_confidence(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, bool):
        return "UNKNOWN"
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1:
            number /= 100
        if number >= 0.8:
            return "HIGH"
        if number >= 0.5:
            return "MEDIUM"
        return "LOW"
    text = _canonical_key(value)
    if text in {"HIGH", "MEDIUM", "LOW"}:
        return text
    try:
        return _normalize_confidence(float(str(value).strip()))
    except (TypeError, ValueError):
        return "UNKNOWN"


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def _scanner_name(value: Any) -> str:
    key = _canonical_key(value)
    aliases = {
        "CISCO": "CISCO_AI_SKILL_SCANNER",
        "CISCO_AI_DEFENSE": "CISCO_AI_SKILL_SCANNER",
        "CISCO_AI_SKILL_SCANNER": "CISCO_AI_SKILL_SCANNER",
        "SKILLSPECTOR": "NVIDIA_SKILLSPECTOR",
        "NVIDIA": "NVIDIA_SKILLSPECTOR",
        "NVIDIA_SKILLSPECTOR": "NVIDIA_SKILLSPECTOR",
        "AI": "AI_REVIEW",
        "AI_REVIEW": "AI_REVIEW",
    }
    return aliases.get(key, _text(value) or "UNKNOWN_SOURCE")


def _location(
    path: Any,
    start: Any,
    end: Any,
    column: Any = None,
) -> dict[str, Any] | None:
    normalized_path = normalize_path(path)
    start_line = _integer(start)
    end_line = _integer(end) or start_line
    normalized_column = _integer(column)
    if not normalized_path and start_line is None and end_line is None and normalized_column is None:
        return None
    if start_line is not None and end_line is not None and end_line < start_line:
        start_line, end_line = end_line, start_line
    return {
        "path": normalized_path,
        "start_line": start_line,
        "end_line": end_line,
        "column": normalized_column,
    }


def _scanner_locations(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    path = _first(raw, "path", "file", "file_path", "filename", "file_name")
    start = _first(raw, "line", "line_number", "start_line", "line_start")
    end = _first(raw, "end_line", "line_end")
    column = _first(raw, "column", "column_number", "start_column")
    result = _location(path, start, end, column)
    return [result] if result else []


def _ai_locations(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = raw.get("locations")
    if not isinstance(values, list):
        values = []
    locations: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        result = _location(
            _first(value, "path", "file", "file_path"),
            _first(value, "line_start", "start_line", "line"),
            _first(value, "line_end", "end_line", "line"),
            _first(value, "column", "column_start"),
        )
        if result:
            locations.append(result)
    if not locations:
        result = _location(
            _first(raw, "path", "file", "file_path"),
            _first(raw, "line_start", "start_line", "line"),
            _first(raw, "line_end", "end_line", "line"),
            _first(raw, "column", "column_start"),
        )
        if result:
            locations.append(result)
    return locations


def _location_sort_key(location: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(location.get("path") or ""),
        location.get("start_line") or 0,
        location.get("end_line") or 0,
        location.get("column") or 0,
    )


def _fingerprint(
    category: str,
    locations: Sequence[Mapping[str, Any]],
    evidence: str,
) -> str:
    canonical = {
        "category": category,
        "locations": [
            {
                "path": location.get("path") or "",
                "start_line": location.get("start_line"),
                "end_line": location.get("end_line"),
                "column": location.get("column"),
            }
            for location in sorted(locations, key=_location_sort_key)
        ],
        "evidence_summary": _redact_evidence(evidence),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_reference(
    *,
    source: str,
    finding_id: Any,
    rule_id: Any,
    raw_severity: Any,
    raw_category: Any,
    domain: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "finding_id": _text(finding_id),
        "source_rule_id": _text(rule_id),
        "raw_severity": _text(raw_severity),
        "raw_category": _text(raw_category),
        "domain": domain,
    }


def _dedupe_references(references: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in references:
        item = dict(reference)
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _normalized_finding(
    *,
    source: str,
    raw: Mapping[str, Any],
    skill_digest: str | None,
    domain: str,
    locations: Sequence[Mapping[str, Any]],
    finding_id: Any,
    rule_id: Any,
    raw_severity: Any,
    raw_category: Any,
    title: Any,
    description: Any,
    evidence: Any,
    recommendation: Any,
    confidence: Any,
    incomplete: bool = False,
) -> dict[str, Any]:
    category = normalize_category(raw_category)
    normalized_locations = [dict(item) for item in sorted(locations, key=_location_sort_key)]
    first_location = normalized_locations[0] if normalized_locations else {}
    normalized_description = _text(description) or _text(title) or ""
    evidence_summary = normalize_evidence(evidence, fallback=normalized_description or title or category)
    status = "INCOMPLETE" if incomplete else "OPEN"
    fingerprint = _fingerprint(category, normalized_locations, evidence_summary)
    # Stable IDs come from the de-duplication identity, never from a scanner's
    # mutable array index.  The original IDs are retained in source_references.
    normalized_id = "finding-" + fingerprint[:20]
    return {
        "finding_id": normalized_id,
        "skill_digest": skill_digest,
        "source_scanner": source,
        "source_rule_id": _text(rule_id),
        "category": category,
        "severity": normalize_severity(raw_severity),
        "title": _text(title) or category,
        "description": normalized_description,
        "file_path": first_location.get("path") or None,
        "start_line": first_location.get("start_line"),
        "end_line": first_location.get("end_line"),
        "evidence_summary": evidence_summary,
        "recommendation": _text(recommendation) or "",
        "confidence": _normalize_confidence(confidence),
        "fingerprint": fingerprint,
        "status": status,
        "domain": domain,
        "locations": normalized_locations,
        "source_scanners": [source],
        "source_references": [
            _source_reference(
                source=source,
                finding_id=finding_id,
                rule_id=rule_id,
                raw_severity=raw_severity,
                raw_category=raw_category,
                domain=domain,
            )
        ],
    }


def normalize_scan_finding(
    finding: Mapping[str, Any],
    *,
    scanner: str,
    skill_digest: str | None = None,
) -> dict[str, Any]:
    """Normalize one scanner finding from ``ScanResult.to_dict()``."""

    source = _scanner_name(scanner)
    rule_id = _first(finding, "rule_id", "rule", "check_id", "check", "ruleId")
    raw_category = _first(finding, "category", "type", "finding_type", "kind")
    raw_severity = _first(finding, "severity", "risk_level", "risk", "level", "priority")
    title = _first(finding, "title", "name", "summary")
    description = _first(finding, "message", "description", "detail", "reason", "finding")
    evidence = _first(finding, "evidence", "snippet", "code", "match")
    recommendation = _first(finding, "remediation", "recommendation", "fix", "solution")
    confidence = _first(finding, "confidence", "score")
    locations = _scanner_locations(finding)
    missing = finding.get("missing_fields")
    incomplete = finding.get("complete") is False or bool(missing)
    return _normalized_finding(
        source=source,
        raw=finding,
        skill_digest=skill_digest,
        domain="SECURITY",
        locations=locations,
        finding_id=_first(finding, "finding_id", "id", "uuid"),
        rule_id=rule_id,
        raw_severity=raw_severity,
        raw_category=raw_category,
        title=title,
        description=description,
        evidence=evidence,
        recommendation=recommendation,
        confidence=confidence,
        incomplete=incomplete,
    )


def normalize_ai_finding(
    finding: Mapping[str, Any],
    *,
    domain: str,
    skill_digest: str | None = None,
) -> dict[str, Any]:
    """Normalize one finding from the AI review schema."""

    normalized_domain = "QUALITY" if _canonical_key(domain) == "QUALITY" else "SECURITY"
    source_id = _first(finding, "id", "finding_id")
    raw_category = _first(finding, "category", "type")
    raw_severity = _first(finding, "severity", "risk_level", "risk")
    title = _first(finding, "title", "name", "summary")
    description = _first(finding, "description", "message", "detail", "reason")
    evidence = _first(finding, "evidence", "snippet", "match")
    recommendation = _first(finding, "recommendation", "remediation", "fix", "solution")
    locations = _ai_locations(finding)
    references: list[dict[str, Any]] = []
    for reference in finding.get("source_references", ()):
        if not isinstance(reference, Mapping):
            continue
        source = _scanner_name(_first(reference, "source", "scanner"))
        references.append(
            _source_reference(
                source=source,
                finding_id=_first(reference, "finding_id", "id"),
                rule_id=_first(reference, "source_rule_id", "rule_id", "rule"),
                raw_severity=raw_severity,
                raw_category=raw_category,
                domain=normalized_domain,
            )
        )
    normalized = _normalized_finding(
        source="AI_REVIEW",
        raw=finding,
        skill_digest=skill_digest,
        domain=normalized_domain,
        locations=locations,
        finding_id=source_id,
        rule_id=None,
        raw_severity=raw_severity,
        raw_category=raw_category,
        title=title,
        description=description,
        evidence=evidence,
        recommendation=recommendation,
        confidence=_first(finding, "confidence"),
    )
    normalized["source_references"] = _dedupe_references(
        [
            *normalized["source_references"],
            *references,
        ]
    )
    normalized["source_scanners"] = sorted(
        {str(item.get("source")) for item in normalized["source_references"]}
    )
    return normalized


def _merge_finding(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    if _SEVERITY_ORDER[incoming["severity"]] > _SEVERITY_ORDER[target["severity"]]:
        target["severity"] = incoming["severity"]
    if target.get("status") != "INCOMPLETE" and incoming.get("status") == "INCOMPLETE":
        target["status"] = "INCOMPLETE"
    for field in ("title", "description", "recommendation", "evidence_summary"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]
    target["source_scanners"] = sorted(
        set(target.get("source_scanners", ())) | set(incoming.get("source_scanners", ()))
    )
    target["source_references"] = _dedupe_references(
        [*target.get("source_references", ()), *incoming.get("source_references", ())]
    )
    domains = set()
    for item in (target.get("domain"), incoming.get("domain")):
        if item == "MIXED":
            domains.update({"SECURITY", "QUALITY"})
        elif item:
            domains.add(str(item))
    if len(domains) > 1:
        target["domain"] = "MIXED"
    elif domains:
        target["domain"] = next(iter(domains))


def deduplicate_findings(findings: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Merge findings by the required category/path/position/evidence key.

    Every source reference is retained.  The merged severity is the maximum
    severity observed, so adding another scanner can only preserve or raise a
    risk level.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise ReviewPolicyError("normalized finding must be a mapping")
        fingerprint = _text(finding.get("fingerprint"))
        if not fingerprint:
            raise ReviewPolicyError("normalized finding fingerprint is required")
        current = grouped.get(fingerprint)
        if current is None:
            current = dict(finding)
            current["source_scanners"] = list(finding.get("source_scanners", ()))
            current["source_references"] = [
                dict(item) for item in finding.get("source_references", ())
            ]
            grouped[fingerprint] = current
        else:
            _merge_finding(current, finding)
    return tuple(grouped.values())


def _scan_items(scan_results: Any) -> list[Mapping[str, Any]]:
    if scan_results is None:
        return []
    if isinstance(scan_results, Mapping):
        # A single ScanResult is a mapping with a scanner field.  Also accept
        # the convenient {scanner: result} form used by small callers.
        if "scanner" in scan_results or "tool_ok" in scan_results:
            return [scan_results]
        values = list(scan_results.values())
    else:
        try:
            values = list(scan_results)
        except TypeError as exc:
            raise ReviewPolicyError("scan_results must be iterable") from exc
    result: list[Mapping[str, Any]] = []
    for value in values:
        converted = _mapping(value)
        if converted is not None:
            result.append(converted)
    return result


def normalize_findings(
    scan_results: Any = (),
    ai_review: Mapping[str, Any] | None = None,
    *,
    skill_digest: str | None = None,
    include_quality: bool = True,
) -> tuple[dict[str, Any], ...]:
    """Normalize and de-duplicate static and AI findings.

    ``include_quality`` defaults to true so the returned evidence contains
    every reported finding.  The gate itself filters security findings before
    making a security decision.
    """

    normalized: list[dict[str, Any]] = []
    for result in _scan_items(scan_results):
        scanner = _scanner_name(result.get("scanner"))
        result_digest = _text(_first(result, "skill_digest", "skill_digest_sha256"))
        digest = skill_digest or result_digest
        values = result.get("findings", ())
        if not isinstance(values, list):
            continue
        for finding in values:
            if isinstance(finding, Mapping):
                normalized.append(
                    normalize_scan_finding(finding, scanner=scanner, skill_digest=digest)
                )
    if ai_review is not None:
        review = _mapping(ai_review)
        if review is not None:
            subject = review.get("subject")
            subject_map = subject if isinstance(subject, Mapping) else {}
            digest = skill_digest or _text(
                _first(subject_map, "skill_digest_sha256", "skill_digest")
            )
            security = review.get("security_review")
            if isinstance(security, Mapping):
                for finding in security.get("findings", ()):
                    if isinstance(finding, Mapping):
                        normalized.append(
                            normalize_ai_finding(
                                finding, domain="SECURITY", skill_digest=digest
                            )
                        )
            quality = review.get("quality_review")
            if include_quality and isinstance(quality, Mapping):
                for finding in quality.get("findings", ()):
                    if isinstance(finding, Mapping):
                        normalized.append(
                            normalize_ai_finding(
                                finding, domain="QUALITY", skill_digest=digest
                            )
                        )
    return deduplicate_findings(normalized)


def _security_domain(finding: Mapping[str, Any]) -> bool:
    return finding.get("domain") in {"SECURITY", "MIXED"}


def _finding_decision_inputs(
    findings: Iterable[Mapping[str, Any]],
) -> tuple[bool, bool, bool, bool]:
    blocking = review = incomplete = False
    has_medium = False
    for finding in findings:
        severity = normalize_severity(finding.get("severity"))
        if finding.get("status") == "INCOMPLETE":
            incomplete = True
        if severity == "CRITICAL":
            blocking = True
        elif severity == "HIGH":
            review = True
        elif severity == "MEDIUM":
            has_medium = True
        elif severity == "UNKNOWN":
            review = True
    return blocking, review, incomplete, has_medium


def _decision(value: Any) -> str:
    key = _canonical_key(value)
    if key in {"BLOCK", "BLOCKED", "DO_NOT_INSTALL", "FAIL", "FAILED"}:
        return SECURITY_BLOCK
    if key in {"REVIEW", "REVIEW_REQUIRED", "MANUAL_REVIEW"}:
        return SECURITY_REVIEW_REQUIRED
    if key in {"INCOMPLETE", "ERROR", "TIMEOUT", "MISSING", "INVALID", "UNKNOWN"}:
        return SECURITY_INCOMPLETE
    if key in {"PASS", "PASSED", "CLEAN", "OK", "COMPLETED"}:
        return SECURITY_PASS
    return ""


def _scan_is_complete(result: Mapping[str, Any], expected_digest: str | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if result.get("tool_ok") is not True:
        reasons.append(f"{_scanner_name(result.get('scanner'))}: tool_ok is not true")
    if result.get("status") != "COMPLETED":
        reasons.append(f"{_scanner_name(result.get('scanner'))}: status is not COMPLETED")
    if result.get("report_complete") is not True:
        reasons.append(f"{_scanner_name(result.get('scanner'))}: report is incomplete")
    if "completed" in result and result.get("completed") is not True:
        reasons.append(f"{_scanner_name(result.get('scanner'))}: completed is not true")
    if result.get("errors"):
        reasons.append(f"{_scanner_name(result.get('scanner'))}: execution errors present")
    actual_digest = _text(_first(result, "skill_digest", "skill_digest_sha256"))
    if not expected_digest:
        reasons.append("expected skill digest is missing")
    elif not actual_digest:
        reasons.append(f"{_scanner_name(result.get('scanner'))}: scan digest is missing")
    elif actual_digest.lower() != expected_digest.lower():
        reasons.append(f"{_scanner_name(result.get('scanner'))}: scan digest differs")
    comparison = result.get("directory_comparison")
    if isinstance(comparison, Mapping) and comparison.get("unchanged") is not True:
        reasons.append(f"{_scanner_name(result.get('scanner'))}: input changed during scan")
    return not reasons, reasons


def _ai_completeness(review: Mapping[str, Any], expected_digest: str | None) -> list[str]:
    reasons: list[str] = []
    if not expected_digest:
        reasons.append("expected skill digest is missing")
    subject = review.get("subject")
    subject_map = subject if isinstance(subject, Mapping) else {}
    actual_digest = _text(_first(subject_map, "skill_digest_sha256", "skill_digest"))
    if not actual_digest:
        reasons.append("AI review digest is missing")
    elif expected_digest and actual_digest.lower() != expected_digest.lower():
        reasons.append("AI review digest differs")
    coverage = review.get("input_coverage")
    if not isinstance(coverage, Mapping):
        reasons.append("AI input coverage is missing")
        return reasons
    if coverage.get("package_complete") is not True:
        reasons.append("AI package coverage is incomplete")
    expected_files = coverage.get("files_expected")
    reviewed_files = coverage.get("files_reviewed")
    if not isinstance(expected_files, int) or not isinstance(reviewed_files, int):
        reasons.append("AI file coverage counts are missing")
    elif expected_files != reviewed_files:
        reasons.append("AI file coverage counts differ")
    if coverage.get("unreadable_or_skipped_files"):
        reasons.append("AI review skipped or unreadable files")
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, Mapping) or not _text(reviewer.get("model")):
        reasons.append("AI reviewer model is missing")
    for field in ("review_id", "policy_version", "reviewed_at"):
        if not _text(review.get(field)):
            reasons.append(f"AI {field} is missing")
    if not isinstance(review.get("security_review"), Mapping):
        reasons.append("AI security review is missing")
    if not isinstance(review.get("quality_review"), Mapping):
        reasons.append("AI quality review is missing")
    return reasons


def _quality_result(
    review: Mapping[str, Any] | None,
    *,
    threshold: int,
    incomplete_reasons: Sequence[str],
) -> tuple[str, int | None, bool, list[str]]:
    if not isinstance(review, Mapping):
        return QUALITY_INCOMPLETE, None, False, ["AI quality review is missing"]
    verdict = _canonical_key(review.get("verdict"))
    score = review.get("score")
    reasons: list[str] = []
    if verdict == "INCOMPLETE" or score is None:
        return QUALITY_INCOMPLETE, None, False, ["AI quality score is incomplete"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        return QUALITY_INCOMPLETE, None, False, ["AI quality score is invalid"]
    if verdict not in {"PASS", "FAIL"}:
        return QUALITY_INCOMPLETE, score, False, ["AI quality verdict is invalid"]
    if verdict == "PASS" and score < threshold:
        reasons.append("AI quality verdict PASS is below the quality threshold")
    if verdict == "FAIL" and score >= threshold:
        reasons.append("AI quality verdict FAIL contradicts the quality threshold")
    if reasons:
        return QUALITY_INCOMPLETE, score, False, reasons
    return verdict, score, verdict == "PASS" and score >= threshold, []


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Immutable output of :func:`evaluate_policy`."""

    security_decision: str
    quality_decision: str
    quality_score: int | None
    quality_threshold: int
    quality_eligible: bool
    candidate_eligible: bool
    findings: tuple[dict[str, Any], ...]
    security_findings: tuple[dict[str, Any], ...]
    quality_findings: tuple[dict[str, Any], ...]
    reasons: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    incomplete_reasons: tuple[str, ...] = ()

    @property
    def security_conclusion(self) -> str:
        """Naming alias used by result writers."""

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
            "findings": [dict(item) for item in self.findings],
            "security_findings": [dict(item) for item in self.security_findings],
            "quality_findings": [dict(item) for item in self.quality_findings],
            "reasons": list(self.reasons),
            "blocking_reasons": list(self.blocking_reasons),
            "review_reasons": list(self.review_reasons),
            "incomplete_reasons": list(self.incomplete_reasons),
        }


def evaluate_policy(
    scan_results: Any = (),
    ai_review: Mapping[str, Any] | None = None,
    *,
    skill_digest: str | None = None,
    source_selection_status: str | None = None,
    stale_inventory: bool = False,
    branch_content_conflict: bool = False,
    coverage_requires_review: bool = False,
    quality_threshold: int = DEFAULT_QUALITY_THRESHOLD,
    medium_requires_review: bool = True,
) -> PolicyResult:
    """Apply the security gate and independent AI quality score.

    A blocker takes precedence over incomplete/review-needed state because a
    confirmed critical issue must remain a block even when another required
    stage is also incomplete.  In every non-PASS case the candidate flag is
    false.
    """

    if isinstance(quality_threshold, bool) or not isinstance(quality_threshold, int):
        raise ReviewPolicyError("quality_threshold must be an integer")
    if not 0 <= quality_threshold <= 100:
        raise ReviewPolicyError("quality_threshold must be between 0 and 100")
    expected_digest = _text(skill_digest)
    results = _scan_items(scan_results)
    findings = normalize_findings(results, ai_review, skill_digest=expected_digest)
    security_findings = tuple(item for item in findings if _security_domain(item))
    quality_findings = tuple(item for item in findings if item.get("domain") == "QUALITY")

    blocking_reasons: list[str] = []
    review_reasons: list[str] = []
    incomplete_reasons: list[str] = []

    by_scanner: dict[str, Mapping[str, Any]] = {}
    for result in results:
        scanner = _scanner_name(result.get("scanner"))
        if scanner in by_scanner:
            incomplete_reasons.append(f"duplicate scan result for {scanner}")
        else:
            by_scanner[scanner] = result
    for scanner in sorted(_SCANNERS):
        result = by_scanner.get(scanner)
        if result is None:
            incomplete_reasons.append(f"missing scan result for {scanner}")
            continue
        complete, reasons = _scan_is_complete(result, expected_digest)
        if not complete:
            incomplete_reasons.extend(reasons)
        decision = _decision(result.get("decision"))
        if decision == SECURITY_BLOCK:
            blocking_reasons.append(f"{scanner} reported a blocking decision")
        elif decision == SECURITY_REVIEW_REQUIRED:
            review_reasons.append(f"{scanner} requires review")
        elif decision == SECURITY_INCOMPLETE:
            incomplete_reasons.append(f"{scanner} reported an incomplete decision")

    ai_incomplete = _ai_completeness(ai_review or {}, expected_digest)
    incomplete_reasons.extend(ai_incomplete)
    ai_security = ai_review.get("security_review") if isinstance(ai_review, Mapping) else None
    ai_security_verdict = _decision(ai_security.get("verdict")) if isinstance(ai_security, Mapping) else ""
    if ai_security_verdict == SECURITY_BLOCK:
        blocking_reasons.append("AI security review verdict is BLOCK")
    elif ai_security_verdict == SECURITY_REVIEW_REQUIRED:
        review_reasons.append("AI security review requires manual review")
    elif ai_security_verdict == SECURITY_INCOMPLETE:
        incomplete_reasons.append("AI security review is incomplete")

    finding_blocking, finding_review, finding_incomplete, has_medium = _finding_decision_inputs(
        security_findings
    )
    if finding_blocking:
        blocking_reasons.append("a security finding has CRITICAL severity")
    if finding_review:
        review_reasons.append("a security finding has HIGH or UNKNOWN severity")
    if medium_requires_review and has_medium:
        review_reasons.append("a security finding has MEDIUM severity")
    if finding_incomplete:
        incomplete_reasons.append("a normalized security finding is incomplete")

    selection = _canonical_key(source_selection_status)
    if stale_inventory or selection == "STALE_INVENTORY":
        incomplete_reasons.append("source inventory is stale and unconfirmed")
    if branch_content_conflict or selection in {"CONFLICT", "BRANCH_CONTENT_CONFLICT"}:
        review_reasons.append("branch content conflict requires manual review")
    if coverage_requires_review:
        review_reasons.append("snapshot contains special content that requires manual review")
    if selection in {"INPUT_INVALID", "INVALID", "SOURCE_UNAVAILABLE"}:
        incomplete_reasons.append(f"source selection status is {source_selection_status}")

    quality_review = ai_review.get("quality_review") if isinstance(ai_review, Mapping) else None
    quality_decision, quality_score, quality_eligible, quality_reasons = _quality_result(
        quality_review,
        threshold=quality_threshold,
        incomplete_reasons=incomplete_reasons,
    )
    if quality_decision == QUALITY_INCOMPLETE:
        incomplete_reasons.extend(quality_reasons)
    elif quality_reasons:
        # Defensive path for future quality policies: a quality-only reason
        # must not be turned into a security downgrade.
        incomplete_reasons.extend(quality_reasons)

    if blocking_reasons:
        security_decision = SECURITY_BLOCK
    elif incomplete_reasons:
        security_decision = SECURITY_INCOMPLETE
    elif review_reasons:
        security_decision = SECURITY_REVIEW_REQUIRED
    else:
        security_decision = SECURITY_PASS

    candidate_eligible = (
        security_decision == SECURITY_PASS
        and quality_eligible
        and not incomplete_reasons
        and not review_reasons
        and not blocking_reasons
    )
    reasons = tuple(
        [*blocking_reasons, *incomplete_reasons, *review_reasons]
        + ([] if quality_eligible else ["quality score is below the candidate threshold"])
    )
    return PolicyResult(
        security_decision=security_decision,
        quality_decision=quality_decision,
        quality_score=quality_score,
        quality_threshold=quality_threshold,
        quality_eligible=quality_eligible,
        candidate_eligible=candidate_eligible,
        findings=findings,
        security_findings=security_findings,
        quality_findings=quality_findings,
        reasons=reasons,
        blocking_reasons=tuple(blocking_reasons),
        review_reasons=tuple(review_reasons),
        incomplete_reasons=tuple(incomplete_reasons),
    )


def evaluate_review(*args: Any, **kwargs: Any) -> PolicyResult:
    """Compatibility alias for callers naming the operation ``review``."""

    return evaluate_policy(*args, **kwargs)


aggregate_findings = normalize_findings


__all__ = [
    "DEFAULT_QUALITY_THRESHOLD",
    "PolicyResult",
    "ReviewPolicyError",
    "SECURITY_BLOCK",
    "SECURITY_INCOMPLETE",
    "SECURITY_PASS",
    "SECURITY_REVIEW_REQUIRED",
    "aggregate_findings",
    "deduplicate_findings",
    "evaluate_policy",
    "evaluate_review",
    "normalize_ai_finding",
    "normalize_category",
    "normalize_evidence",
    "normalize_findings",
    "normalize_path",
    "normalize_scan_finding",
    "normalize_severity",
]
