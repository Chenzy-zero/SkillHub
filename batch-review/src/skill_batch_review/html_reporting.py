"""Self-contained, redacted HTML report for a completed review batch."""

from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .reporting import build_batch_summary, build_detail_rows, redact


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4, "NONE": 5, "": 6}


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _status_class(value: Any) -> str:
    text = str(value or "").upper()
    if text in {"PASS", "COMPLETED", "APPROVED", "EXPORTED_LOCAL", "READY_TO_EXPORT", "NONE"}:
        return "ok"
    if text in {"BLOCKED", "BLOCK", "REJECT", "FAILED", "ERROR", "CRITICAL", "HIGH"}:
        return "bad"
    if text in {"REVIEW_REQUIRED", "MANUAL_REVIEW", "MEDIUM", "INCOMPLETE", "TIMEOUT"}:
        return "warn"
    return "neutral"


def _safe_evidence_document(reference: Any, evidence_root: Path | None) -> Mapping[str, Any] | None:
    if evidence_root is None or not reference:
        return None
    root = evidence_root.expanduser().resolve()
    target = Path(str(reference)).expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if target.is_symlink() or not target.is_dir():
        return None
    result = target / "final-result.json"
    if result.is_symlink() or not result.is_file():
        return None
    try:
        value = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return redact(value) if isinstance(value, Mapping) else None


def _finding_values(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _extract_findings(document: Mapping[str, Any] | None) -> list[dict[str, str]]:
    if not document:
        return []
    collected: list[tuple[str, Mapping[str, Any]]] = []
    policy_findings = _finding_values(document.get("findings"))
    if policy_findings:
        collected.extend(("综合结论", item) for item in policy_findings)
    else:
        for report in _finding_values(document.get("static_reports")):
            scanner = str(report.get("scanner") or "静态扫描")
            collected.extend((scanner, item) for item in _finding_values(report.get("findings")))
        ai = document.get("ai_review")
        if isinstance(ai, Mapping):
            for section_name, label in (("security_review", "AI 安全审查"), ("quality_review", "AI 质量审查")):
                section = ai.get(section_name)
                if isinstance(section, Mapping):
                    collected.extend((label, item) for item in _finding_values(section.get("findings")))
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for source, item in collected:
        location = item.get("location") if isinstance(item.get("location"), Mapping) else {}
        path = item.get("file_path") or item.get("path") or location.get("path") or ""
        line = item.get("line_number") or item.get("line") or location.get("line") or ""
        title = item.get("title") or item.get("rule_id") or item.get("category") or "未命名问题"
        description = item.get("description") or item.get("message") or item.get("evidence") or ""
        severity = str(item.get("severity") or "INFO").upper()
        remediation = item.get("remediation") or item.get("recommendation") or ""
        domain = str(item.get("domain") or item.get("category") or "SECURITY").upper()
        key = tuple(str(value) for value in (source, severity, domain, path, line, title, description))
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {
                "source": str(source),
                "severity": severity,
                "domain": domain,
                "path": str(path),
                "line": str(line),
                "title": str(title),
                "description": str(description)[:1200],
                "remediation": str(remediation)[:800],
            }
        )
    return sorted(
        findings,
        key=lambda item: (_SEVERITY_ORDER.get(item["severity"], 6), item["path"], item["title"]),
    )


def _badge(value: Any) -> str:
    text = str(value or "未产生")
    return f'<span class="badge {_status_class(text)}">{_escape(text)}</span>'


def _metric(label: str, value: Any, note: str) -> str:
    return (
        '<article class="metric">'
        f'<span>{_escape(label)}</span><strong>{_escape(value)}</strong><small>{_escape(note)}</small>'
        "</article>"
    )


