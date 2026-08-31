"""Deterministic, redacted reports for a completed (or partial) batch.

The report writer consumes result mappings from the individual pipeline
stages.  It intentionally accepts mappings instead of coupling the reporting
layer to one runner implementation: a result may be read from the local
state store, a normalized result index, or a test fixture.

Only derived summaries are written here.  The source CSV is never opened for
writing and raw scanner/AI evidence must remain in the restricted evidence
area; report fields contain references and short, redacted explanations.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SECURITY_DECISIONS = ("PASS", "REVIEW_REQUIRED", "BLOCKED", "INCOMPLETE")
QUALITY_LEVELS = ("EXCELLENT", "GOOD", "QUALIFIED", "UNQUALIFIED", "UNSCORED")
_CANDIDATE_STATUSES = frozenset(
    {"READY_TO_EXPORT", "EXPORTED_LOCAL", "VERIFIED", "MANUAL_SYNC_PENDING", "MANUALLY_SYNCED"}
)
_FAILURE_STATUSES = frozenset(
    {
        "FAILED",
        "ERROR",
        "TIMEOUT",
        "INVALID",
        "MISSING",
        "INCOMPLETE",
        "SOURCE_UNAVAILABLE",
        "INPUT_INVALID",
        "STALE_INVENTORY",
        "BRANCH_CONTENT_CONFLICT",
        "CONFLICT",
    }
)
_RETRY_STATUSES = frozenset({"PENDING", "RETRY", "RETRY_PENDING", "WAITING_FOR_RETRY"})
_TOOL_NAMES = ("cisco", "skillspector")

_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_\-.])(password|passwd|secret|token|api[_\-.]?key|access[_\-.]?key|private[_\-.]?key|credential|authorization)(?:$|[_\-.])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:-----BEGIN [^-]{1,80} PRIVATE KEY-----.*?-----END [^-]{1,80} PRIVATE KEY-----)"
    r"|authorization\s*[:=]\s*(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|(?:token|password|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{12,}\b"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]{10,}\.[A-Za-z0-9._-]{10,}\b"
)


class ReportingError(ValueError):
    """Raised when a report cannot be generated safely."""


@dataclass(frozen=True, slots=True)
class BatchReportPaths:
    """The four independent report files produced for one batch."""

    summary: Path
    details: Path
    failures: Path
    candidates: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "batch_summary": str(self.summary),
            "details": str(self.details),
            "failures": str(self.failures),
            "candidates": str(self.candidates),
        }


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret-like fields and values.

    This is deliberately conservative for report material.  A key named
    ``token``, ``password`` or ``secret`` is always replaced, and common
    token/key formats are removed from free text.  The original evidence is
    not modified; callers should store only a reference to that evidence in a
    report.
    """

    if key and _SENSITIVE_KEY_RE.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE_RE.sub("[REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    if path.exists() and path.is_dir():
        raise ReportingError(f"report path is a directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ReportingError(f"cannot write report {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    text = json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _path_get(value: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _first(value: Mapping[str, Any], *paths: Sequence[str], default: Any = None) -> Any:
    for path in paths:
        found = _path_get(value, *path, default=None)
        if found is not None:
            return found
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _upper(value: Any, default: str = "") -> str:
    return _text(value, default=default).upper()


def _record_id(record: Mapping[str, Any]) -> str:
    value = _first(
        record,
        ("source_row_id",),
        ("review_id",),
        ("task_key",),
        ("id",),
        ("subject", "source_row_id"),
        default="",
    )
    if value:
        return _text(value)
    # A deterministic fallback keeps two otherwise-identical records ordered
    # without inventing a random identifier.
    return hashlib.sha256(_json_bytes(record)).hexdigest()


def _repo(record: Mapping[str, Any]) -> str:
    return _text(_first(record, ("repo_name",), ("repository",), ("subject", "repo_name")))


def _skill_name(record: Mapping[str, Any]) -> str:
    return _text(_first(record, ("skill_name",), ("subject", "skill_name")))


def _branch(record: Mapping[str, Any]) -> str:
    return _text(_first(record, ("source_branch",), ("branch",), ("subject", "branch")))


def _skill_path(record: Mapping[str, Any]) -> str:
    return _text(
        _first(record, ("normalized_skill_path",), ("skill_path",), ("subject", "skill_path"))
    )


def _inventory_revision(record: Mapping[str, Any]) -> str:
    return _text(
        _first(record, ("inventory_revision",), ("lasted_commited",), ("subject", "inventory_revision"))
    )


def _source_revision(record: Mapping[str, Any]) -> str:
    return _text(
        _first(
            record,
            ("reviewed_source_revision",),
            ("source_revision",),
            ("subject", "source_revision"),
        )
    )


def _digest(record: Mapping[str, Any]) -> str:
    return _text(
        _first(
            record,
            ("reviewed_skill_digest",),
            ("skill_digest",),
            ("skill_digest_sha256",),
            ("subject", "skill_digest_sha256"),
        )
    )


def _last_change_revision(record: Mapping[str, Any]) -> str:
    return _text(
        _first(
            record,
            ("skill_last_change_revision",),
            ("last_change_revision",),
            ("resolution", "skill_last_change_revision"),
        )
    )


def _source_selection_status(record: Mapping[str, Any]) -> str:
    return _upper(
        _first(
            record,
            ("source_selection_status",),
            ("selection_status",),
            ("resolution", "status"),
            default="",
        )
    )


def _tool_report(record: Mapping[str, Any], tool: str) -> Mapping[str, Any]:
    aliases = {
        "cisco": ("cisco", "CISCO_AI_SKILL_SCANNER", "cisco_ai_skill_scanner"),
        "skillspector": ("skillspector", "NVIDIA_SKILLSPECTOR", "skill_spector"),
    }
    scanners = _mapping(record.get("scanners")) or _mapping(record.get("static_reports"))
    if scanners:
        for alias in aliases[tool]:
            candidate = scanners.get(alias)
            if isinstance(candidate, Mapping):
                return candidate
        if isinstance(scanners, Mapping) and isinstance(scanners.get(tool), Mapping):
            return scanners[tool]  # type: ignore[return-value]
        # ``static_reports`` is often a list, handled below.
    reports = record.get("static_reports")
    if isinstance(reports, Sequence) and not isinstance(reports, (str, bytes, bytearray)):
        expected = {
            "cisco": "CISCO_AI_SKILL_SCANNER",
            "skillspector": "NVIDIA_SKILLSPECTOR",
        }[tool]
        for report in reports:
            if isinstance(report, Mapping):
                name = _upper(report.get("scanner"))
                if name == expected or tool in name.lower():
                    return report
    for key in aliases[tool]:
        candidate = record.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _tool_status(record: Mapping[str, Any], tool: str) -> str:
    report = _tool_report(record, tool)
    return _upper(
        _first(report, ("status",), ("execution_status",), ("scan_status",), default=_first(record, (f"{tool}_status",), default=""))
    )


def _tool_severity(record: Mapping[str, Any], tool: str) -> str:
    report = _tool_report(record, tool)
    return _upper(
        _first(report, ("max_severity",), ("highest_severity",), ("severity",), default=_first(record, (f"{tool}_max_severity",), default=""))
    )


def _ai_review(record: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("ai_review", "ai", "ai_result"):
        candidate = record.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _ai_status(record: Mapping[str, Any]) -> str:
    ai = _ai_review(record)
    status = _first(ai, ("status",), ("review_status",), default=_first(record, ("ai_status",), default=""))
    if status:
        return _upper(status)
    if ai:
        if _path_get(ai, "overall", "disposition"):
            return "COMPLETED"
        if _path_get(ai, "security_review", "verdict") or _path_get(ai, "quality_review", "verdict"):
            return "COMPLETED"
    return ""


def _ai_severity(record: Mapping[str, Any]) -> str:
    ai = _ai_review(record)
    return _upper(
        _first(
            ai,
            ("max_severity",),
            ("security_review", "max_severity"),
            default=_first(record, ("ai_max_severity",), default=""),
        )
    )


def _security_decision(record: Mapping[str, Any]) -> str:
    value = _first(
        record,
        ("security_decision",),
        ("security_status",),
        ("security", "decision"),
        ("security_review", "verdict"),
        ("ai_review", "security_review", "verdict"),
        default="",
    )
    value = _upper(value)
    return {"BLOCK": "BLOCKED", "REJECT": "BLOCKED"}.get(value, value)


def _quality_score(record: Mapping[str, Any]) -> int | None:
    value = _first(record, ("quality_score",), ("quality", "score"), ("quality_review", "score"), ("ai_review", "quality_review", "score"))
    if value is None or isinstance(value, bool):
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 100 else None


def quality_level(score: int | None) -> str:
    if score is None:
        return "UNSCORED"
    if score >= 90:
        return "EXCELLENT"
    if score >= 85:
        return "GOOD"
    if score >= 70:
        return "QUALIFIED"
    return "UNQUALIFIED"


def _quality_dimensions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = _first(
        record,
        ("quality_dimensions",),
        ("quality_review", "dimensions"),
        ("ai_review", "quality_review", "dimensions"),
        default=[],
    )
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return [dict(redact(item)) for item in values if isinstance(item, Mapping)]


def _candidate_status(record: Mapping[str, Any]) -> str:
    value = _first(
        record,
        ("candidate_status",),
        ("candidate", "status"),
        ("archive_status",),
        default="",
    )
    if value:
        return _upper(value)
    eligible = _first(
        record,
        ("private_candidate_eligible",),
        ("candidate", "eligible"),
        ("overall", "private_candidate_eligible"),
        ("ai_review", "overall", "private_candidate_eligible"),
    )
    if eligible is True:
        return "READY_TO_EXPORT"
    return "NOT_ELIGIBLE"


def _manual_reason(record: Mapping[str, Any]) -> str:
    return _text(
        _first(
            record,
            ("manual_reason",),
            ("review_reason",),
            ("manual", "reason"),
            ("manual_review", "reason"),
            ("decision", "reason"),
            default="",
        )
    )


def _evidence_ref(record: Mapping[str, Any]) -> str:
    value = _first(
        record,
        ("evidence_ref",),
        ("evidence_index",),
        ("evidence_path",),
        ("evidence", "ref"),
        ("artifacts", "evidence_ref"),
        default="",
    )
    return _text(value)


def _reviewed_at(record: Mapping[str, Any]) -> str:
    return _text(_first(record, ("reviewed_at",), ("ai_review", "reviewed_at"), default=""))


def _policy_version(record: Mapping[str, Any]) -> str:
    return _text(_first(record, ("review_policy_version",), ("policy_version",), ("ai_review", "policy_version"), default=""))


def _status_values(record: Mapping[str, Any]) -> list[str]:
    statuses: list[str] = []
    for key in ("status", "task_status", "snapshot_status", "static_status", "ai_status", "candidate_status"):
        value = _upper(record.get(key))
        if value:
            statuses.append(value)
    selection = _source_selection_status(record)
    if selection:
        statuses.append(selection)
    for tool in _TOOL_NAMES:
        status = _tool_status(record, tool)
        if status:
            statuses.append(status)
    ai = _ai_status(record)
    if ai:
        statuses.append(ai)
    return statuses


def _is_failure(record: Mapping[str, Any]) -> bool:
    if _first(record, ("failed",), ("failure",), default=False) is True:
        return True
    for key in ("failure_reason", "error", "error_message"):
        if _text(record.get(key)):
            return True
    return any(status in _FAILURE_STATUSES for status in _status_values(record))


def _needs_retry(record: Mapping[str, Any]) -> bool:
    if _first(record, ("retry_required",), ("needs_retry",), default=False) is True:
        return True
    statuses = set(_status_values(record))
    return bool(statuses & _RETRY_STATUSES) or (
        bool(statuses & {"FAILED", "ERROR", "TIMEOUT"})
        and _text(_first(record, ("retry_exhausted",), default="false")).lower() != "true"
    )


def _source_row_count(record: Mapping[str, Any]) -> int:
    value = _first(record, ("source_row_count",), default=None)
    if value is None:
        values = record.get("source_row_numbers")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            return max(1, len(values))
        return 1
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, number)


def _selected(record: Mapping[str, Any]) -> bool:
    status = _source_selection_status(record)
    if status:
        return status in {"SELECTED", "RECEIVED", "VALIDATING"}
    return not status in {"SKIPPED_SUPERSEDED_BRANCH", "CONFLICT", "BRANCH_CONTENT_CONFLICT", "STALE_INVENTORY", "INPUT_INVALID", "INVALID"}


def _candidate(record: Mapping[str, Any]) -> bool:
    status = _candidate_status(record)
    return status in _CANDIDATE_STATUSES


def _normalized_record(record: Mapping[str, Any], *, batch_id: str) -> dict[str, Any]:
    """Produce the stable, redacted columns shared by all report outputs."""

    score = _quality_score(record)
    selection = _source_selection_status(record)
    security = _security_decision(record)
    candidate = _candidate_status(record)
    return dict(redact({
        "source_row_id": _record_id(record),
        "source_row_count": _source_row_count(record),
        "skill_name": _skill_name(record),
        "repo_name": _repo(record),
        "source_branch": _branch(record),
        "normalized_skill_path": _skill_path(record),
        "inventory_revision": _inventory_revision(record),
        "source_revision": _source_revision(record),
        "skill_last_change_revision": _last_change_revision(record),
        "skill_digest": _digest(record),
        "cisco_status": _tool_status(record, "cisco"),
        "cisco_max_severity": _tool_severity(record, "cisco"),
        "skillspector_status": _tool_status(record, "skillspector"),
        "skillspector_max_severity": _tool_severity(record, "skillspector"),
        "ai_status": _ai_status(record),
        "ai_max_severity": _ai_severity(record),
        "security_decision": security,
        "quality_score": score,
        "quality_level": quality_level(score),
        "quality_dimensions": _quality_dimensions(record),
        "manual_reason": _manual_reason(record),
        "candidate_status": candidate,
        "evidence_ref": _evidence_ref(record),
        "source_selection_status": selection,
        "review_policy_version": _policy_version(record),
        "reviewed_at": _reviewed_at(record),
        "failure_reason": _text(_first(record, ("failure_reason",), ("error_message",), ("error",), default="")),
        "retry_required": _needs_retry(record),
        "is_failure": _is_failure(record),
        "batch_id": batch_id,
    }))


DETAIL_FIELDS = (
    "batch_id",
    "source_row_id",
    "source_row_count",
    "skill_name",
    "repo_name",
    "source_branch",
    "normalized_skill_path",
    "inventory_revision",
    "source_revision",
    "skill_last_change_revision",
    "skill_digest",
    "cisco_status",
    "cisco_max_severity",
    "skillspector_status",
    "skillspector_max_severity",
    "ai_status",
    "ai_max_severity",
    "security_decision",
    "quality_score",
    "quality_level",
    "quality_dimensions",
    "manual_reason",
    "candidate_status",
    "evidence_ref",
    "source_selection_status",
    "review_policy_version",
    "reviewed_at",
    "failure_reason",
    "retry_required",
)


def _sort_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        _record_id(record),
        _repo(record),
        _skill_path(record),
        _skill_name(record),
        _source_revision(record),
        _digest(record),
    )


def _materialize(records: Iterable[Mapping[str, Any]], *, batch_id: str) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise ReportingError("each batch result must be a mapping")
        materialized.append(_normalized_record(item, batch_id=batch_id))
    return sorted(materialized, key=_sort_key)


def build_batch_summary(
    records: Iterable[Mapping[str, Any]],
    *,
    batch_id: str,
    input_csv_sha256: str | None = None,
    policy_version: str | None = None,
    generated_at: str | None = None,
    candidate_threshold: int = 70,
) -> dict[str, Any]:
    """Build the deterministic JSON-compatible batch overview."""

    if not _text(batch_id):
        raise ReportingError("batch_id must not be empty")
    if not isinstance(candidate_threshold, int) or isinstance(candidate_threshold, bool) or not 0 <= candidate_threshold <= 100:
        raise ReportingError("candidate_threshold must be between 0 and 100")
    rows = _materialize(records, batch_id=batch_id)
    repositories = {row["repo_name"] for row in rows if row["repo_name"]}
    digests = {row["skill_digest"] for row in rows if row["skill_digest"] and row["source_selection_status"] in {"", "SELECTED", "RECEIVED", "VALIDATING"}}
    security_counts = {status: 0 for status in SECURITY_DECISIONS}
    quality_counts = {level: 0 for level in QUALITY_LEVELS}
    for row in rows:
        decision = row["security_decision"]
        if decision in security_counts:
            security_counts[decision] += 1
        quality_counts[row["quality_level"]] += 1
    superseded = sum(row["source_selection_status"] == "SKIPPED_SUPERSEDED_BRANCH" for row in rows)
    conflicts = sum(
        row["source_selection_status"] in {"CONFLICT", "BRANCH_CONTENT_CONFLICT"} for row in rows
    )
    stale = sum(row["source_selection_status"] == "STALE_INVENTORY" for row in rows)
    candidate_count = sum(_candidate(row) for row in rows)
    failure_count = sum(row["is_failure"] for row in rows)
    retry_count = sum(row["retry_required"] for row in rows)
    summary: dict[str, Any] = {
        "schema_version": "0.1",
        "batch_id": batch_id,
        "input_csv_sha256": input_csv_sha256 or "",
        "review_policy_version": policy_version or "",
        "candidate_threshold": candidate_threshold,
        "repository_count": len(repositories),
        "source_row_count": sum(row["source_row_count"] for row in rows),
        "result_record_count": len(rows),
        "selected_content_version_count": len(digests),
        "superseded_branch_source_count": superseded,
        "branch_conflict_count": conflicts,
        "stale_inventory_count": stale,
        "security_decision_counts": security_counts,
        "quality_level_distribution": quality_counts,
        "candidate_count": candidate_count,
        "failure_count": failure_count,
        "retry_pending_count": retry_count,
        "tool_failure_or_incomplete_count": sum(
            row["cisco_status"] in _FAILURE_STATUSES or row["skillspector_status"] in _FAILURE_STATUSES
            for row in rows
        ),
        "records_without_digest_count": sum(not row["skill_digest"] for row in rows),
    }
    # The default intentionally omits wall-clock time: the same input results
    # must produce byte-for-byte deterministic summaries.  A caller may supply
    # the batch's recorded generation time when that audit field is required.
    if generated_at is not None:
        summary["generated_at"] = generated_at
    return summary


def build_detail_rows(records: Iterable[Mapping[str, Any]], *, batch_id: str) -> list[dict[str, Any]]:
    """Return stable report rows without writing a file."""

    return _materialize(records, batch_id=batch_id)


def write_details_csv(records: Iterable[Mapping[str, Any]], path: Path, *, batch_id: str) -> Path:
    """Write ``details.csv`` with a fixed field order and redacted values."""

    rows = build_detail_rows(records, batch_id=batch_id)
    output = []
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=DETAIL_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        safe_row: dict[str, Any] = {}
        for field in DETAIL_FIELDS:
            value = redact(row.get(field), key=field)
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            elif value is None:
                value = ""
            safe_row[field] = value
        writer.writerow(safe_row)
    _atomic_write_text(Path(path), stream.getvalue())
    return Path(path)


def _independent_result(row: Mapping[str, Any]) -> dict[str, Any]:
    """Fields required for a later ledger reconciliation, not raw evidence."""

    return dict(redact({
        "review_batch_id": row["batch_id"],
        "source_row_id": row["source_row_id"],
        "skill_name": row["skill_name"],
        "repo_name": row["repo_name"],
        "source_branch": row["source_branch"],
        "normalized_skill_path": row["normalized_skill_path"],
        "reviewed_source_revision": row["source_revision"],
        "reviewed_skill_digest": row["skill_digest"],
        "security_decision": row["security_decision"],
        "quality_score": row["quality_score"],
        "review_policy_version": row["review_policy_version"],
        "reviewed_at": row["reviewed_at"],
        "candidate_status": row["candidate_status"],
        "source_selection_status": row["source_selection_status"],
        "failure_reason": row["failure_reason"],
        "evidence_ref": row["evidence_ref"],
    }))


def build_failure_records(records: Iterable[Mapping[str, Any]], *, batch_id: str) -> list[dict[str, Any]]:
    rows = _materialize(records, batch_id=batch_id)
    return [_independent_result(row) for row in rows if row["is_failure"]]


def build_candidate_records(records: Iterable[Mapping[str, Any]], *, batch_id: str) -> list[dict[str, Any]]:
    rows = _materialize(records, batch_id=batch_id)
    return [_independent_result(row) for row in rows if _candidate(row)]


def write_batch_reports(
    records: Iterable[Mapping[str, Any]],
    output_dir: Path,
    *,
    batch_id: str,
    input_csv_sha256: str | None = None,
    policy_version: str | None = None,
    generated_at: str | None = None,
    candidate_threshold: int = 70,
) -> BatchReportPaths:
    """Write summary, details, failures and candidates as separate files.

    Records are materialized once so all four files are based on the same
    deterministic ordering and point-in-time input.  ``output_dir`` must be a
    directory (or a new path); the original CSV path is never touched.
    """

    output_dir = Path(output_dir)
    if output_dir.exists() and not output_dir.is_dir():
        raise ReportingError(f"report output is not a directory: {output_dir}")
    rows = _materialize(records, batch_id=batch_id)
    # Convert normalized rows back through the same public shape: this avoids
    # ever putting an unredacted source mapping in any report.
    summary = build_batch_summary(
        rows,
        batch_id=batch_id,
        input_csv_sha256=input_csv_sha256,
        policy_version=policy_version,
        generated_at=generated_at,
        candidate_threshold=candidate_threshold,
    )
    paths = BatchReportPaths(
        summary=output_dir / "batch-summary.json",
        details=output_dir / "details.csv",
        failures=output_dir / "failures.json",
        candidates=output_dir / "candidates.json",
    )
    _write_json(paths.summary, summary)
    # ``write_details_csv`` re-normalizes rows but remains deterministic.
    write_details_csv(rows, paths.details, batch_id=batch_id)
    _write_json(
        paths.failures,
        {"schema_version": "0.1", "batch_id": batch_id, "failures": [_independent_result(row) for row in rows if row["is_failure"]]},
    )
    _write_json(
        paths.candidates,
        {"schema_version": "0.1", "batch_id": batch_id, "candidates": [_independent_result(row) for row in rows if _candidate(row)]},
    )
    return paths


# Common names for callers that prefer a verb describing the operation.
generate_batch_reports = write_batch_reports
write_reports = write_batch_reports


__all__ = [
    "BatchReportPaths",
    "DETAIL_FIELDS",
    "QUALITY_LEVELS",
    "ReportingError",
    "SECURITY_DECISIONS",
    "build_batch_summary",
    "build_candidate_records",
    "build_detail_rows",
    "build_failure_records",
    "generate_batch_reports",
    "quality_level",
    "redact",
    "write_batch_reports",
    "write_details_csv",
    "write_reports",
]
