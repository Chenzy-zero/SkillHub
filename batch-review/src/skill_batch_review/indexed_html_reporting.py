"""Indexed HTML report adapter with score-driven automatic approval UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import html_reporting as _legacy
from .evidence_index_migration import safe_evidence_bundle_compat
from .risk_scoring import scoring_rules


_EXTRA_SCRIPT = r'''(() => {
  'use strict';
  const payload = JSON.parse(document.getElementById('report-data').textContent);
  const links = new Map();
  (payload.skills || []).forEach(skill => (skill.evidence_artifacts || []).forEach(item => {
    if (item && item.href && ['DERIVED','NORMALIZED'].includes(String(item.type || '').toUpperCase())) {
      links.set(String(item.path || ''), String(item.href));
    }
  }));
  function enhance() {
    document.querySelectorAll('.evidence-item').forEach(row => {
      const codes = [...row.querySelectorAll('code')];
      const pathCode = codes.find(item => !item.classList.contains('raw-index'));
      if (!pathCode || pathCode.dataset.navigationBound === '1') return;
      pathCode.dataset.navigationBound = '1';
      const href = links.get(pathCode.textContent || '');
      if (!href) return;
      const anchor = document.createElement('a');
      anchor.href = href;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      anchor.textContent = pathCode.textContent || '';
      anchor.style.fontFamily = 'var(--mono)';
      anchor.style.fontSize = '12px';
      anchor.style.overflowWrap = 'anywhere';
      anchor.title = '打开受控派生/规范化证据';
      pathCode.replaceWith(anchor);
    });
  }
  new MutationObserver(enhance).observe(document.body, {subtree:true, childList:true});
  enhance();
})();'''


_TYPOGRAPHY_CSS = r'''
:root{
  --sans:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Source Han Sans SC","Segoe UI",Arial,sans-serif;
}
body{font:15px/1.65 var(--sans);text-rendering:optimizeLegibility}
button,input,select{font-family:var(--sans)}
.batch-meta span,.eyebrow{font-size:11px}
.filter-group label,.button,.tab{font-weight:600}
.metric span,.metric small,.panel-head span,.data-head p,.data-head>span,.primary-cell small,.subline,.page-foot{font-size:12px}
.data-table th{font-size:12px;font-weight:600;letter-spacing:.02em}
.data-table td{font-size:13px;line-height:1.6}
.badge{font-size:12px;font-weight:700;padding:4px 9px}
.mono{font-size:12px}.trace-step span,.fact span{font-size:12px}.trace-step b,.fact b{font-size:13px}
.finding-card-head code,.detail-block h4,.source-ref,.evidence-item span,.evidence-item code,.raw-index{font-size:12px}
.finding-card p,.evidence-note{font-size:13px}.evidence-item strong{font-size:12px}
.scoring-rules{max-width:1440px;margin:18px auto 0;padding:0 24px}
.scoring-card{background:var(--panel,#fff);border:1px solid var(--line,#d9e0e7);border-radius:16px;padding:18px 20px;box-shadow:0 8px 24px rgba(0,0,0,.04)}
.scoring-card h2{margin:0 0 8px;font-size:18px}.scoring-card p{margin:6px 0;color:var(--muted,#5d6875)}
.scoring-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:12px}
.scoring-rule{border:1px solid var(--line,#e1e6eb);border-radius:12px;padding:11px 12px;background:rgba(127,127,127,.035)}
.scoring-rule b{display:block;margin-bottom:4px}.scoring-formula{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
@media print{body{font-size:11pt;line-height:1.55}.data-table th{font-size:9pt}.data-table td,.badge{font-size:9pt}}
'''


_SCORING_RULES_HTML = r'''
<section class="scoring-rules" id="security-scoring-rules">
  <div class="scoring-card">
    <h2>自动审批与安全扣分规则</h2>
    <p><b>安全评分从 100 分开始。</b> 每个安全风险按“严重度基础扣分 × 置信度系数 × 来源/交叉确认系数 × 重复告警衰减”计算。最终安全评分 ≥ 60 且质量评分 ≥ 70、审查完整且未触发 Hard Block 时自动通过。</p>
    <div class="scoring-grid">
      <div class="scoring-rule"><b>严重度基础扣分</b><span>INFO 0 / LOW 3 / MEDIUM 10 / HIGH 25 / CRITICAL 50</span></div>
      <div class="scoring-rule"><b>置信度系数</b><span>LOW ×0.25 / MEDIUM ×0.60 / HIGH ×1.00 / UNKNOWN ×0.50</span></div>
      <div class="scoring-rule"><b>来源权重</b><span>Cisco ×0.45 / SkillSpector ×0.55 / AI ×1.00；AI+静态 ×1.40；三方确认 ×1.70</span></div>
      <div class="scoring-rule"><b>重复告警衰减</b><span>同类第 1 条 ×1.00，第 2 条 ×0.50，第 3 条 ×0.25，第 4 条起 ×0.10</span></div>
      <div class="scoring-rule"><b>Hard Block</b><span>AI 明确 verdict=BLOCK，或 AI 高置信 CRITICAL，安全评分直接归零并自动不通过。</span></div>
      <div class="scoring-rule"><b>最终门禁</b><span class="scoring-formula">完整审查 AND 安全评分 ≥ 60 AND 质量评分 ≥ 70 AND 无 Hard Block</span></div>
    </div>
    <p>静态 Scanner 的 BLOCK / REVIEW_REQUIRED 仅作为风险信号，不再直接等价于最终拒绝；Cisco 关键词/模式类误报会因较低来源权重和重复衰减显著降低影响。</p>
  </div>
</section>
'''


def _replace_required(script: str, old: str, new: str, *, count: int = 1) -> str:
    if old not in script:
        raise RuntimeError(f"legacy HTML application contract changed: {old[:80]!r}")
    return script.replace(old, new, count)


def _overall_app_script() -> str:
    script = _legacy._APP_SCRIPT
    script = _replace_required(
        script,
        "repositories:['仓库视图','按仓库汇总 Skill、问题、提交人与综合结论。'],",
        "repositories:['仓库视图','按仓库汇总 Skill、问题、提交人与最终结论。'],",
    )
    script = _replace_required(
        script,
        "const badge = value => node('span','badge '+tone(value),text(value));",
        "const badge = value => node('span','badge '+tone(value),text(value));\n"
        "  const overallLabel = value => ({APPROVED:'通过',REJECTED:'不通过',INCOMPLETE:'审查未完成',PENDING:'审查中',MANUAL_REVIEW:'历史复核项'})[String(value||'').toUpperCase()]||text(value);\n"
        "  const overallBadge = skill => {const item=badge(skill.overall_decision);item.textContent=skill.overall_decision_zh||overallLabel(skill.overall_decision);return item;};\n"
        "  const deductionText = skill => (skill.security_deductions||[]).map(d=>`${d.category||'OTHER'} ${d.severity||''} / ${(d.sources||[]).join('+')||'UNKNOWN'}：${d.base_deduction}×${d.confidence_factor}×${d.source_factor}×${d.repetition_factor} = -${d.deduction}分${d.path?' @ '+d.path:''}`).join('；') || '无安全扣分';",
    )
    script = _replace_required(
        script,
        "skill.source_revision,skill.skill_digest,skill.security_decision,",
        "skill.source_revision,skill.skill_digest,skill.overall_decision,skill.overall_decision_zh,skill.security_score,skill.security_risk_level,skill.security_decision,skill.security_decision_zh,skill.quality_decision,skill.quality_decision_zh,",
    )
    script = _replace_required(script,"if(state.decision&&skill.security_decision!==state.decision)return false;","if(state.decision&&skill.overall_decision!==state.decision)return false;")
    script = _replace_required(script,"fillOptions('#filter-decision',allSkills.map(item=>item.security_decision));","fillOptions('#filter-decision',allSkills.map(item=>item.overall_decision),overallLabel);")
    script = _replace_required(
        script,
        "const decisions={PASS:0,REVIEW_REQUIRED:0,BLOCKED:0,INCOMPLETE:0};\n    skills.forEach(skill=>{if(Object.prototype.hasOwnProperty.call(decisions,skill.security_decision))decisions[skill.security_decision]++;});",
        "const decisions={APPROVED:0,REJECTED:0,INCOMPLETE:0,PENDING:0};\n    skills.forEach(skill=>{if(Object.prototype.hasOwnProperty.call(decisions,skill.overall_decision))decisions[skill.overall_decision]++;});",
    )
    script = _replace_required(
        script,
        "if(skill.security_decision==='PASS')values.pass++;\n      else if(skill.security_decision==='REVIEW_REQUIRED')values.review++;\n      else if(skill.security_decision==='BLOCKED')values.block++;\n      else values.incomplete++;",
        "if(skill.overall_decision==='APPROVED')values.pass++;\n      else if(skill.overall_decision==='REJECTED')values.block++;\n      else values.incomplete++;",
    )
    script = _replace_required(script,"metric('安全通过',current.decisions.PASS,'仅完整结论','ok'),","metric('最终通过',current.decisions.APPROVED,'自动审批结论','ok'),")
    script = _replace_required(script,"{label:'综合结论',render:item=>badge(item.security_decision)},","{label:'最终结论',render:item=>overallBadge(item)},")
    script = _replace_required(script,"{label:'安全通过',render:group=>group.skills.filter(s=>s.security_decision==='PASS').length},","{label:'最终通过',render:group=>group.skills.filter(s=>s.overall_decision==='APPROVED').length},",count=2)
    script = _replace_required(script,"['AI 审查',skill.ai_status||skill.reuse_status||'未完成'],['综合判定',skill.security_decision||'未形成']","['AI 审查',skill.ai_status||skill.reuse_status||'未完成'],['最终结论',skill.overall_decision_zh||overallLabel(skill.overall_decision)]")
    script = _replace_required(
        script,
        "['安全结论',skill.security_decision],['质量得分',Number.isFinite(skill.quality_score)?skill.quality_score+'/100':'未评分'],\n      ['最高风险',skill.max_severity],['问题数量',skill.finding_count],['候选状态',skill.candidate_status],\n      ['失败/人工说明',skill.failure_reason||skill.manual_reason||'无'],['证据目录',skill.evidence_ref||'不可用',true]",
        "['最终结论',skill.overall_decision_zh||overallLabel(skill.overall_decision)],['安全评分',Number.isFinite(skill.security_score)?skill.security_score+'/100':'未评分'],\n      ['风险扣分',Number.isFinite(skill.security_risk_deduction)?'-'+skill.security_risk_deduction+' 分':'未计算'],['风险等级',skill.security_risk_level||'未计算'],\n      ['Hard Block',skill.security_hard_block===true?'是':'否'],['质量得分',Number.isFinite(skill.quality_score)?skill.quality_score+'/100':'未评分'],\n      ['最高风险',skill.max_severity],['问题数量',skill.finding_count],['候选资格',skill.candidate_eligible===true?'可进入候选区':skill.candidate_eligible===false?'不可进入候选区':'待形成'],\n      ['扣分明细',deductionText(skill)],['判定原因',(skill.overall_reasons||[]).map(item=>item.label_zh||item.code).join('；')||skill.failure_reason||'无'],['证据目录',skill.evidence_ref||'不可用',true]",
    )
    script = _replace_required(script,"submitter_count:group.people.size,pass_count:group.skills.filter(s=>s.security_decision==='PASS').length","submitter_count:group.people.size,pass_count:group.skills.filter(s=>s.overall_decision==='APPROVED').length")
    script = _replace_required(script,"pass_count:group.skills.filter(s=>s.security_decision==='PASS').length","pass_count:group.skills.filter(s=>s.overall_decision==='APPROVED').length")
    script = _replace_required(
        script,
        "skillspector_status:skill.skillspector_status,ai_status:skill.ai_status,security_decision:skill.security_decision,\n      quality_score:skill.quality_score,max_severity:skill.max_severity,finding_count:skill.finding_count,",
        "skillspector_status:skill.skillspector_status,ai_status:skill.ai_status,overall_decision:skill.overall_decision,overall_decision_zh:skill.overall_decision_zh,\n      security_score:skill.security_score,security_risk_deduction:skill.security_risk_deduction,security_risk_level:skill.security_risk_level,security_hard_block:skill.security_hard_block,\n      security_scoring_rules_version:skill.security_scoring_rules_version,security_deductions:skill.security_deductions,security_decision:skill.security_decision,security_decision_zh:skill.security_decision_zh,quality_decision:skill.quality_decision,quality_decision_zh:skill.quality_decision_zh,\n      candidate_eligible:skill.candidate_eligible,overall_reason_codes:skill.overall_reason_codes,quality_score:skill.quality_score,max_severity:skill.max_severity,finding_count:skill.finding_count,",
    )
    return script


def _navigation_href(output: Path, evidence_root: Path, artifact: Mapping[str, Any]) -> str | None:
    if str(artifact.get("navigation") or "").upper() != "OPEN":
        return None
    relative = str(artifact.get("path") or "")
    if not relative:
        return None
    try:
        root = evidence_root.expanduser().resolve(strict=True)
        target = (root / Path(*relative.split("/"))).resolve(strict=True)
        target.relative_to(root)
        if target.is_symlink() or not target.is_file():
            return None
        link = os.path.relpath(target, start=output.parent.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return Path(link).as_posix()


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
    original_bundle = _legacy._safe_evidence_bundle
    source_records = [dict(item) for item in records]
    try:
        _legacy._safe_evidence_bundle = safe_evidence_bundle_compat
        payload = _legacy.build_html_report_payload(
            source_records,
            batch_id=batch_id,
            input_csv_sha256=input_csv_sha256,
            policy_version=policy_version,
            generated_at=generated_at,
            candidate_threshold=candidate_threshold,
            evidence_root=evidence_root,
        )
    finally:
        _legacy._safe_evidence_bundle = original_bundle

    by_row = {str(item.get("source_row_id") or ""): item for item in source_records}
    score_fields = (
        "security_score","security_risk_deduction","security_risk_level","security_hard_block",
        "security_scoring_rules_version","security_deductions","security_score_status",
    )
    for skill in payload.get("skills", []):
        if not isinstance(skill, dict):
            continue
        source = by_row.get(str(skill.get("source_row_id") or ""), {})
        for field in score_fields:
            if field in source:
                skill[field] = source[field]
    payload["security_scoring_rules"] = scoring_rules()

    output = output.expanduser().resolve()
    if evidence_root is not None:
        for skill in payload.get("skills", []):
            if not isinstance(skill, dict):
                continue
            for artifact in skill.get("evidence_artifacts", []):
                if not isinstance(artifact, dict):
                    continue
                href = _navigation_href(output, evidence_root, artifact)
                if href:
                    artifact["href"] = href

    page_template = _legacy._PAGE.replace('<label for="filter-decision">安全结论</label>','<label for="filter-decision">最终结论</label>',1)
    if "<body>" not in page_template:
        raise RuntimeError("legacy HTML page contract changed: missing <body>")
    page_template = page_template.replace("<body>", "<body>\n" + _SCORING_RULES_HTML, 1)
    page = (
        page_template.replace("</style>", _TYPOGRAPHY_CSS + "\n</style>", 1)
        .replace("__BATCH_ID__", _legacy._escape(batch_id))
        .replace("__POLICY_VERSION__", _legacy._escape(policy_version or "未记录"))
        .replace("__GENERATED_AT__", _legacy._escape(generated_at or "未记录"))
        .replace("__INPUT_SHA__", _legacy._escape(input_csv_sha256 or "未记录"))
        .replace("__APP_SCRIPT__", _overall_app_script() + "\n" + _EXTRA_SCRIPT)
        .replace("__REPORT_DATA__", _legacy._script_safe_json(payload))
    )
    return _legacy._atomic_write(output, page)


__all__ = ["write_html_report"]
