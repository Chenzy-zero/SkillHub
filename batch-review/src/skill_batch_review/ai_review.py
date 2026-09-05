"""Validate the JSON returned by the read-only AI review Skill.

This module does not invoke a model. The operator runs Codex CLI or Claude Code
in the approved intranet environment and places its JSON result in the evidence
workspace; this module validates that result before it can influence a gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from jsonschema import Draft202012Validator, FormatChecker


class AIReviewValidationError(ValueError):
    """Raised when an AI review result is malformed or internally inconsistent."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("AI review result is invalid: " + "; ".join(errors))


@dataclass(frozen=True)
class AIReviewExpectation:
    """Values frozen by the orchestrator before the manual AI review."""

    skill_digest_sha256: str
    source_revision: str
    review_id: Optional[str] = None
    policy_version: Optional[str] = None


@dataclass(frozen=True)
class AIReviewSourceMetadata:
    skill_name: str
    repo_name: str
    branch: str
    skill_path: str
    inventory_revision: str


def _scanner_handoff_status(scan: Any) -> str:
    if getattr(scan, "status", None) == "TIMEOUT":
        return "TIMEOUT"
    if getattr(scan, "status", None) == "ERROR":
        return "FAILED"
    if (
        getattr(scan, "status", None) == "COMPLETED"
        and bool(getattr(scan, "completed", False))
        and bool(getattr(scan, "tool_ok", False))
    ):
        return "COMPLETED"
    return "INVALID"


def build_ai_review_handoff(
    *,
    snapshot: Any,
    scans: Sequence[Any],
    source: AIReviewSourceMetadata,
    review_id: str,
    policy_version: str,
    assigned_reviewed_at: str,
    reviewer_model: str,
    result_schema_path: Path,
    require_complete_inputs: bool = True,
) -> dict[str, Any]:
    """Build the exact read-only context handed to an approved AI reviewer.

    No process is launched and no file is written.  The caller may save this
    dictionary in the restricted evidence area. The model receives package
    content plus compact frozen metadata; scanner reports remain program-only
    evidence and are merged after the AI result is validated.
    """

    if len(scans) != 2:
        raise AIReviewValidationError(["exactly two static scan results are required"])
    by_name = {getattr(scan, "scanner", None): scan for scan in scans}
    if set(by_name) != {"cisco", "skillspector"}:
        raise AIReviewValidationError(["Cisco and NVIDIA SkillSpector results are required"])
    if not getattr(snapshot, "skill_digest", None):
        raise AIReviewValidationError(["snapshot is missing skill_digest"])
    if not getattr(snapshot, "source_revision", None):
        raise AIReviewValidationError(["snapshot is missing source_revision"])
    errors: list[str] = []
    for scanner in ("cisco", "skillspector"):
        scan = by_name[scanner]
        handoff_status = _scanner_handoff_status(scan)
        if getattr(scan, "skill_digest", None) != snapshot.skill_digest:
            errors.append(f"{scanner} digest does not match the snapshot")
        if require_complete_inputs and handoff_status != "COMPLETED":
            errors.append(f"{scanner} scan is not complete and successful")
    if require_complete_inputs and not bool(getattr(snapshot, "coverage_complete", False)):
        errors.append("snapshot coverage is incomplete")
    if errors:
        raise AIReviewValidationError(errors)
    files_expected = getattr(snapshot, "file_count", None)
    if not isinstance(files_expected, int):
        files_expected = len(getattr(snapshot, "entries", ()))
    return {
        "schema_version": "1.0",
        "review_id": review_id,
        "policy_version": policy_version,
        "assigned_reviewed_at": assigned_reviewed_at,
        "reviewer_model": reviewer_model,
        "skill_root": str(Path(snapshot.snapshot_path).resolve()),
        "result_schema_path": str(result_schema_path.resolve()),
        "package_summary": {
            "coverage_complete": bool(getattr(snapshot, "coverage_complete", False)),
            "files_expected": files_expected,
            "coverage_issues": [
                issue.to_dict() for issue in getattr(snapshot, "coverage_issues", ())
            ],
        },
        "subject": {
            "skill_name": source.skill_name,
            "repo_name": source.repo_name,
            "branch": source.branch,
            "skill_path": source.skill_path,
            "inventory_revision": source.inventory_revision,
            "source_revision": snapshot.source_revision,
            "skill_digest_sha256": snapshot.skill_digest,
        },
        "execution_boundary": {
            "allowed_tools": ["Read", "Glob", "Grep"],
            "network_allowed": False,
            "execute_target_content": False,
            "skill_name": "skill-security-review",
            "skill_invocations": {
                "claude_code": "/skill-security-review",
                "codex_cli": "$skill-security-review",
            },
            "invoke_skill": "/skill-security-review",
        },
    }


_SEVERITY_ORDER = {
    "NONE": 0,
    "INFO": 1,
    "LOW": 2,
    "MEDIUM": 3,
    "HIGH": 4,
    "CRITICAL": 5,
}


def _json_path(parts: tuple[Any, ...]) -> str:
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts
    )


