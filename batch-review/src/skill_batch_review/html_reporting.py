"""Self-contained, redacted HTML workbench for a Skill review batch.

The report embeds normalized findings and an integrity index for restricted
evidence. Raw scanner documents stay in the evidence root and are never
copied into the HTML file.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .reporting import build_batch_summary, build_detail_rows, redact


_SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "INFO": 4,
    "NONE": 5,
    "": 6,
}
_MAX_FINAL_RESULT_BYTES = 20 * 1024 * 1024
_EVIDENCE_FILES = (
    ("final-result.json", "综合结论", "DERIVED"),
    ("source-metadata.json", "来源元数据", "SOURCE"),
    ("package-manifest.json", "包清单", "SOURCE"),
    ("scanners/cisco/normalized-result.json", "Cisco 规范化结果", "NORMALIZED"),
    ("scanners/cisco/raw-report.json", "Cisco 原始输出", "RAW"),
    ("scanners/skillspector/normalized-result.json", "SkillSpector 规范化结果", "NORMALIZED"),
    ("scanners/skillspector/raw-report.json", "SkillSpector 原始输出", "RAW"),
    ("ai/handoff.json", "AI 任务交接", "SOURCE"),
    ("ai/imported-result.json", "AI 审查结果", "RAW"),
    ("result-reuse.json", "结果复用记录", "DERIVED"),
)


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _first_text(item: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = _text(item.get(name))
        if value:
            return value
    return ""


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_evidence_directory(
    reference: Any, evidence_root: Path | None
) -> tuple[Path, Path] | None:
    """Resolve an evidence directory and reject escapes or symbolic links."""

    if evidence_root is None or not reference:
        return None
    raw_root = evidence_root.expanduser().absolute()
    try:
        root = raw_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    raw = Path(str(reference)).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(candidate))
    try:
        # Check symlinks only below the configured root. Platform aliases in
        # a parent path (for example macOS /var -> /private/var) are allowed.
        relative = lexical.relative_to(raw_root)
    except ValueError:
        relative = None
    current = raw_root
    if relative is None:
        try:
            target = lexical.resolve(strict=True)
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        relative = target.relative_to(root)
        current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return None
        except OSError:
            return None
    try:
        target = lexical.resolve(strict=True)
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not target.is_dir():
        return None
    return root, target


def _safe_evidence_bundle(reference: Any, evidence_root: Path | None) -> dict[str, Any]:
    """Return a redacted conclusion document and a hash-only evidence index."""

    resolved = _safe_evidence_directory(reference, evidence_root)
    if resolved is None:
        return {"document": None, "reference": "", "artifacts": []}
    root, target = resolved
    artifacts: list[dict[str, Any]] = []
    document: Mapping[str, Any] | None = None
    for relative_name, label, evidence_type in _EVIDENCE_FILES:
        path = target.joinpath(*relative_name.split("/"))
        try:
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(target)
        except (OSError, RuntimeError, ValueError):
            continue
        if path.is_symlink() or not resolved_path.is_file():
            continue
        try:
            size = resolved_path.stat().st_size
            checksum = _sha256(resolved_path)
        except OSError:
            continue
        artifacts.append(
            {
                "label": label,
                "type": evidence_type,
                "path": resolved_path.relative_to(root).as_posix(),
                "task_path": resolved_path.relative_to(target).as_posix(),
                "size_bytes": size,
                "sha256": checksum,
            }
        )
        if relative_name == "final-result.json" and size <= _MAX_FINAL_RESULT_BYTES:
            try:
                value = json.loads(resolved_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                safe_value = redact(value)
                document = safe_value if isinstance(safe_value, Mapping) else None
    return {
        "document": document,
        "reference": target.relative_to(root).as_posix(),
        "artifacts": artifacts,
    }


def _location(item: Mapping[str, Any]) -> tuple[str, str, str, list[dict[str, str]]]:
    locations = _mapping_list(item.get("locations"))
    location = item.get("location") if isinstance(item.get("location"), Mapping) else {}
    if not locations and location:
        locations = [location]
    first = locations[0] if locations else {}
    path = _first_text(item, "file_path", "path") or _first_text(
        first, "file_path", "path", "file"
    )
    start = _first_text(item, "start_line", "line_number", "line") or _first_text(
        first, "start_line", "line_number", "line"
    )
    end = _first_text(item, "end_line") or _first_text(first, "end_line")
    normalized = [
        {
            "path": _first_text(value, "file_path", "path", "file"),
            "start_line": _first_text(value, "start_line", "line_number", "line"),
            "end_line": _first_text(value, "end_line"),
        }
        for value in locations
    ]
    return path, start, end, normalized


def _extract_findings(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not document:
        return []
    collected: list[tuple[str, Mapping[str, Any]]] = []
    policy_findings = _mapping_list(document.get("findings"))
    if policy_findings:
        collected.extend(("综合结论", item) for item in policy_findings)
    else:
        for report in _mapping_list(document.get("static_reports")):
            scanner = _text(report.get("scanner"), "静态扫描")
            collected.extend(
                (scanner, item) for item in _mapping_list(report.get("findings"))
            )
        ai = document.get("ai_review")
        if isinstance(ai, Mapping):
            for section_name, label in (
                ("security_review", "AI 安全审查"),
                ("quality_review", "AI 质量审查"),
            ):
                section = ai.get(section_name)
                if isinstance(section, Mapping):
                    collected.extend(
                        (label, item) for item in _mapping_list(section.get("findings"))
                    )

    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for fallback_source, item in collected:
        path, start, end, locations = _location(item)
        raw_sources = item.get("source_scanners")
        source_scanners = (
            [_text(value) for value in raw_sources]
            if isinstance(raw_sources, list)
            else []
        )
        source = (
            ", ".join(value for value in source_scanners if value)
            or _first_text(item, "source_scanner", "scanner")
            or fallback_source
        )
        title = (
            _first_text(item, "title", "rule_id", "source_rule_id", "category")
            or "未命名问题"
        )
        description = _first_text(
            item, "description", "message", "explanation", "finding", "evidence"
        )
        evidence_summary = _first_text(item, "evidence_summary", "evidence", "detail")
        recommendation = _first_text(
            item, "recommendation", "remediation", "suggestion"
        )
        severity = (_text(item.get("severity"), "INFO") or "INFO").upper()
        domain = (_first_text(item, "domain", "category") or "SECURITY").upper()
        finding_id = _first_text(
            item, "finding_id", "id", "uuid", "source_rule_id", "rule_id"
        )
        source_rule_id = _first_text(item, "source_rule_id", "rule_id")
        references = [
            dict(redact(value)) for value in _mapping_list(item.get("source_references"))
        ]
        key = tuple(
            str(value)
            for value in (
                source,
                severity,
                domain,
                path,
                start,
                title,
                description,
                finding_id,
            )
        )
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {
                "finding_id": finding_id,
                "source": source,
                "source_rule_id": source_rule_id,
                "severity": severity,
                "domain": domain,
                "category": _first_text(item, "category"),
                "path": path,
                "start_line": start,
                "end_line": end,
                "locations": locations,
                "title": title,
                "description": description[:2400],
                "evidence_summary": evidence_summary[:2400],
                "recommendation": recommendation[:1600],
                "confidence": _first_text(item, "confidence"),
                "fingerprint": _first_text(item, "fingerprint"),
                "status": _first_text(item, "status"),
                "source_references": references,
            }
        )
    return sorted(
        findings,
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item["severity"]), 6),
            str(item["path"]),
            str(item["title"]),
        ),
    )


def _max_severity(
    findings: Sequence[Mapping[str, Any]], row: Mapping[str, Any]
) -> str:
    values = {_text(item.get("severity")).upper() for item in findings}
    if not values:
        values = {
            _text(row.get("cisco_max_severity")).upper(),
            _text(row.get("skillspector_max_severity")).upper(),
            _text(row.get("ai_max_severity")).upper(),
        }
    return min(
        values,
        key=lambda value: _SEVERITY_ORDER.get(value, 6),
        default="NONE",
    ) or "NONE"


def build_html_report_payload(
    records: Iterable[Mapping[str, Any]],
    *,
    batch_id: str,
    input_csv_sha256: str | None = None,
    policy_version: str | None = None,
    generated_at: str | None = None,
    candidate_threshold: int = 70,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Build the redacted data model embedded by the offline report."""

    source = [dict(redact(item)) for item in records]
    details = build_detail_rows(source, batch_id=batch_id)
    summary = build_batch_summary(
        source,
        batch_id=batch_id,
        input_csv_sha256=input_csv_sha256,
        policy_version=policy_version,
        generated_at=generated_at,
        candidate_threshold=candidate_threshold,
    )
    skills: list[dict[str, Any]] = []
    severity_counts = {
        name: 0 for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
    }
    total_findings = 0
    for row in details:
        bundle = _safe_evidence_bundle(row.get("evidence_ref"), evidence_root)
        findings = _extract_findings(bundle.get("document"))
        for index, finding in enumerate(findings, 1):
            finding["finding_key"] = (
                f"{row['source_row_id']}:{finding.get('finding_id') or index}"
            )
            if finding["severity"] in severity_counts:
                severity_counts[finding["severity"]] += 1
        total_findings += len(findings)
        skill = dict(row)
        # Do not embed absolute workstation paths in the distributable report.
        skill["evidence_ref"] = bundle["reference"]
        skill["findings"] = findings
        skill["finding_count"] = len(findings)
        skill["max_severity"] = _max_severity(findings, row)
        skill["evidence_artifacts"] = bundle["artifacts"]
        skills.append(skill)
    summary["finding_count"] = total_findings
    summary["finding_severity_counts"] = severity_counts
    summary["critical_high_finding_count"] = (
        severity_counts["CRITICAL"] + severity_counts["HIGH"]
    )
    result = redact(
        {
            "schema_version": "1.0",
            "metadata": {
                "batch_id": batch_id,
                "input_csv_sha256": input_csv_sha256 or "",
                "policy_version": policy_version or "",
                "generated_at": generated_at or "",
                "candidate_threshold": candidate_threshold,
                "evidence_mode": "REDACTED_DERIVED_WITH_RESTRICTED_INDEX",
            },
            "summary": summary,
            "skills": skills,
        }
    )
    return dict(result) if isinstance(result, Mapping) else {}


