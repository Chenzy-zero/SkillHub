"""Install authoritative overall-decision fields into legacy report projections.

The reporting module predates ``overall_decision`` and intentionally remains a
stable redacted exporter. This small compatibility boundary enriches its
normalized rows and live-report export field lists without changing evidence or
policy inputs.
"""

from __future__ import annotations

from typing import Any, Mapping

from .overall_decision import overall_fields


_OVERALL_EXPORT_FIELDS = (
    "overall_decision",
    "overall_decision_zh",
    "overall_reason_codes",
    "overall_reasons",
    "security_decision_zh",
    "quality_decision",
    "quality_decision_zh",
    "candidate_eligible",
)

_READY_CANDIDATE_STATUSES = {
    "READY_TO_EXPORT",
    "EXPORTED_LOCAL",
    "VERIFIED",
    "MANUAL_SYNC_PENDING",
    "MANUALLY_SYNCED",
}

_LEGACY_BANNER_FONT = "font:600 13px/1.5 Segoe UI,Microsoft YaHei UI,sans-serif"
_CJK_BANNER_FONT = (
    'font:600 13px/1.5 "Microsoft YaHei UI","Microsoft YaHei",'
    '"PingFang SC","Noto Sans CJK SC","Source Han Sans SC","Segoe UI",sans-serif'
)


def _candidate_eligible(record: Mapping[str, Any]) -> bool | None:
    value = record.get("candidate_eligible")
    if isinstance(value, bool):
        return value
    status = str(record.get("candidate_status") or "").strip().upper()
    if status in _READY_CANDIDATE_STATUSES:
        return True
    if status == "NOT_ELIGIBLE":
        return False
    return None


def _quality_decision(record: Mapping[str, Any]) -> Any:
    direct = record.get("quality_decision")
    if direct:
        return direct
    ai = record.get("ai_review")
    if isinstance(ai, Mapping):
        quality = ai.get("quality_review")
        if isinstance(quality, Mapping):
            return quality.get("verdict")
    return ""


def _final_status(record: Mapping[str, Any]) -> str:
    value = str(record.get("final_status") or "").strip().upper()
    if value:
        return value
    review = str(record.get("review_status") or "").strip().upper()
    if review == "COMPLETED":
        return "COMPLETED"
    if review == "INCOMPLETE":
        return "INCOMPLETE"
    return "PENDING"


def install_reporting_compat(reporting_module: Any, live_report_module: Any) -> None:
    """Patch report normalization/export lists once, preserving public APIs."""

    if getattr(reporting_module, "_overall_decision_compat_installed", False):
        return
    original_normalized = reporting_module._normalized_record
    original_annotate_html = live_report_module._annotate_html

    def normalized_record(record: Mapping[str, Any], *, batch_id: str) -> dict[str, Any]:
        row = dict(original_normalized(record, batch_id=batch_id))
        existing = str(record.get("overall_decision") or "").strip().upper()
        if existing:
            decision_fields = {
                field: record.get(field)
                for field in _OVERALL_EXPORT_FIELDS
                if field in record
            }
            # Old/new records may contain only part of the presentation fields;
            # deterministically fill any missing values from canonical inputs.
            computed = overall_fields(
                final_status=_final_status(record),
                security_decision=row.get("security_decision") or record.get("security_decision"),
                quality_decision=_quality_decision(record),
                candidate_eligible=_candidate_eligible(record),
            )
            computed.update({key: value for key, value in decision_fields.items() if value not in (None, "")})
        else:
            computed = overall_fields(
                final_status=_final_status(record),
                security_decision=row.get("security_decision") or record.get("security_decision"),
                quality_decision=_quality_decision(record),
                candidate_eligible=_candidate_eligible(record),
            )
        row.update(computed)
        return row

    def annotate_html(*args: Any, **kwargs: Any) -> None:
        original_annotate_html(*args, **kwargs)
        html_path = args[0] if args else kwargs.get("html_path")
        if html_path is None:
            return
        path = live_report_module.Path(html_path)
        html = path.read_text(encoding="utf-8")
        if _LEGACY_BANNER_FONT in html:
            html = html.replace(_LEGACY_BANNER_FONT, _CJK_BANNER_FONT, 1)
            live_report_module._atomic_text(path, html)

    reporting_module._normalized_record = normalized_record
    reporting_module.DETAIL_FIELDS = tuple(
        dict.fromkeys((*reporting_module.DETAIL_FIELDS, *_OVERALL_EXPORT_FIELDS))
    )

    live_report_module._PHASE_FIELDS = tuple(
        dict.fromkeys((*live_report_module._PHASE_FIELDS, *_OVERALL_EXPORT_FIELDS))
    )
    live_report_module._CURRENT_EXPORT_FIELDS = tuple(
        dict.fromkeys((*live_report_module._CURRENT_EXPORT_FIELDS, *_OVERALL_EXPORT_FIELDS))
    )
    live_report_module._JSON_EXPORT_FIELDS = tuple(
        dict.fromkeys((*live_report_module._JSON_EXPORT_FIELDS, *_OVERALL_EXPORT_FIELDS))
    )
    live_report_module._annotate_html = annotate_html
    reporting_module._overall_decision_compat_installed = True


__all__ = ["install_reporting_compat"]