def _atomic_write(path: Path, content: str) -> Path:
    path = path.expanduser().resolve()
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
    """Render a single-file report without embedding raw evidence or secrets."""

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
    findings_by_id: dict[str, list[dict[str, str]]] = {}
    total_findings = 0
    severity_counts = {name: 0 for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}
    for row in details:
        document = _safe_evidence_document(row.get("evidence_ref"), evidence_root)
        findings = _extract_findings(document)
        findings_by_id[str(row["source_row_id"])] = findings
        total_findings += len(findings)
        for finding in findings:
            if finding["severity"] in severity_counts:
                severity_counts[finding["severity"]] += 1

    rows_html: list[str] = []
    skill_sections: list[str] = []
    for index, row in enumerate(details, 1):
        findings = findings_by_id.get(str(row["source_row_id"]), [])
        max_severity = next((name for name in severity_counts if any(item["severity"] == name for item in findings)), "NONE")
        searchable = " ".join(
            str(row.get(key) or "")
            for key in ("skill_name", "repo_name", "normalized_skill_path", "security_decision")
        ).lower()
        rows_html.append(
            f'<tr data-search="{_escape(searchable)}" data-severity="{_escape(max_severity)}">'
            f'<td><strong>{_escape(row["skill_name"])}</strong><small>{_escape(row["normalized_skill_path"])}</small></td>'
            f'<td>{_escape(row["repo_name"])}<small>{_escape(row["source_branch"])}</small></td>'
            f'<td>{_badge(row["cisco_status"])}<small>{_escape(row["cisco_max_severity"] or "—")}</small></td>'
            f'<td>{_badge(row["skillspector_status"])}<small>{_escape(row["skillspector_max_severity"] or "—")}</small></td>'
            f'<td>{_badge(row["ai_status"])}</td>'
            f'<td>{_badge(row["security_decision"])}<small>质量 {row["quality_score"] if row["quality_score"] is not None else "—"}</small></td>'
            f'<td><a href="#skill-{index}">{len(findings)} 项</a></td></tr>'
        )
        finding_html = []
        for finding in findings:
            location = finding["path"] + (f':{finding["line"]}' if finding["line"] else "")
            finding_html.append(
                f'<article class="finding severity-{_escape(finding["severity"].lower())}">'
                f'<header>{_badge(finding["severity"])}<span>{_escape(finding["source"])}</span>'
                f'<code>{_escape(location or "未定位")}</code></header>'
                f'<h4>{_escape(finding["title"])}</h4><p>{_escape(finding["description"])}</p>'
                + (f'<aside><strong>处理建议</strong>{_escape(finding["remediation"])}</aside>' if finding["remediation"] else "")
                + "</article>"
            )
        if not finding_html:
            finding_html.append('<div class="empty">未从受限证据中提取到问题。若某个检查未完成，请以状态为准，不能视为安全通过。</div>')
        revision = str(row["source_revision"] or row["inventory_revision"])
        digest = str(row["skill_digest"] or "")
        skill_sections.append(
            f'<section class="skill" id="skill-{index}"><div class="skill-head"><div><span class="eyebrow">SKILL {index:02d}</span>'
            f'<h3>{_escape(row["skill_name"])}</h3><p>{_escape(row["repo_name"])} / {_escape(row["normalized_skill_path"])}</p></div>'
            f'<div class="decision">{_badge(row["security_decision"])}<strong>{row["quality_score"] if row["quality_score"] is not None else "—"}<small>/100</small></strong></div></div>'
            '<div class="rail">'
            f'<div><span>来源版本</span><b>{_escape(revision[:12] or "未冻结")}</b></div>'
            f'<div><span>Cisco</span>{_badge(row["cisco_status"])}</div>'
            f'<div><span>SkillSpector</span>{_badge(row["skillspector_status"])}</div>'
            f'<div><span>AI 审查</span>{_badge(row["ai_status"])}</div>'
            f'<div><span>准入结论</span>{_badge(row["candidate_status"])}</div></div>'
            f'<dl><div><dt>内容摘要</dt><dd><code>{_escape(digest or "未生成")}</code></dd></div>'
            f'<div><dt>策略版本</dt><dd>{_escape(row["review_policy_version"] or policy_version or "未记录")}</dd></div>'
            f'<div><dt>人工说明</dt><dd>{_escape(row["manual_reason"] or row["failure_reason"] or "无")}</dd></div></dl>'
            f'<div class="finding-list">{"".join(finding_html)}</div></section>'
        )

    critical_high = severity_counts["CRITICAL"] + severity_counts["HIGH"]
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skill 安全审查报告 · {_escape(batch_id)}</title>
<style>
:root{{--ink:#13233a;--muted:#65728a;--line:#dfe5ee;--paper:#fff;--canvas:#f3f6fa;--blue:#2458c6;--blue2:#e8efff;--green:#18794e;--green2:#e8f6ef;--amber:#9a5b00;--amber2:#fff4d6;--red:#b42318;--red2:#fdecea;--shadow:0 12px 32px rgba(25,46,80,.08)}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.65 "PingFang SC","Microsoft YaHei",Inter,Arial,sans-serif}}a{{color:var(--blue);text-decoration:none}}code{{font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}}.wrap{{width:min(1240px,calc(100% - 40px));margin:auto}}.hero{{background:linear-gradient(110deg,#112540 0%,#173867 72%,#2458c6 100%);color:white;padding:56px 0 44px;border-bottom:5px solid #75a2ff}}.hero-grid{{display:grid;grid-template-columns:1.5fr .8fr;gap:48px;align-items:end}}.kicker,.eyebrow{{font-size:11px;letter-spacing:.16em;font-weight:800;text-transform:uppercase}}h1{{font-size:clamp(30px,4vw,52px);line-height:1.12;margin:8px 0 14px}}.hero p{{max-width:760px;color:#dce7fb;font-size:16px}}.meta{{border-left:1px solid #ffffff42;padding-left:24px}}.meta div{{margin:8px 0}}.meta span{{display:block;color:#acc4eb;font-size:11px}}main{{padding:30px 0 60px}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:-50px;position:relative}}.metric{{background:var(--paper);border:1px solid #ffffff;border-radius:10px;padding:18px;box-shadow:var(--shadow)}}.metric span,.metric small{{display:block;color:var(--muted)}}.metric strong{{display:block;font-size:30px;line-height:1.25;margin:5px 0}}.panel,.skill{{background:var(--paper);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);margin-top:22px}}.panel-head,.skill-head{{display:flex;justify-content:space-between;gap:20px;align-items:end;padding:22px 24px;border-bottom:1px solid var(--line)}}h2,h3,h4,p{{margin-top:0}}h2{{margin-bottom:4px}}h3{{font-size:24px;margin:3px 0}}.panel-head p,.skill-head p{{margin:0;color:var(--muted)}}.controls{{display:flex;gap:10px;flex-wrap:wrap}}input,select{{border:1px solid #cbd4e1;background:white;border-radius:7px;padding:9px 11px;color:var(--ink)}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:980px}}th,td{{text-align:left;padding:14px 16px;border-bottom:1px solid #edf0f5;vertical-align:top}}th{{font-size:11px;color:var(--muted);letter-spacing:.06em;background:#f8fafd;position:sticky;top:0}}td small,td strong{{display:block}}td small{{color:var(--muted);margin-top:3px}}.badge{{display:inline-flex;align-items:center;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap}}.badge.ok{{color:var(--green);background:var(--green2)}}.badge.bad{{color:var(--red);background:var(--red2)}}.badge.warn{{color:var(--amber);background:var(--amber2)}}.badge.neutral{{color:#526177;background:#edf1f6}}.skill{{scroll-margin-top:16px}}.decision{{display:flex;gap:18px;align-items:center}}.decision>strong{{font-size:34px}}.decision small{{font-size:12px;color:var(--muted)}}.rail{{display:grid;grid-template-columns:repeat(5,1fr);padding:18px 24px;background:#f8fafd;border-bottom:1px solid var(--line)}}.rail>div{{position:relative;padding-right:18px}}.rail>div:not(:last-child)::after{{content:"";position:absolute;right:8px;top:16px;width:1px;height:34px;background:#ccd6e5}}.rail span{{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}}dl{{padding:4px 24px;margin:0}}dl>div{{display:grid;grid-template-columns:120px 1fr;padding:11px 0;border-bottom:1px solid #edf0f5}}dt{{color:var(--muted)}}dd{{margin:0}}.finding-list{{padding:18px 24px 24px;display:grid;gap:12px}}.finding{{border:1px solid var(--line);border-left:4px solid #8794a8;border-radius:8px;padding:15px 17px}}.finding.severity-critical,.finding.severity-high{{border-left-color:var(--red)}}.finding.severity-medium{{border-left-color:#e18a00}}.finding.severity-low{{border-left-color:var(--blue)}}.finding header{{display:flex;gap:10px;align-items:center;color:var(--muted)}}.finding header code{{margin-left:auto}}.finding h4{{margin:10px 0 4px;font-size:16px}}.finding p{{margin-bottom:0}}.finding aside{{background:#f6f8fb;margin-top:10px;padding:9px 11px;border-radius:6px}}.finding aside strong{{margin-right:10px}}.empty{{border:1px dashed #cbd4e1;color:var(--muted);padding:18px;border-radius:8px}}.foot{{color:var(--muted);padding:22px 0 0;font-size:12px}}@media(max-width:900px){{.hero-grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(2,1fr);margin-top:-28px}}.rail{{grid-template-columns:1fr 1fr;gap:12px}}.rail>div::after{{display:none}}}}@media(max-width:600px){{.wrap{{width:min(100% - 22px,1240px)}}.metrics{{grid-template-columns:1fr}}.panel-head,.skill-head{{align-items:flex-start;flex-direction:column}}dl>div{{grid-template-columns:1fr}}}}@media print{{body{{background:white}}.hero{{padding:24px 0}}.controls{{display:none}}.panel,.skill,.metric{{box-shadow:none;break-inside:avoid}}.metrics{{margin-top:16px}}}}
</style></head><body>
<header class="hero"><div class="wrap hero-grid"><div><span class="kicker">Skill Security Review / Evidence Report</span><h1>Skill 安全审查报告</h1><p>以固定 Git 版本为来源，对完整 Skill Package 汇总静态扫描、AI 审查、质量评分与候选准入结论。没有发现问题不等于不存在风险。</p></div><div class="meta"><div><span>批次号</span>{_escape(batch_id)}</div><div><span>策略版本</span>{_escape(policy_version or "未记录")}</div><div><span>生成时间</span>{_escape(generated_at or "未记录")}</div><div><span>CSV SHA-256</span><code>{_escape(input_csv_sha256 or "未记录")}</code></div></div></div></header>
<main class="wrap"><section class="metrics">{_metric("Skill 清单", summary["result_record_count"], f'{summary["repository_count"]} 个仓库')}{_metric("扫描问题", total_findings, "已脱敏汇总")}{_metric("高危问题", critical_high, "Critical + High")}{_metric("安全通过", summary["security_decision_counts"].get("PASS",0), "不含未完成")}{_metric("候选归档", summary["candidate_count"], f'质量门槛 {candidate_threshold}')}</section>
<section class="panel"><div class="panel-head"><div><h2>Skill 清单与结论</h2><p>按名称或路径筛选；状态为空表示该环节没有产生有效结果。</p></div><div class="controls"><input id="search" type="search" placeholder="搜索 Skill / 仓库 / 路径"><select id="severity"><option value="">全部风险</option><option>CRITICAL</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option><option>INFO</option><option>NONE</option></select></div></div><div class="table-wrap"><table><thead><tr><th>Skill</th><th>来源</th><th>Cisco</th><th>SkillSpector</th><th>AI 审查</th><th>综合结论</th><th>问题</th></tr></thead><tbody id="inventory">{"".join(rows_html)}</tbody></table></div></section>
<section class="panel"><div class="panel-head"><div><h2>风险分布</h2><p>Critical {severity_counts["CRITICAL"]} · High {severity_counts["HIGH"]} · Medium {severity_counts["MEDIUM"]} · Low {severity_counts["LOW"]} · Info {severity_counts["INFO"]}</p></div></div></section>
{"".join(skill_sections)}<p class="foot">本报告仅包含脱敏后的派生信息。原始扫描器输出、AI 完整结果和包清单保存在受限证据区；报告本身不构成运行安全保证或公开发布许可。</p></main>
<script>const q=document.querySelector('#search'),s=document.querySelector('#severity'),rows=[...document.querySelectorAll('#inventory tr')];function filter(){{const text=q.value.trim().toLowerCase(),sev=s.value;for(const row of rows)row.hidden=!!((text&&!row.dataset.search.includes(text))||(sev&&row.dataset.severity!==sev));}}q.addEventListener('input',filter);s.addEventListener('change',filter);</script></body></html>"""
    return _atomic_write(output, page)


__all__ = ["write_html_report"]