def _script_safe_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _atomic_write(path: Path, content: str) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
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


_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>Skill 安全审查报告 · __BATCH_ID__</title>
<style>
:root{--ink:#152238;--ink2:#33425b;--muted:#68758a;--line:#d8e0ea;--canvas:#edf1f6;--paper:#fff;--paper2:#f7f9fc;--navy:#10243f;--blue:#2f63d7;--blue2:#eaf0ff;--teal:#087b83;--teal2:#e7f5f4;--orange:#b75416;--orange2:#fff0e5;--red:#b42318;--red2:#fdecea;--green:#176b45;--green2:#e9f6ef;--shadow:0 10px 28px rgba(30,48,75,.08);--sans:"Segoe UI","Microsoft YaHei UI","PingFang SC",Arial,sans-serif;--mono:"Cascadia Mono","SFMono-Regular",Consolas,monospace}
*{box-sizing:border-box}html{height:100%;background:var(--canvas)}body{margin:0;min-height:100%;color:var(--ink);font:14px/1.55 var(--sans);background:var(--canvas)}button,input,select{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:3px solid rgba(47,99,215,.25);outline-offset:2px}
.topbar{height:76px;background:var(--navy);color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 max(28px,calc((100vw - 1800px)/2));border-bottom:4px solid #376bd7}.brand{display:flex;align-items:center;gap:15px}.brand-mark{width:38px;height:38px;border:1px solid #6f90bd;border-radius:9px;display:grid;place-items:center;font-weight:800;background:#183555}.brand h1{font-size:20px;margin:0}.brand p{margin:2px 0 0;color:#adbed5;font-size:12px}.batch-meta{display:flex;align-items:center;gap:28px}.batch-meta div{display:grid;gap:2px}.batch-meta span{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:#94a9c6}.batch-meta b,.batch-meta code{font-size:12px;color:#f6f9ff}.batch-meta code{font-family:var(--mono);max-width:210px;overflow:hidden;text-overflow:ellipsis}
.app{width:min(1800px,calc(100% - 36px));margin:18px auto 34px;display:grid;grid-template-columns:286px minmax(0,1fr);gap:18px;min-height:calc(100vh - 128px)}.sidebar,.workspace{background:var(--paper);border:1px solid var(--line);box-shadow:var(--shadow)}.sidebar{border-radius:12px;padding:20px;align-self:start;position:sticky;top:18px;max-height:calc(100vh - 36px);overflow:auto}.workspace{border-radius:12px;min-width:0;overflow:hidden}.sidebar-head{display:flex;align-items:flex-start;justify-content:space-between;padding-bottom:17px;border-bottom:1px solid var(--line)}.sidebar-head h2{font-size:16px;margin:0}.sidebar-head span{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}.link-button{border:0;background:none;color:var(--blue);font-size:12px;padding:2px}.filter-group{margin-top:16px}.filter-group label{display:block;color:var(--ink2);font-size:12px;font-weight:650;margin:0 0 6px}.control{width:100%;height:38px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;color:var(--ink);padding:0 10px}.control:hover{border-color:#9aabc1}.filter-note{margin-top:16px;padding:11px 12px;border-radius:7px;background:var(--paper2);color:var(--muted);font-size:12px}.filter-note strong{display:block;color:var(--ink);font-size:18px}.sidebar-actions{display:grid;gap:8px;margin-top:18px}.button{border:1px solid #bdc9d8;background:#fff;color:var(--ink);border-radius:7px;padding:9px 12px;text-align:center;font-weight:650}.button:hover{border-color:#8397b1;background:#f8fafc}.button.primary{background:var(--blue);border-color:var(--blue);color:#fff}.button.primary:hover{background:#2557c2}.button.subtle{font-weight:500;color:var(--ink2)}
.workspace-head{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;padding:24px 28px 18px;border-bottom:1px solid var(--line);background:#fbfcfe}.eyebrow{font-size:10px;letter-spacing:.15em;color:var(--blue);font-weight:800}.workspace-head h2{font-size:25px;margin:4px 0 2px}.workspace-head p{margin:0;color:var(--muted)}.scope-indicator{padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink2);font-size:12px;white-space:nowrap}.scope-indicator strong{color:var(--blue);font-size:16px}.tabs{display:flex;align-items:center;gap:2px;padding:0 28px;border-bottom:1px solid var(--line);background:#fbfcfe;overflow:auto}.tab{border:0;background:transparent;padding:13px 16px;color:var(--muted);font-weight:650;border-bottom:3px solid transparent;white-space:nowrap}.tab:hover{color:var(--ink)}.tab.active{color:var(--blue);border-bottom-color:var(--blue)}.view{padding:24px 28px 32px}.view[hidden]{display:none}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px}.metric{border:1px solid var(--line);border-top:3px solid #8fa2b9;background:var(--paper);padding:15px 16px;min-height:112px}.metric[data-tone=bad]{border-top-color:var(--red)}.metric[data-tone=warn]{border-top-color:var(--orange)}.metric[data-tone=ok]{border-top-color:var(--green)}.metric[data-tone=accent]{border-top-color:var(--blue)}.metric span{display:block;font-size:11px;color:var(--muted)}.metric strong{display:block;font-size:29px;line-height:1.2;margin:7px 0 4px}.metric small{font-size:11px;color:var(--muted)}.overview-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(340px,.85fr);gap:14px;margin-top:14px}.panel,.data-panel{border:1px solid var(--line);background:#fff;min-width:0}.panel-head,.data-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--paper2)}.panel-head h3,.data-head h3{font-size:14px;margin:0}.panel-head span,.data-head p,.data-head>span{font-size:11px;color:var(--muted);margin:2px 0 0}.panel-body{padding:17px}.distribution{display:grid;gap:12px}.dist-row{display:grid;grid-template-columns:75px 1fr 42px;gap:11px;align-items:center}.dist-row label{font-size:11px;font-weight:700}.dist-track{height:8px;background:#e9eef4;border-radius:10px;overflow:hidden}.dist-fill{height:100%;background:#8fa2b9}.dist-fill.critical,.dist-fill.high{background:var(--red)}.dist-fill.medium{background:var(--orange)}.dist-fill.low{background:var(--blue)}.dist-fill.info{background:var(--teal)}.dist-row b{text-align:right;font:12px var(--mono)}
.watch-list{display:grid;gap:9px}.watch-item{border-left:3px solid var(--line);padding:8px 10px;background:var(--paper2);display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}.watch-item.bad{border-left-color:var(--red)}.watch-item.warn{border-left-color:var(--orange)}.watch-item button{border:0;background:none;padding:0;color:var(--ink);text-align:left;overflow:hidden}.watch-item strong,.watch-item small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.watch-item small{color:var(--muted);margin-top:2px}.watch-item em{font-style:normal;font:11px var(--mono);color:var(--muted)}
.table-wrap{max-height:calc(100vh - 285px);overflow:auto}.data-table{width:100%;border-collapse:collapse;min-width:1040px}.data-table th,.data-table td{text-align:left;padding:11px 13px;border-bottom:1px solid #e9edf3;vertical-align:top}.data-table th{position:sticky;top:0;background:#f4f7fa;z-index:2;font-size:10px;letter-spacing:.04em;text-transform:uppercase;color:#65758b}.data-table tbody tr{cursor:pointer}.data-table tbody tr:hover{background:#f6f9ff}.primary-cell strong,.primary-cell small{display:block;max-width:290px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.primary-cell small,.subline{color:var(--muted);font-size:11px;margin-top:2px}.mono{font-family:var(--mono);font-size:11px;overflow-wrap:anywhere}.badge{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:3px 8px;font-size:10px;font-weight:750;white-space:nowrap;background:#edf1f5;color:#4d5d72}.badge:before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}.badge.ok{background:var(--green2);color:var(--green)}.badge.bad{background:var(--red2);color:var(--red)}.badge.warn{background:var(--orange2);color:var(--orange)}.badge.info{background:var(--blue2);color:var(--blue)}.empty{padding:48px 22px;text-align:center;color:var(--muted)}.empty strong{display:block;color:var(--ink);font-size:16px;margin-bottom:5px}.mini-bars{display:flex;gap:4px;margin-top:5px}.mini-bars span{height:4px;border-radius:3px;min-width:5px}.mini-bars .pass{background:var(--green)}.mini-bars .review{background:var(--orange)}.mini-bars .block{background:var(--red)}.mini-bars .incomplete{background:#77869b}
.drawer-backdrop{position:fixed;inset:0;background:rgba(11,24,42,.38);z-index:20;opacity:0;pointer-events:none;transition:opacity .16s ease}.drawer-backdrop.open{opacity:1;pointer-events:auto}.drawer{position:absolute;right:0;top:0;width:min(840px,92vw);height:100%;background:#fff;box-shadow:-18px 0 45px rgba(8,20,38,.18);display:flex;flex-direction:column;transform:translateX(30px);transition:transform .16s ease}.drawer-backdrop.open .drawer{transform:none}.drawer-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:22px 25px;border-bottom:1px solid var(--line);background:var(--paper2)}.drawer-head h2{font-size:23px;margin:4px 0}.drawer-head p{margin:0;color:var(--muted)}.icon-button{width:36px;height:36px;border:1px solid var(--line);background:#fff;border-radius:7px;color:var(--ink);font-size:20px}.drawer-body{padding:22px 25px 38px;overflow:auto}.drawer-section{margin-top:24px}.drawer-section:first-child{margin-top:0}.drawer-section h3{font-size:14px;margin:0 0 10px}.trace{display:grid;grid-template-columns:repeat(5,1fr);border:1px solid var(--line);background:#fbfcfe}.trace-step{min-width:0;padding:12px;position:relative}.trace-step:not(:last-child){border-right:1px solid var(--line)}.trace-step:not(:last-child):after{content:"›";position:absolute;right:-7px;top:25px;width:13px;background:#fbfcfe;color:#90a0b5;text-align:center}.trace-step span,.trace-step b{display:block}.trace-step span{font-size:10px;color:var(--muted);margin-bottom:5px}.trace-step b{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border:1px solid var(--line)}.fact{padding:11px 13px;border-bottom:1px solid var(--line);min-width:0}.fact:nth-child(odd){border-right:1px solid var(--line)}.fact span,.fact b{display:block}.fact span{font-size:10px;color:var(--muted);margin-bottom:3px}.fact b{font-size:12px;overflow-wrap:anywhere}.fact.wide{grid-column:1/-1;border-right:0}
.finding-stack{display:grid;gap:10px}.finding-card{border:1px solid var(--line);border-left:4px solid #8090a5;padding:13px 15px;background:#fff}.finding-card.high,.finding-card.critical{border-left-color:var(--red)}.finding-card.medium{border-left-color:var(--orange)}.finding-card.low{border-left-color:var(--blue)}.finding-card.info{border-left-color:var(--teal)}.finding-card button{display:block;width:100%;border:0;background:none;text-align:left;padding:0;color:inherit}.finding-card-head{display:flex;align-items:center;gap:8px}.finding-card-head code{margin-left:auto;font:10px var(--mono);color:var(--muted)}.finding-card h4{font-size:14px;margin:9px 0 4px}.finding-card p{margin:0;color:var(--ink2);font-size:12px}.finding-detail{border:1px solid var(--line);background:#fff}.finding-detail-head{padding:16px 18px;border-bottom:1px solid var(--line);background:var(--paper2)}.finding-detail-head h3{font-size:18px;margin:8px 0 2px}.finding-detail-head p{margin:0;color:var(--muted)}.detail-block{padding:15px 18px;border-bottom:1px solid var(--line)}.detail-block:last-child{border-bottom:0}.detail-block h4{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0 0 7px}.detail-block p{margin:0;white-space:pre-wrap}.source-ref-list{display:grid;gap:6px}.source-ref{padding:8px 10px;background:var(--paper2);border:1px solid #e5eaf0;font:10px/1.5 var(--mono);overflow-wrap:anywhere}
.evidence-note{padding:11px 13px;background:#fff7ed;border-left:3px solid var(--orange);font-size:12px;color:#71401f;margin-bottom:10px}.evidence-list{border:1px solid var(--line)}.evidence-item{display:grid;grid-template-columns:170px 100px minmax(0,1fr) 90px;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);align-items:start}.evidence-item:last-child{border-bottom:0}.evidence-item strong{font-size:11px}.evidence-item span,.evidence-item code{font-size:10px;color:var(--muted)}.evidence-item code{font-family:var(--mono);overflow-wrap:anywhere}.raw-index{margin-top:6px;font:10px/1.5 var(--mono);color:var(--muted);overflow-wrap:anywhere}.page-foot{margin:0 auto 28px;color:var(--muted);font-size:11px;display:flex;justify-content:space-between;gap:16px}.noscript{margin:20px;padding:18px;border:1px solid var(--red);color:var(--red);background:#fff}
@media(max-width:1250px){.metrics{grid-template-columns:repeat(3,1fr)}.overview-grid{grid-template-columns:1fr}.batch-meta div:nth-child(n+3){display:none}.trace{grid-template-columns:1fr}.trace-step:not(:last-child){border-right:0;border-bottom:1px solid var(--line)}.trace-step:after{display:none}}
@media(max-width:860px){.topbar{height:auto;padding:16px 18px}.batch-meta{display:none}.app{width:calc(100% - 20px);grid-template-columns:1fr}.sidebar{position:static;max-height:none}.workspace-head{align-items:flex-start;flex-direction:column}.view{padding:18px}.tabs{padding:0 14px}.metrics{grid-template-columns:repeat(2,1fr)}.facts{grid-template-columns:1fr}.fact:nth-child(odd){border-right:0}.evidence-item{grid-template-columns:1fr}.page-foot{flex-direction:column}}
@media print{body{background:#fff}.app{width:100%;display:block;margin:0}.sidebar,.tabs,.scope-indicator,.page-foot{display:none}.workspace{border:0;box-shadow:none}.view{display:block!important;padding:16px}.view:not(#view-overview):not(#view-skills){display:none!important}.table-wrap{max-height:none;overflow:visible}.data-table th{position:static}.drawer-backdrop{display:none}.metrics{grid-template-columns:repeat(3,1fr)}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style>
</head>
<body>
<header class="topbar"><div class="brand"><div class="brand-mark" aria-hidden="true">S✓</div><div><h1>Skill 安全审查报告</h1><p>统一结论、问题详情与受限证据索引</p></div></div><div class="batch-meta"><div><span>批次</span><b>__BATCH_ID__</b></div><div><span>策略版本</span><b>__POLICY_VERSION__</b></div><div><span>生成时间</span><b>__GENERATED_AT__</b></div><div><span>输入 SHA-256</span><code>__INPUT_SHA__</code></div></div></header>
<div class="app"><aside class="sidebar" aria-label="报告筛选"><div class="sidebar-head"><div><h2>筛选范围</h2><span>REPORT SCOPE</span></div><button class="link-button" id="reset-filters" type="button">清空</button></div>
<div class="filter-group"><label for="filter-search">全文检索</label><input class="control" id="filter-search" type="search" placeholder="Skill / 仓库 / 路径 / 问题"></div>
<div class="filter-group"><label for="filter-repo">仓库</label><select class="control" id="filter-repo"><option value="">全部仓库</option></select></div>
<div class="filter-group"><label for="filter-product">产品线</label><select class="control" id="filter-product"><option value="">全部产品线</option></select></div>
<div class="filter-group"><label for="filter-person">提交人</label><select class="control" id="filter-person"><option value="">全部提交人</option></select></div>
<div class="filter-group"><label for="filter-decision">安全结论</label><select class="control" id="filter-decision"><option value="">全部结论</option></select></div>
<div class="filter-group"><label for="filter-severity">问题等级</label><select class="control" id="filter-severity"><option value="">全部等级</option><option>CRITICAL</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>INFO</option><option>NONE</option></select></div>
<div class="filter-note"><strong id="scope-count">0 / 0</strong>当前范围内 Skill；所有视图与导出使用同一筛选条件。</div><div class="sidebar-actions"><button class="button primary" id="export-csv" type="button">导出当前视图 CSV</button><button class="button" id="export-json" type="button">导出筛选结果 JSON</button><button class="button subtle" id="print-report" type="button">打印 / 保存 PDF</button></div></aside>
<main class="workspace"><div class="workspace-head"><div><span class="eyebrow">AUDIT WORKBENCH</span><h2 id="view-title">审查总览</h2><p id="view-description">批次健康度、风险分布和需要优先处理的对象。</p></div><div class="scope-indicator">当前显示 <strong id="visible-count">0</strong> 个 Skill</div></div>
<nav class="tabs" aria-label="报告层级"><button class="tab active" data-tab="overview" type="button">总览</button><button class="tab" data-tab="skills" type="button">单 Skill</button><button class="tab" data-tab="findings" type="button">问题清单</button><button class="tab" data-tab="repositories" type="button">仓库</button><button class="tab" data-tab="submitters" type="button">提交人</button></nav>
<section class="view" id="view-overview"></section><section class="view" id="view-skills" hidden></section><section class="view" id="view-findings" hidden></section><section class="view" id="view-repositories" hidden></section><section class="view" id="view-submitters" hidden></section></main></div>
<footer class="page-foot" style="width:min(1800px,calc(100% - 36px))"><span>报告仅嵌入脱敏派生数据；原始输出保留在受限证据区。</span><span>没有发现问题不等于不存在风险，检查未完成时不得视为通过。</span></footer>
<div class="drawer-backdrop" id="drawer-backdrop" hidden><aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><div class="drawer-head"><div><span class="eyebrow" id="drawer-eyebrow">DETAIL</span><h2 id="drawer-title">详情</h2><p id="drawer-subtitle"></p></div><button class="icon-button" id="drawer-close" type="button" aria-label="关闭详情">×</button></div><div class="drawer-body" id="drawer-body"></div></aside></div>
<noscript><div class="noscript">该离线报告需要浏览器启用 JavaScript 才能筛选、查看详情和导出。</div></noscript><script type="application/json" id="report-data">__REPORT_DATA__</script><script>__APP_SCRIPT__</script>
</body></html>'''

_APP_SCRIPT = r'''(() => {
  'use strict';
  const report = JSON.parse(document.getElementById('report-data').textContent);
  const allSkills = Array.isArray(report.skills) ? report.skills : [];
  const severityOrder = {CRITICAL:0,HIGH:1,MEDIUM:2,LOW:3,INFO:4,NONE:5,'':6};
  const state = {tab:'overview',search:'',repo:'',product:'',person:'',decision:'',severity:''};
  const descriptions = {
    overview:['审查总览','批次健康度、风险分布和需要优先处理的对象。'],
    skills:['单 Skill 审查','逐项查看来源、双静态扫描、AI 结论、质量得分与证据链。'],
    findings:['问题清单','按严重等级清洗后的问题明细；点击任意行查看证据与定位。'],
    repositories:['仓库视图','按仓库汇总 Skill、问题、提交人与综合结论。'],
    submitters:['提交人视图','按 user_name 与 user_email 汇总其关联仓库和审查结果。']
  };
  const $ = selector => document.querySelector(selector);
  const node = (tag,className,content) => {
    const item=document.createElement(tag);
    if(className)item.className=className;
    if(content!==undefined)item.textContent=String(content);
    return item;
  };
  const add = (parent,...children) => {
    children.filter(Boolean).forEach(child=>parent.appendChild(child));
    return parent;
  };
  const text = value => value===null||value===undefined||value===''?'—':String(value);
  const personKey = skill => [skill.user_name||'',skill.user_email||''].join('|');
  const personLabel = skill => skill.user_name&&skill.user_email
    ? skill.user_name+' · '+skill.user_email
    : (skill.user_name||skill.user_email||'未登记');
  const tone = value => {
    const current=String(value||'').toUpperCase();
    if(['PASS','PASSED','APPROVED','COMPLETED','READY_TO_EXPORT','EXPORTED_LOCAL','NONE'].includes(current))return'ok';
    if(['BLOCK','BLOCKED','REJECT','REJECTED','FAILED','ERROR','CRITICAL','HIGH'].includes(current))return'bad';
    if(['REVIEW_REQUIRED','MANUAL_REVIEW','INCOMPLETE','MEDIUM','TIMEOUT'].includes(current))return'warn';
    return'info';
  };
  const badge = value => node('span','badge '+tone(value),text(value));
  const allFindings = skills => skills.flatMap(skill=>(skill.findings||[]).map(finding=>({skill,finding})));
  const searchable = skill => [
    skill.skill_id,skill.skill_name,skill.repo_name,skill.product_line,
    skill.user_name,skill.user_email,skill.source_branch,skill.normalized_skill_path,
    skill.source_revision,skill.skill_digest,skill.security_decision,
    ...(skill.findings||[]).flatMap(item=>[
      item.title,item.description,item.evidence_summary,item.path,item.source,item.source_rule_id
    ])
  ].join(' ').toLowerCase();
  const matches = skill => {
    if(state.search&&!searchable(skill).includes(state.search))return false;
    if(state.repo&&skill.repo_name!==state.repo)return false;
    if(state.product&&skill.product_line!==state.product)return false;
    if(state.person&&personKey(skill)!==state.person)return false;
    if(state.decision&&skill.security_decision!==state.decision)return false;
    if(state.severity){
      const findings=skill.findings||[];
      if(state.severity==='NONE'&&findings.length)return false;
      if(state.severity!=='NONE'&&!findings.some(item=>item.severity===state.severity))return false;
    }
    return true;
  };
  const visibleSkills = () => allSkills.filter(matches);

  function fillOptions(selector,values,label=value=>value){
    const select=$(selector);
    [...new Set(values.filter(Boolean))]
      .sort((a,b)=>String(label(a)).localeCompare(String(label(b)),'zh-CN'))
      .forEach(value=>{const option=node('option','',label(value));option.value=value;select.appendChild(option);});
  }
  fillOptions('#filter-repo',allSkills.map(item=>item.repo_name));
  fillOptions('#filter-product',allSkills.map(item=>item.product_line));
  const people=new Map();
  allSkills.forEach(skill=>{if(skill.user_name||skill.user_email)people.set(personKey(skill),personLabel(skill));});
  fillOptions('#filter-person',[...people.keys()],key=>people.get(key));
  fillOptions('#filter-decision',allSkills.map(item=>item.security_decision));

  function metric(label,value,note,toneName){
    const card=node('article','metric');
    if(toneName)card.dataset.tone=toneName;
    return add(card,node('span','',label),node('strong','',value),node('small','',note));
  }
  function empty(title,detail){
    return add(node('div','empty'),node('strong','',title),node('span','',detail));
  }
  function aggregate(skills,keyFn,labelFn){
    const groups=new Map();
    skills.forEach(skill=>{
      const key=keyFn(skill)||'未登记';
      if(!groups.has(key))groups.set(key,{key,label:labelFn(skill)||'未登记',skills:[],findings:[],repos:new Set(),people:new Set()});
      const group=groups.get(key);
      group.skills.push(skill);
      group.findings.push(...(skill.findings||[]));
      if(skill.repo_name)group.repos.add(skill.repo_name);
      if(skill.user_name||skill.user_email)group.people.add(personKey(skill));
    });
    return [...groups.values()].sort((a,b)=>
      b.findings.length-a.findings.length||b.skills.length-a.skills.length||a.label.localeCompare(b.label,'zh-CN')
    );
  }
  function counts(skills){
    const decisions={PASS:0,REVIEW_REQUIRED:0,BLOCKED:0,INCOMPLETE:0};
    skills.forEach(skill=>{if(Object.prototype.hasOwnProperty.call(decisions,skill.security_decision))decisions[skill.security_decision]++;});
    const findings={CRITICAL:0,HIGH:0,MEDIUM:0,LOW:0,INFO:0};
    allFindings(skills).forEach(({finding})=>{if(Object.prototype.hasOwnProperty.call(findings,finding.severity))findings[finding.severity]++;});
    return {decisions,findings,total:Object.values(findings).reduce((sum,value)=>sum+value,0)};
  }
  function decisionBars(group){
    const wrap=node('div','mini-bars');
    const values={pass:0,review:0,block:0,incomplete:0};
    group.skills.forEach(skill=>{
      if(skill.security_decision==='PASS')values.pass++;
      else if(skill.security_decision==='REVIEW_REQUIRED')values.review++;
      else if(skill.security_decision==='BLOCKED')values.block++;
      else values.incomplete++;
    });
    Object.entries(values).forEach(([name,value])=>{
      if(!value)return;
      const bar=node('span',name);
      bar.style.width=Math.max(8,value/group.skills.length*100)+'px';
      bar.title=name+': '+value;
      wrap.appendChild(bar);
    });
    return wrap;
  }
  function renderOverview(skills){
    const host=$('#view-overview');host.replaceChildren();
    const current=counts(skills);
    const repos=new Set(skills.map(item=>item.repo_name).filter(Boolean));
    const peopleSet=new Set(skills.filter(item=>item.user_name||item.user_email).map(personKey));
    const reuse=skills.filter(item=>item.reuse_status==='RESULT_REUSED').length;
    add(host,add(node('div','metrics'),
      metric('当前 Skill',skills.length,'清单共 '+allSkills.length,'accent'),
      metric('涉及仓库',repos.size,'按 repo_name 汇总'),
      metric('高危问题',current.findings.CRITICAL+current.findings.HIGH,'Critical + High','bad'),
      metric('安全通过',current.decisions.PASS,'仅完整结论','ok'),
      metric('提交人',peopleSet.size,'name + email'),
      metric('结果复用',reuse,'内容摘要一致','warn')
    ));
    const grid=node('div','overview-grid');
    const distribution=node('section','panel');
    add(distribution,add(node('div','panel-head'),node('h3','','问题等级分布'),node('span','',current.total+' 项清洗后问题')));
    const bars=node('div','panel-body distribution');
    const maximum=Math.max(1,...Object.values(current.findings));
    Object.entries(current.findings).forEach(([severity,value])=>{
      const row=node('div','dist-row');
      const track=node('div','dist-track');
      const fill=node('div','dist-fill '+severity.toLowerCase());
      fill.style.width=value/maximum*100+'%';track.appendChild(fill);
      add(row,node('label','',severity),track,node('b','',value));bars.appendChild(row);
    });
    distribution.appendChild(bars);
    const watch=node('section','panel');
    add(watch,add(node('div','panel-head'),node('h3','','优先关注'),node('span','','按风险与问题数排序')));
    const watchBody=node('div','panel-body watch-list');
    const ranked=[...skills].sort((a,b)=>(severityOrder[a.max_severity]??6)-(severityOrder[b.max_severity]??6)||(b.finding_count||0)-(a.finding_count||0)).slice(0,7);
    ranked.forEach(skill=>{
      const item=node('div','watch-item '+tone(skill.max_severity));
      const button=add(node('button'),node('strong','',skill.skill_name),node('small','',skill.repo_name+' / '+skill.normalized_skill_path));
      button.addEventListener('click',()=>openSkill(skill));
      add(item,button,node('em','',(skill.finding_count||0)+' findings'));watchBody.appendChild(item);
    });
    if(!ranked.length)watchBody.appendChild(empty('当前范围没有 Skill','请调整筛选条件。'));
    watch.appendChild(watchBody);add(grid,distribution,watch);host.appendChild(grid);
    const repoPanel=node('section','panel');repoPanel.style.marginTop='14px';
    add(repoPanel,add(node('div','panel-head'),node('h3','','仓库风险快照'),node('span','',repos.size+' 个仓库')));
    const repoBody=node('div','panel-body watch-list');
    aggregate(skills,item=>item.repo_name,item=>item.repo_name).slice(0,8).forEach(group=>{
      const item=node('div','watch-item '+(group.findings.some(f=>['CRITICAL','HIGH'].includes(f.severity))?'bad':''));
      const button=add(node('button'),node('strong','',group.label),node('small','',group.skills.length+' Skills · '+group.people.size+' 提交人'));
      button.addEventListener('click',()=>drillDown('repo',group.key));
      add(item,button,node('em','',group.findings.length+' findings'));repoBody.appendChild(item);
    });
    if(!repoBody.children.length)repoBody.appendChild(empty('没有仓库数据','当前筛选范围为空。'));
    repoPanel.appendChild(repoBody);host.appendChild(repoPanel);
  }
  function tablePanel(title,description,columns,rows,onClick){
    const panel=node('section','data-panel');
    const copy=add(node('div'),node('h3','',title),node('p','',description));
    add(panel,add(node('div','data-head'),copy,node('span','subline',rows.length+' 条')));
    if(!rows.length){panel.appendChild(empty('没有匹配结果','请放宽筛选条件。'));return panel;}
    const table=node('table','data-table');
    const header=node('tr');columns.forEach(column=>header.appendChild(node('th','',column.label)));
    const body=node('tbody');
    rows.forEach(row=>{
      const line=node('tr');
      columns.forEach(column=>{
        const cell=node('td');const value=column.render(row);
        if(value instanceof Node)cell.appendChild(value);else cell.textContent=text(value);
        line.appendChild(cell);
      });
      if(onClick){line.tabIndex=0;line.addEventListener('click',()=>onClick(row));line.addEventListener('keydown',event=>{if(event.key==='Enter')onClick(row);});}
      body.appendChild(line);
    });
    const wrap=node('div','table-wrap');add(table,add(node('thead'),header),body);wrap.appendChild(table);panel.appendChild(wrap);return panel;
  }
  function primary(title,subtitle){return add(node('div','primary-cell'),node('strong','',title),node('small','',subtitle));}
  function renderSkills(skills){
    const columns=[
      {label:'Skill',render:item=>primary(item.skill_name,(item.skill_id||'无 ID')+' · '+item.normalized_skill_path)},
      {label:'仓库 / 提交人',render:item=>primary(item.repo_name,item.source_branch+' · '+personLabel(item))},
      {label:'Cisco',render:item=>badge(item.cisco_status)},
      {label:'SkillSpector',render:item=>badge(item.skillspector_status)},
      {label:'AI 审查',render:item=>badge(item.ai_status||item.reuse_status)},
      {label:'综合结论',render:item=>badge(item.security_decision)},
      {label:'质量',render:item=>text(item.quality_score)},
      {label:'问题',render:item=>(item.finding_count||0)+' 项'}
    ];
    $('#view-skills').replaceChildren(tablePanel('Skill 审查清单','点击行打开完整来源、流程和证据索引。',columns,skills,openSkill));
  }
  function renderFindings(skills){
    const rows=allFindings(skills).sort((a,b)=>(severityOrder[a.finding.severity]??6)-(severityOrder[b.finding.severity]??6)||a.skill.repo_name.localeCompare(b.skill.repo_name,'zh-CN'));
    const columns=[
      {label:'等级',render:row=>badge(row.finding.severity)},
      {label:'问题',render:row=>primary(row.finding.title,row.finding.finding_id||row.finding.source_rule_id||'无规则编号')},
      {label:'来源工具',render:row=>row.finding.source},
      {label:'Skill / 仓库',render:row=>primary(row.skill.skill_name,row.skill.repo_name)},
      {label:'文件定位',render:row=>primary(row.finding.path||'未定位',row.finding.start_line?'行 '+row.finding.start_line+(row.finding.end_line?'–'+row.finding.end_line:''):'未提供行号')},
      {label:'领域',render:row=>row.finding.domain}
    ];
    $('#view-findings').replaceChildren(tablePanel('清洗后问题清单','合并同内容问题并保留扫描器、规则、文件与证据引用。',columns,rows,row=>openFinding(row.skill,row.finding)));
  }
  function renderRepositories(skills){
    const groups=aggregate(skills,item=>item.repo_name,item=>item.repo_name);
    const columns=[
      {label:'仓库',render:group=>{const value=primary(group.label,group.people.size+' 提交人');value.appendChild(decisionBars(group));return value;}},
      {label:'Skill',render:group=>group.skills.length},{label:'问题',render:group=>group.findings.length},
      {label:'Critical / High',render:group=>group.findings.filter(f=>['CRITICAL','HIGH'].includes(f.severity)).length},
      {label:'安全通过',render:group=>group.skills.filter(s=>s.security_decision==='PASS').length},
      {label:'平均质量',render:group=>{const scored=group.skills.filter(s=>Number.isFinite(s.quality_score));return scored.length?Math.round(scored.reduce((sum,s)=>sum+s.quality_score,0)/scored.length):'—';}}
    ];
    $('#view-repositories').replaceChildren(tablePanel('仓库汇总','点击仓库可进入该仓库的单 Skill 清单。',columns,groups,group=>drillDown('repo',group.key)));
  }
  function renderSubmitters(skills){
    const groups=aggregate(skills,personKey,personLabel);
    const columns=[
      {label:'提交人',render:group=>{const parts=group.key.split('|');return primary(parts[0]||parts[1]||'未登记',parts[0]&&parts[1]?parts[1]:'未提供独立邮箱');}},
      {label:'仓库',render:group=>group.repos.size},{label:'Skill',render:group=>group.skills.length},
      {label:'问题',render:group=>group.findings.length},
      {label:'Critical / High',render:group=>group.findings.filter(f=>['CRITICAL','HIGH'].includes(f.severity)).length},
      {label:'安全通过',render:group=>group.skills.filter(s=>s.security_decision==='PASS').length}
    ];
    $('#view-submitters').replaceChildren(tablePanel('提交人汇总','user_name 与 user_email 共同作为汇总标签；点击后查看其 Skill。',columns,groups,group=>drillDown('person',group.key)));
  }
  function facts(items){
    const box=node('div','facts');
    items.forEach(([label,value,wide])=>{
      const fact=node('div','fact'+(wide?' wide':''));
      add(fact,node('span','',label),node('b',String(value||'').length>28?'mono':'',text(value)));
      box.appendChild(fact);
    });
    return box;
  }
  function trace(skill){
    const box=node('div','trace');
    [
      ['来源锁定',(skill.source_revision||skill.inventory_revision||'未冻结').slice(0,12)],
      ['Cisco',skill.cisco_status||'未完成'],['SkillSpector',skill.skillspector_status||'未完成'],
      ['AI 审查',skill.ai_status||skill.reuse_status||'未完成'],['综合判定',skill.security_decision||'未形成']
    ].forEach(([label,value])=>add(box,add(node('div','trace-step'),node('span','',label),node('b','',value))));
    return box;
  }
  function findingCard(skill,finding){
    const card=node('article','finding-card '+String(finding.severity||'').toLowerCase());
    const button=node('button');
    const head=add(node('div','finding-card-head'),badge(finding.severity),node('span','subline',finding.source),node('code','',finding.path+(finding.start_line?':'+finding.start_line:'')));
    add(button,head,node('h4','',finding.title),node('p','',finding.description||finding.evidence_summary||'未提供问题说明'));
    button.addEventListener('click',()=>openFinding(skill,finding));card.appendChild(button);return card;
  }
  function formatBytes(value){
    const bytes=Number(value)||0;if(bytes<1024)return bytes+' B';if(bytes<1048576)return(bytes/1024).toFixed(1)+' KB';return(bytes/1048576).toFixed(1)+' MB';
  }
  function evidenceSection(skill){
    const section=node('section','drawer-section');section.appendChild(node('h3','','证据索引'));
    section.appendChild(node('div','evidence-note','HTML 仅展示脱敏摘录和完整性索引；原始扫描器输出仍在受限证据区，按相对路径与 SHA-256 核验。'));
    const list=node('div','evidence-list');
    (skill.evidence_artifacts||[]).forEach(item=>{
      const row=node('div','evidence-item');
      add(row,node('strong','',item.label),node('span','',item.type),node('code','',item.path),node('span','',formatBytes(item.size_bytes)));
      const hash=node('code','raw-index','SHA-256 '+item.sha256);hash.style.gridColumn='1/-1';row.appendChild(hash);list.appendChild(row);
    });
    if(!list.children.length)list.appendChild(empty('没有可用证据索引','证据引用缺失、越界、损坏或尚未生成。'));
    section.appendChild(list);return section;
  }
  function openDrawer(eyebrow,title,subtitle,body){
    $('#drawer-eyebrow').textContent=eyebrow;$('#drawer-title').textContent=title;$('#drawer-subtitle').textContent=subtitle;
    $('#drawer-body').replaceChildren(...body);const backdrop=$('#drawer-backdrop');backdrop.hidden=false;
    requestAnimationFrame(()=>backdrop.classList.add('open'));document.body.style.overflow='hidden';$('#drawer-close').focus();
  }
  function closeDrawer(){
    const backdrop=$('#drawer-backdrop');backdrop.classList.remove('open');document.body.style.overflow='';
    setTimeout(()=>{if(!backdrop.classList.contains('open'))backdrop.hidden=true;},170);
  }
  function openSkill(skill){
    const process=node('section','drawer-section');add(process,node('h3','','审查证据链'),trace(skill));
    const origin=node('section','drawer-section');add(origin,node('h3','','来源与责任信息'),facts([
      ['Skill ID',skill.skill_id],['Skill Root',skill.skill_name],['仓库',skill.repo_name],['分支',skill.source_branch],
      ['产品线',skill.product_line],['提交人',personLabel(skill)],['Skill 路径',skill.normalized_skill_path,true],
      ['来源 Revision',skill.source_revision||skill.inventory_revision,true],['内容 SHA-256',skill.skill_digest,true],
      ['审查策略',skill.review_policy_version],['审查时间',skill.reviewed_at],
      ['结果复用',skill.reuse_status==='RESULT_REUSED'?((skill.reuse_reason||'是')+(skill.timestamp_ignored?'；忽略时间戳':'')):'否',true]
    ]));
    const result=node('section','drawer-section');add(result,node('h3','','综合结果'),facts([
      ['安全结论',skill.security_decision],['质量得分',Number.isFinite(skill.quality_score)?skill.quality_score+'/100':'未评分'],
      ['最高风险',skill.max_severity],['问题数量',skill.finding_count],['候选状态',skill.candidate_status],
      ['失败/人工说明',skill.failure_reason||skill.manual_reason||'无'],['证据目录',skill.evidence_ref||'不可用',true]
    ]));
    const findingSection=node('section','drawer-section');findingSection.appendChild(node('h3','','问题清单'));
    const stack=node('div','finding-stack');(skill.findings||[]).forEach(item=>stack.appendChild(findingCard(skill,item)));
    if(!stack.children.length)stack.appendChild(empty('未提取到问题','仍应结合扫描完成状态判断，不能据此单独认定安全。'));
    findingSection.appendChild(stack);
    openDrawer('SKILL DETAIL',skill.skill_name,skill.repo_name+' / '+skill.normalized_skill_path,[process,origin,result,findingSection,evidenceSection(skill)]);
  }
  function detailBlock(title,value,mono){
    const block=node('div','detail-block');add(block,node('h4','',title),node('p',mono?'mono':'',value));return block;
  }
  function openFinding(skill,finding){
    const card=node('section','finding-detail');
    const head=add(node('div','finding-detail-head'),badge(finding.severity),node('h3','',finding.title),node('p','',finding.source+' · '+(finding.finding_id||finding.source_rule_id||'未提供规则编号')));
    card.appendChild(head);add(card,
      detailBlock('来源定位',skill.repo_name+' / '+skill.source_branch+' / '+skill.normalized_skill_path+' / '+(finding.path||'未定位')+(finding.start_line?':'+finding.start_line+(finding.end_line?'–'+finding.end_line:''):'')),
      detailBlock('问题说明',finding.description||'未提供'),detailBlock('证据摘录',finding.evidence_summary||finding.description||'未提供'),
      detailBlock('处理建议',finding.recommendation||'未提供'),
      detailBlock('内容版本','Revision '+(skill.source_revision||skill.inventory_revision||'未记录')+'\nSHA-256 '+(skill.skill_digest||'未记录'),true),
      detailBlock('问题指纹',finding.fingerprint||'未提供',true)
    );
    if((finding.source_references||[]).length){
      const block=node('div','detail-block');block.appendChild(node('h4','','扫描来源引用'));
      const refs=node('div','source-ref-list');finding.source_references.forEach(ref=>refs.appendChild(node('div','source-ref',JSON.stringify(ref,null,2))));
      block.appendChild(refs);card.appendChild(block);
    }
    openDrawer('FINDING TRACE',finding.title,skill.skill_name+' · '+skill.repo_name,[card,evidenceSection(skill)]);
  }
  function drillDown(kind,value){
    if(kind==='repo'){$('#filter-repo').value=value;state.repo=value;}else{$('#filter-person').value=value;state.person=value;}
    setTab('skills');render();
  }
  function setTab(tab){
    state.tab=tab;document.querySelectorAll('.tab').forEach(button=>button.classList.toggle('active',button.dataset.tab===tab));
    document.querySelectorAll('.view').forEach(view=>view.hidden=view.id!=='view-'+tab);
    $('#view-title').textContent=descriptions[tab][0];$('#view-description').textContent=descriptions[tab][1];
  }
  function render(){
    const skills=visibleSkills();$('#scope-count').textContent=skills.length+' / '+allSkills.length;$('#visible-count').textContent=skills.length;
    if(state.tab==='overview')renderOverview(skills);
    else if(state.tab==='skills')renderSkills(skills);
    else if(state.tab==='findings')renderFindings(skills);
    else if(state.tab==='repositories')renderRepositories(skills);
    else renderSubmitters(skills);
  }
  function csvCell(value){
    let content=value===null||value===undefined?'':(typeof value==='object'?JSON.stringify(value):String(value));
    if(/^[=+\-@]/.test(content))content="'"+content;return'"'+content.replace(/"/g,'""')+'"';
  }
  function exportRows(){
    const skills=visibleSkills();
    if(state.tab==='findings')return allFindings(skills).map(({skill,finding})=>({
      skill_id:skill.skill_id,skill_name:skill.skill_name,repo_name:skill.repo_name,branch:skill.source_branch,
      skill_path:skill.normalized_skill_path,product_line:skill.product_line,user_name:skill.user_name,user_email:skill.user_email,
      source_revision:skill.source_revision,skill_digest:skill.skill_digest,finding_id:finding.finding_id,severity:finding.severity,
      source_scanner:finding.source,source_rule_id:finding.source_rule_id,domain:finding.domain,file_path:finding.path,
      start_line:finding.start_line,end_line:finding.end_line,title:finding.title,description:finding.description,
      evidence_summary:finding.evidence_summary,recommendation:finding.recommendation
    }));
    if(state.tab==='repositories')return aggregate(skills,item=>item.repo_name,item=>item.repo_name).map(group=>({
      repo_name:group.label,skill_count:group.skills.length,finding_count:group.findings.length,
      critical_high_count:group.findings.filter(f=>['CRITICAL','HIGH'].includes(f.severity)).length,
      submitter_count:group.people.size,pass_count:group.skills.filter(s=>s.security_decision==='PASS').length
    }));
    if(state.tab==='submitters')return aggregate(skills,personKey,personLabel).map(group=>({
      user_name:group.key.split('|')[0],user_email:group.key.split('|')[1],repository_count:group.repos.size,
      skill_count:group.skills.length,finding_count:group.findings.length,
      critical_high_count:group.findings.filter(f=>['CRITICAL','HIGH'].includes(f.severity)).length,
      pass_count:group.skills.filter(s=>s.security_decision==='PASS').length
    }));
    return skills.map(skill=>({
      skill_id:skill.skill_id,skill_name:skill.skill_name,repo_name:skill.repo_name,branch:skill.source_branch,
      skill_path:skill.normalized_skill_path,product_line:skill.product_line,user_name:skill.user_name,user_email:skill.user_email,
      source_revision:skill.source_revision,skill_digest:skill.skill_digest,cisco_status:skill.cisco_status,
      skillspector_status:skill.skillspector_status,ai_status:skill.ai_status,security_decision:skill.security_decision,
      quality_score:skill.quality_score,max_severity:skill.max_severity,finding_count:skill.finding_count,
      reuse_status:skill.reuse_status,evidence_ref:skill.evidence_ref
    }));
  }
  function download(name,content,type){
    const blob=new Blob([content],{type});const url=URL.createObjectURL(blob);const anchor=node('a');anchor.href=url;anchor.download=name;
    document.body.appendChild(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),500);
  }
  function exportCsv(){
    const rows=exportRows();if(!rows.length)return;const headers=[...new Set(rows.flatMap(row=>Object.keys(row)))];
    const content='\ufeff'+[headers.map(csvCell).join(','),...rows.map(row=>headers.map(key=>csvCell(row[key])).join(','))].join('\r\n');
    download('skill-review-'+report.metadata.batch_id+'-'+state.tab+'.csv',content,'text/csv;charset=utf-8');
  }
  function exportJson(){
    const skills=visibleSkills();const payload={schema_version:'1.0',batch_id:report.metadata.batch_id,exported_view:state.tab,
      filters:{search:state.search,repo_name:state.repo,product_line:state.product,submitter:state.person,security_decision:state.decision,severity:state.severity},
      skill_count:skills.length,finding_count:allFindings(skills).length,skills};
    download('skill-review-'+report.metadata.batch_id+'-filtered.json',JSON.stringify(payload,null,2),'application/json;charset=utf-8');
  }
  const bindings=[
    ['#filter-search','search','input',value=>value.trim().toLowerCase()],['#filter-repo','repo','change'],
    ['#filter-product','product','change'],['#filter-person','person','change'],['#filter-decision','decision','change'],
    ['#filter-severity','severity','change']
  ];
  bindings.forEach(([selector,key,event,transform])=>$(selector).addEventListener(event,eventObject=>{
    state[key]=transform?transform(eventObject.target.value):eventObject.target.value;render();
  }));
  document.querySelectorAll('.tab').forEach(button=>button.addEventListener('click',()=>{setTab(button.dataset.tab);render();}));
  $('#reset-filters').addEventListener('click',()=>{
    state.search=state.repo=state.product=state.person=state.decision=state.severity='';bindings.forEach(([selector])=>$(selector).value='');render();
  });
  $('#export-csv').addEventListener('click',exportCsv);$('#export-json').addEventListener('click',exportJson);
  $('#print-report').addEventListener('click',()=>window.print());$('#drawer-close').addEventListener('click',closeDrawer);
  $('#drawer-backdrop').addEventListener('click',event=>{if(event.target===$('#drawer-backdrop'))closeDrawer();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer();});render();
})();'''


def write_html_report(
    records: Iterable[Mapping[str, Any]],
    output: Path,
    *,
    batch_id: str,
    input_csv_sha256: str | None = None,
    policy_version: str | None = None,
    generated_at: str | None = None,
    candidate_threshold: int = 70,
    evidence_root: Path | None = None,
) -> Path:
    """Render the single-file, interactive and redacted audit workbench."""

    payload = build_html_report_payload(
        records,
        batch_id=batch_id,
        input_csv_sha256=input_csv_sha256,
        policy_version=policy_version,
        generated_at=generated_at,
        candidate_threshold=candidate_threshold,
        evidence_root=evidence_root,
    )
    page = (
        _PAGE.replace("__BATCH_ID__", _escape(batch_id))
        .replace("__POLICY_VERSION__", _escape(policy_version or "未记录"))
        .replace("__GENERATED_AT__", _escape(generated_at or "未记录"))
        .replace("__INPUT_SHA__", _escape(input_csv_sha256 or "未记录"))
        .replace("__APP_SCRIPT__", _APP_SCRIPT)
        .replace("__REPORT_DATA__", _script_safe_json(payload))
    )
    return _atomic_write(output, page)


__all__ = ["build_html_report_payload", "write_html_report"]