def _expected_disposition(payload: Mapping[str, Any]) -> str:
    security = payload["security_review"]["verdict"]
    quality = payload["quality_review"]["verdict"]
    if security == "BLOCK" or quality == "FAIL":
        return "REJECT"
    if security == "INCOMPLETE" or quality == "INCOMPLETE":
        return "INCOMPLETE"
    if security == "REVIEW_REQUIRED":
        return "MANUAL_REVIEW"
    return "APPROVE_CANDIDATE"


def _semantic_errors(
    payload: Mapping[str, Any], expectation: Optional[AIReviewExpectation]
) -> list[str]:
    errors: list[str] = []
    subject = payload["subject"]
    coverage = payload["input_coverage"]
    security = payload["security_review"]
    quality = payload["quality_review"]
    overall = payload["overall"]

    if expectation is not None:
        actual_digest = subject["skill_digest_sha256"]
        if actual_digest is None or actual_digest.lower() != expectation.skill_digest_sha256.lower():
            errors.append("$.subject.skill_digest_sha256 does not match the frozen package")
        actual_revision = subject["source_revision"]
        if actual_revision is None or actual_revision.lower() != expectation.source_revision.lower():
            errors.append("$.subject.source_revision does not match the frozen revision")
        if expectation.review_id is not None and payload["review_id"] != expectation.review_id:
            errors.append("$.review_id does not match the assigned review id")
        if expectation.policy_version is not None and payload["policy_version"] != expectation.policy_version:
            errors.append("$.policy_version does not match the frozen policy version")

    expected_files = coverage["files_expected"]
    reviewed_files = coverage["files_reviewed"]
    if expected_files is not None and reviewed_files > expected_files:
        errors.append("$.input_coverage.files_reviewed exceeds files_expected")

    package_digest = subject["skill_digest_sha256"]

    incomplete = any(
        (
            payload["review_id"] is None,
            payload["policy_version"] is None,
            payload["reviewed_at"] is None,
            payload["reviewer"]["model"] is None,
            subject["source_revision"] is None,
            package_digest is None,
            not coverage["package_complete"],
            expected_files is None,
            expected_files is not None and reviewed_files != expected_files,
            bool(coverage["unreadable_or_skipped_files"]),
        )
    )
    if incomplete and security["verdict"] not in {"BLOCK", "INCOMPLETE"}:
        errors.append(
            "$.security_review.verdict must be INCOMPLETE unless a blocking finding is confirmed"
        )

    findings = security["findings"]
    actual_max = max(
        (finding["severity"] for finding in findings),
        key=lambda item: _SEVERITY_ORDER[item],
        default="NONE",
    )
    if security["max_severity"] != actual_max:
        errors.append("$.security_review.max_severity does not match security findings")

    finding_ids = [
        finding["id"]
        for section in (security, quality)
        for finding in section["findings"]
    ]
    if len(finding_ids) != len(set(finding_ids)):
        errors.append("finding ids must be unique across security and quality findings")

    dimension_scores = [dimension["score"] for dimension in quality["dimensions"]]
    for index, dimension in enumerate(quality["dimensions"]):
        score = dimension["score"]
        if score is not None and score > dimension["max_score"]:
            errors.append(f"$.quality_review.dimensions[{index}].score exceeds max_score")
    if quality["score"] is not None:
        if any(score is None for score in dimension_scores):
            errors.append("quality dimensions cannot contain null scores when total score is present")
        elif sum(dimension_scores) != quality["score"]:
            errors.append("$.quality_review.score does not equal the dimension score sum")

    expected_disposition = _expected_disposition(payload)
    if overall["disposition"] != expected_disposition:
        errors.append(
            f"$.overall.disposition must be {expected_disposition} for the review verdicts"
        )
    expected_eligible = expected_disposition == "APPROVE_CANDIDATE"
    if overall["private_candidate_eligible"] is not expected_eligible:
        errors.append("$.overall.private_candidate_eligible contradicts disposition")
    return errors


def validate_ai_review_result(
    payload: Mapping[str, Any],
    *,
    schema_path: Path,
    expectation: Optional[AIReviewExpectation] = None,
) -> None:
    """Validate JSON Schema plus cross-field safety invariants."""

    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [
        f"{_json_path(tuple(error.absolute_path))}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    ]
    if not errors:
        errors.extend(_semantic_errors(payload, expectation))
    if errors:
        raise AIReviewValidationError(errors)


def load_and_validate_ai_review_result(
    result_path: Path,
    *,
    schema_path: Path,
    expectation: Optional[AIReviewExpectation] = None,
) -> dict[str, Any]:
    """Load a JSON object from disk and validate it without modifying it."""

    try:
        with result_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIReviewValidationError([f"cannot read JSON result: {exc}"]) from exc
    if not isinstance(payload, dict):
        raise AIReviewValidationError(["$ must be a JSON object"])
    validate_ai_review_result(payload, schema_path=schema_path, expectation=expectation)
    return payload


__all__ = [
    "AIReviewExpectation",
    "AIReviewSourceMetadata",
    "AIReviewValidationError",
    "build_ai_review_handoff",
    "load_and_validate_ai_review_result",
    "validate_ai_review_result",
]
