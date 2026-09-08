"""Chinese-first bilingual presentation for localized report fields.

This layer changes only the self-contained HTML presentation. Canonical English
fields remain embedded in report-data and localized ``*_zh`` fields are optional;
missing translations automatically fall back to the original text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_BILINGUAL_CSS = r'''
.original-text{margin-top:10px;border-top:1px dashed var(--line);padding-top:8px;color:var(--muted);font-size:12px}
.original-text summary{cursor:pointer;color:var(--blue);font-weight:600;user-select:none}
.original-text pre{margin:8px 0 0;padding:10px 12px;background:var(--paper2);border:1px solid #e5eaf0;border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 var(--sans);color:var(--ink2)}
'''


def _replace_required(text: str, old: str, new: str, *, count: int = 1) -> str:
    if old not in text:
        raise RuntimeError(f"localized HTML contract changed: {old[:100]!r}")
    return text.replace(old, new, count)


def _adapt_page(page: str) -> str:
    page = page.replace("</style>", _BILINGUAL_CSS + "\n</style>", 1)
    page = _replace_required(
        page,
        "const allFindings = skills => skills.flatMap(skill=>(skill.findings||[]).map(finding=>({skill,finding})));",
        "const allFindings = skills => skills.flatMap(skill=>(skill.findings||[]).map(finding=>({skill,finding})));\n"
        "  const hasLocalizedFinding = finding => ['title','description','evidence_summary','recommendation'].some(field=>String(finding[field+'_zh']||'').trim());\n"
        "  const localizedFindingText = (finding,field,fallback='') => finding[field+'_zh']||finding[field]||fallback;\n"
        "  const originalFindingText = finding => [['标题',finding.title],['问题说明',finding.description],['证据摘录',finding.evidence_summary],['处理建议',finding.recommendation]].filter(item=>item[1]).map(item=>item[0]+': '+item[1]).join('\\n\\n');\n"
        "  const appendOriginalFinding = (parent,finding) => {if(!hasLocalizedFinding(finding))return;const details=node('details','original-text');const summary=node('summary','','查看英文原文');const original=node('pre','',originalFindingText(finding));add(details,summary,original);parent.appendChild(details);};",
    )
    page = _replace_required(
        page,
        "item.title,item.description,item.evidence_summary,item.path,item.source,item.source_rule_id",
        "item.title,item.title_zh,item.description,item.description_zh,item.evidence_summary,item.evidence_summary_zh,item.recommendation,item.recommendation_zh,item.path,item.source,item.source_rule_id",
    )
    page = _replace_required(
        page,
        "{label:'问题',render:row=>primary(row.finding.title,row.finding.finding_id||row.finding.source_rule_id||'无规则编号')},",
        "{label:'问题',render:row=>primary(row.finding.title_zh||row.finding.title,row.finding.finding_id||row.finding.source_rule_id||'无规则编号')},",
    )
    page = _replace_required(
        page,
        "add(button,head,node('h4','',finding.title),node('p','',finding.description||finding.evidence_summary||'未提供问题说明'));",
        "add(button,head,node('h4','',localizedFindingText(finding,'title','未命名问题')),node('p','',localizedFindingText(finding,'description',localizedFindingText(finding,'evidence_summary','未提供问题说明'))));",
    )
    page = _replace_required(
        page,
        "button.addEventListener('click',()=>openFinding(skill,finding));card.appendChild(button);return card;",
        "button.addEventListener('click',()=>openFinding(skill,finding));card.appendChild(button);appendOriginalFinding(card,finding);return card;",
    )
    page = _replace_required(
        page,
        "const head=add(node('div','finding-detail-head'),badge(finding.severity),node('h3','',finding.title),node('p','',finding.source+' · '+(finding.finding_id||finding.source_rule_id||'未提供规则编号')));",
        "const head=add(node('div','finding-detail-head'),badge(finding.severity),node('h3','',localizedFindingText(finding,'title','未命名问题')),node('p','',finding.source+' · '+(finding.finding_id||finding.source_rule_id||'未提供规则编号')));",
    )
    page = _replace_required(
        page,
        "detailBlock('问题说明',finding.description||'未提供'),detailBlock('证据摘录',finding.evidence_summary||finding.description||'未提供'),\n      detailBlock('处理建议',finding.recommendation||'未提供'),",
        "detailBlock('问题说明',localizedFindingText(finding,'description','未提供')),detailBlock('证据摘录',localizedFindingText(finding,'evidence_summary',localizedFindingText(finding,'description','未提供'))),\n      detailBlock('处理建议',localizedFindingText(finding,'recommendation','未提供')),
",
    )
    page = _replace_required(
        page,
        "if((finding.source_references||[]).length){",
        "if(hasLocalizedFinding(finding))card.appendChild(detailBlock('英文原文',originalFindingText(finding)));\n    if((finding.source_references||[]).length){",
    )
    page = _replace_required(
        page,
        "openDrawer('FINDING TRACE',finding.title,skill.skill_name+' · '+skill.repo_name,[card,evidenceSection(skill)]);",
        "openDrawer('FINDING TRACE',localizedFindingText(finding,'title','未命名问题'),skill.skill_name+' · '+skill.repo_name,[card,evidenceSection(skill)]);",
    )
    page = _replace_required(
        page,
        "start_line:finding.start_line,end_line:finding.end_line,title:finding.title,description:finding.description,\n      evidence_summary:finding.evidence_summary,recommendation:finding.recommendation",
        "start_line:finding.start_line,end_line:finding.end_line,title:finding.title,title_zh:finding.title_zh,description:finding.description,description_zh:finding.description_zh,\n      evidence_summary:finding.evidence_summary,evidence_summary_zh:finding.evidence_summary_zh,recommendation:finding.recommendation,recommendation_zh:finding.recommendation_zh",
    )
    return page


def install_bilingual_html_reporting(html_reporting_module: Any) -> None:
    if getattr(html_reporting_module, "_bilingual_html_installed", False):
        return
    original_writer = html_reporting_module.write_html_report

    def write_html_report(*args: Any, **kwargs: Any) -> Path:
        output = Path(original_writer(*args, **kwargs))
        page = output.read_text(encoding="utf-8")
        adapted = _adapt_page(page)
        return html_reporting_module._atomic_write(output, adapted)

    html_reporting_module.write_html_report = write_html_report
    html_reporting_module._bilingual_html_installed = True


__all__ = ["install_bilingual_html_reporting"]
