"""Read-only report projection for an in-progress or completed review batch.

This layer never advances review state. It projects the latest durable per-Skill
``current-result.json`` (or final ``review-result.json``) into the existing
CSV/JSON/HTML report workbench so operators can inspect static results before AI
review is complete.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import ReviewConfig
from .inventory import InventoryDocument, InventoryRow
from .reporting import BatchReportPaths, write_batch_reports


_REPORT_DATA_RE = re.compile(
    r'(<script type="application/json" id="report-data">)(.*?)(</script>)',
    re.DOTALL,
)
_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4, "NONE": 5, "": 6}
_PHASE_FIELDS = (
    "result_kind",
    "review_status",
    "static_status",
    "ai_status",
    "final_status",
    "static_security_decision",
)
_CURRENT_EXPORT_FIELDS = (
    "skill_id",
    "skill_name",
    "repo_name",
    "branch",
    "skill_path",
    "source_revision",
    "skill_digest",
    "result_kind",
    "review_status",
    "static_status",
    "static_security_decision",
    "ai_status",
    "final_status",
    "security_decision",
    "quality_decision",
    "quality_score",
    "max_severity",
    "finding_count",
    "evidence_ref",
)


@dataclass(frozen=True, slots=True)
class LiveReportResult:
    paths: BatchReportPaths
    current_json: Path
    current_csv: Path
    report_status: str
    progress: Mapping[str, int]


def _atomic_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    return _atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _load_json(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _result_for_row(config: ReviewConfig, row: InventoryRow) -> tuple[Mapping[str, Any], str]:
    identifier = str(row.trace_values.get("skill_id") or "").strip()
    if not identifier:
        return {}, "NONE"
    root = config.workspace.skills_root / identifier
    final = _load_json(root / "review-result.json")
    if final is not None and final.get("source_row_id") == row.source_row_id:
        return final, "FINAL"
    current = _load_json(root / "current-result.json")
    if current is not None and current.get("source_row_id") == row.source_row_id:
        return current, "CURRENT"
    return {}, "NONE"


def _phase_defaults(result: Mapping[str, Any], source_kind: str) -> dict[str, str]:
    if result.get("static_status") or result.get("ai_status") or result.get("final_status"):
        return {
            "static_status": str(result.get("static_status") or ""),
            "ai_status": str(result.get("ai_status") or ""),
            "final_status": str(result.get("final_status") or ""),
        }
    review_status = str(result.get("review_status") or "").upper()
    if source_kind == "FINAL" or review_status == "COMPLETED":
        return {
            "static_status": "COMPLETED",
            "ai_status": "NOT_REQUIRED" if result.get("reuse_status") else "COMPLETED",
            "final_status": "COMPLETED",
        }
    if review_status == "INCOMPLETE":
        return {
            "static_status": "INCOMPLETE",
            "ai_status": "NOT_REQUIRED",
            "final_status": "INCOMPLETE",
        }
    return {"static_status": "PENDING", "ai_status": "PENDING", "final_status": "PENDING"}


def build_live_records(
    config: ReviewConfig,
    inventory: InventoryDocument,
) -> list[dict[str, Any]]:
    """Materialize one report row per inventory row from the latest durable projection."""

    records: list[dict[str, Any]] = []
    for row in inventory.rows:
        result, source_kind = _result_for_row(config, row)
        selected = row.status in config.batch.included_statuses
        phases = _phase_defaults(result, source_kind) if selected else {
            "static_status": "",
            "ai_status": "NOT_REQUIRED",
            "final_status": "",
        }
        record = {
            **dict(row.raw),
            "source_row_id": row.source_row_id,
            "skill_id": row.trace_values.get("skill_id", ""),
            "skill_name": row.skill_name,
            "repo_name": row.repo_name,
            "branch": row.branch,
            "skill_path": row.skill_path,
            "inventory_revision": row.inventory_revision,
            "source_selection_status": "SELECTED" if selected else "SKIPPED_STATUS",
            "projection_source": source_kind,
            **phases,
            **dict(result),
        }
        # New result documents already carry phase fields. For legacy final
        # documents, retain the inferred values above.
        for name, value in phases.items():
            record.setdefault(name, value)
        records.append(record)
    return records


def phase_progress(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    selected = [row for row in records if row.get("source_selection_status") == "SELECTED"]
    total = len(selected)
    return {
        "selected": total,
        "static_completed": sum(str(row.get("static_status") or "") == "COMPLETED" for row in selected),
        "static_incomplete": sum(str(row.get("static_status") or "") == "INCOMPLETE" for row in selected),
        "ai_completed": sum(str(row.get("ai_status") or "") == "COMPLETED" for row in selected),
        "ai_not_required": sum(str(row.get("ai_status") or "") == "NOT_REQUIRED" for row in selected),
        "ai_pending": sum(str(row.get("ai_status") or "") in {"PENDING", "DISPATCHED"} for row in selected),
        "ai_failed": sum(str(row.get("ai_status") or "") == "FAILED" for row in selected),
        "final_completed": sum(str(row.get("final_status") or "") == "COMPLETED" for row in selected),
        "final_incomplete": sum(str(row.get("final_status") or "") == "INCOMPLETE" for row in selected),
        "final_pending": sum(str(row.get("final_status") or "") in {"", "PENDING"} for row in selected),
    }


def report_status(progress: Mapping[str, int]) -> str:
    total = int(progress.get("selected", 0))
    resolved = int(progress.get("final_completed", 0)) + int(progress.get("final_incomplete", 0))
    return "FINAL" if total > 0 and resolved == total else "INTERIM"


def _html_finding(item: Mapping[str, Any], *, row_id: str, index: int) -> dict[str, Any]:
    sources = item.get("source_scanners")
    if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
        source = ", ".join(str(value) for value in sources if value)
    else:
        source = str(item.get("source_scanner") or "静态扫描")
    return {
        "finding_key": f"{row_id}:{item.get('finding_id') or index}",
        "finding_id": item.get("finding_id") or "",
        "source": source,
        "source_rule_id": item.get("source_rule_id") or "",
        "severity": str(item.get("severity") or "INFO").upper(),
        "domain": str(item.get("domain") or "SECURITY").upper(),
        "category": item.get("category") or "",
        "path": item.get("file_path") or "",
        "start_line": item.get("start_line") or "",
        "end_line": item.get("end_line") or "",
        "locations": list(item.get("locations") or []),
        "title": item.get("title") or item.get("category") or "未命名问题",
        "description": item.get("description") or "",
        "evidence_summary": item.get("evidence_summary") or "",
        "recommendation": item.get("recommendation") or "",
        "confidence": item.get("confidence") or "",
        "fingerprint": item.get("fingerprint") or "",
        "status": item.get("status") or "",
        "source_references": list(item.get("source_references") or []),
    }


def _annotate_html(
    html_path: Path,
    *,
    records: Sequence[Mapping[str, Any]],
    status: str,
    progress: Mapping[str, int],
) -> None:
    html = html_path.read_text(encoding="utf-8")
    match = _REPORT_DATA_RE.search(html)
    if match is None:
        raise ValueError("generated HTML does not contain report-data payload")
    payload = json.loads(match.group(2))
    by_row = {str(row.get("source_row_id") or ""): row for row in records}
    severity_counts = {name: 0 for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}
    total_findings = 0
    for skill in payload.get("skills", []):
        if not isinstance(skill, dict):
            continue
        row = by_row.get(str(skill.get("source_row_id") or ""), {})
        for field in _PHASE_FIELDS:
            skill[field] = row.get(field, skill.get(field, ""))
        findings = [
            _html_finding(item, row_id=str(skill.get("source_row_id") or ""), index=index)
            for index, item in enumerate(row.get("findings") or [], 1)
            if isinstance(item, Mapping)
        ]
        skill["findings"] = findings
        skill["finding_count"] = len(findings)
        total_findings += len(findings)
        maximum = "NONE"
        for finding in findings:
            severity = str(finding.get("severity") or "INFO").upper()
            if severity in severity_counts:
                severity_counts[severity] += 1
            if _SEVERITY_ORDER.get(severity, 6) < _SEVERITY_ORDER.get(maximum, 6):
                maximum = severity
        skill["max_severity"] = maximum

    payload.setdefault("metadata", {})["report_status"] = status
    payload["metadata"]["phase_progress"] = dict(progress)
    payload.setdefault("summary", {})["report_status"] = status
    payload["summary"]["phase_progress"] = dict(progress)
    payload["summary"]["finding_count"] = total_findings
    payload["summary"]["finding_severity_counts"] = severity_counts
    payload["summary"]["critical_high_finding_count"] = severity_counts["CRITICAL"] + severity_counts["HIGH"]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = html[: match.start(2)] + encoded + html[match.end(2) :]

    total = int(progress.get("selected", 0))
    static_done = int(progress.get("static_completed", 0)) + int(progress.get("static_incomplete", 0))
    ai_done = int(progress.get("ai_completed", 0)) + int(progress.get("ai_not_required", 0))
    final_done = int(progress.get("final_completed", 0)) + int(progress.get("final_incomplete", 0))
    warning = (
        "当前为中间审计快照；AI 未完成的 Skill 不代表最终安全通过。"
        if status == "INTERIM"
        else "当前报告已形成最终批次投影。"
    )
    banner = (
        '<div data-report-status="' + status + '" style="padding:10px 24px;background:#fff7ed;'
        'border-bottom:1px solid #f0c9a8;color:#71401f;font:600 13px/1.5 Segoe UI,Microsoft YaHei UI,sans-serif">'
        f'<strong>{status}</strong> · Static {static_done}/{total} · AI {ai_done}/{total} · Final {final_done}/{total} · {warning}</div>'
    )
    html = html.replace("<body>", "<body>" + banner, 1)
    html = html.replace(
        "<title>Skill 安全审查报告",
        f"<title>[{status}] Skill 安全审查报告",
        1,
    )
    _atomic_text(html_path, html)


def _write_current_exports(
    output_root: Path,
    *,
    batch_id: str,
    records: Sequence[Mapping[str, Any]],
    status: str,
    progress: Mapping[str, int],
) -> tuple[Path, Path]:
    json_path = _atomic_json(
        output_root / "current-review-results.json",
        {
            "schema_version": "1.0",
            "batch_id": batch_id,
            "report_status": status,
            "phase_progress": dict(progress),
            "skills": [dict(row) for row in records],
        },
    )
    csv_path = output_root / "current-review-results.csv"
    rows: list[dict[str, Any]] = []
    for record in records:
        findings = record.get("findings") if isinstance(record.get("findings"), Sequence) else []
        counts: dict[str, int] = {}
        for finding in findings:
            if isinstance(finding, Mapping):
                severity = str(finding.get("severity") or "INFO").upper()
                counts[severity] = counts.get(severity, 0) + 1
        maximum = min(counts, key=lambda value: _SEVERITY_ORDER.get(value, 6), default="NONE")
        row = {field: record.get(field, "") for field in _CURRENT_EXPORT_FIELDS}
        row["max_severity"] = maximum
        row["finding_count"] = sum(counts.values())
        rows.append(row)
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=_CURRENT_EXPORT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_text(csv_path, stream.getvalue())
    return json_path, csv_path


def write_live_batch_report(
    config: ReviewConfig,
    inventory: InventoryDocument,
    *,
    batch_id: str,
) -> LiveReportResult:
    """Write an INTERIM or FINAL report snapshot from durable Skill projections."""

    records = build_live_records(config, inventory)
    progress = phase_progress(records)
    status = report_status(progress)
    output_root = config.workspace.results_root / batch_id
    paths = write_batch_reports(
        records,
        output_root,
        batch_id=batch_id,
        input_csv_sha256=inventory.raw_csv_sha256,
        policy_version=config.ai.policy_version,
        candidate_threshold=config.quality.candidate_threshold,
        evidence_root=config.workspace.evidence_root,
    )
    summary = _load_json(paths.summary) or {}
    _atomic_json(
        paths.summary,
        {
            **dict(summary),
            "report_status": status,
            "phase_progress": dict(progress),
        },
    )
    current_json, current_csv = _write_current_exports(
        output_root,
        batch_id=batch_id,
        records=records,
        status=status,
        progress=progress,
    )
    _annotate_html(paths.html, records=records, status=status, progress=progress)
    return LiveReportResult(paths, current_json, current_csv, status, progress)


__all__ = [
    "LiveReportResult",
    "build_live_records",
    "phase_progress",
    "report_status",
    "write_live_batch_report",
]
