"""Indexed HTML report adapter.

This adapter reuses the existing self-contained report UI while replacing its
historical evidence hashing pass with the persistent evidence index. Only
DERIVED/NORMALIZED evidence receives a relative file link; RAW/SOURCE remains
path-only in the offline report. Evidence created before indexes existed is
bootstrapped once, then all later refreshes use the persisted index.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import html_reporting as _legacy
from .evidence_index_migration import safe_evidence_bundle_compat


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
      anchor.style.fontSize = '10px';
      anchor.style.overflowWrap = 'anywhere';
      anchor.title = '打开受控派生/规范化证据';
      pathCode.replaceWith(anchor);
    });
  }
  new MutationObserver(enhance).observe(document.body, {subtree:true, childList:true});
  enhance();
})();'''


def _navigation_href(
    output: Path,
    evidence_root: Path,
    artifact: Mapping[str, Any],
) -> str | None:
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
    """Render the existing offline workbench from persistent evidence indexes."""

    original_bundle = _legacy._safe_evidence_bundle
    try:
        _legacy._safe_evidence_bundle = safe_evidence_bundle_compat
        payload = _legacy.build_html_report_payload(
            records,
            batch_id=batch_id,
            input_csv_sha256=input_csv_sha256,
            policy_version=policy_version,
            generated_at=generated_at,
            candidate_threshold=candidate_threshold,
            evidence_root=evidence_root,
        )
    finally:
        _legacy._safe_evidence_bundle = original_bundle

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

    page = (
        _legacy._PAGE.replace("__BATCH_ID__", _legacy._escape(batch_id))
        .replace("__POLICY_VERSION__", _legacy._escape(policy_version or "未记录"))
        .replace("__GENERATED_AT__", _legacy._escape(generated_at or "未记录"))
        .replace("__INPUT_SHA__", _legacy._escape(input_csv_sha256 or "未记录"))
        .replace("__APP_SCRIPT__", _legacy._APP_SCRIPT + "\n" + _EXTRA_SCRIPT)
        .replace("__REPORT_DATA__", _legacy._script_safe_json(payload))
    )
    return _legacy._atomic_write(output, page)


__all__ = ["write_html_report"]
