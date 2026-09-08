"""Install report-safe localization into the live batch projection.

The canonical review state remains untouched. This adapter reads Translation
Memory from the batch results directory, projects only redacted text into the
report, and materializes the next pending translation-unit set after every
static/AI report refresh.
"""

from __future__ import annotations

from typing import Any, Mapping

from .localization import (
    DEFAULT_LOCALE,
    LocalizationError,
    build_localized_projection,
    empty_translation_memory,
    load_translation_memory,
    localization_pending_path,
    translation_memory_path,
    write_pending_translation_units,
)


_LOCALIZATION_EXPORT_FIELDS = (
    "localization_locale",
    "localization_status",
    "localization_total",
    "localization_translated",
    "failure_reason_zh",
    "manual_reason_zh",
    "reuse_reason_zh",
)
_FINDING_LOCALIZED_FIELDS = (
    "title_zh",
    "description_zh",
    "evidence_summary_zh",
    "recommendation_zh",
)


def install_localization_reporting(live_report_module: Any) -> None:
    if getattr(live_report_module, "_localization_projection_installed", False):
        return

    canonical_build_live_records = live_report_module.build_live_records
    original_html_finding = live_report_module._html_finding

    def html_finding(
        item: Mapping[str, Any], *, row_id: str, index: int
    ) -> dict[str, Any]:
        result = dict(original_html_finding(item, row_id=row_id, index=index))
        for field in _FINDING_LOCALIZED_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                result[field] = value
        return result

    def write_live_batch_report(config: Any, inventory: Any, *, batch_id: str) -> Any:
        canonical_records = canonical_build_live_records(config, inventory)
        memory_path = translation_memory_path(
            config.workspace.results_root,
            batch_id,
            locale=DEFAULT_LOCALE,
        )
        memory_status = "READY"
        memory_error = ""
        try:
            memory = load_translation_memory(memory_path, locale=DEFAULT_LOCALE)
            if not memory_path.exists():
                memory_status = "MISSING"
        except LocalizationError as exc:
            # A malformed optional localization artifact must never block or
            # overwrite the canonical report. Keep it in place for diagnosis.
            memory = empty_translation_memory(DEFAULT_LOCALE)
            memory_status = "INVALID"
            memory_error = str(exc)

        records = build_localized_projection(
            canonical_records,
            memory,
            locale=DEFAULT_LOCALE,
        )
        progress = live_report_module.phase_progress(records)
        status = live_report_module.report_status(progress)
        output_root = config.workspace.results_root / batch_id
        paths = live_report_module.write_batch_reports(
            records,
            output_root,
            batch_id=batch_id,
            input_csv_sha256=inventory.raw_csv_sha256,
            policy_version=config.ai.policy_version,
            candidate_threshold=config.quality.candidate_threshold,
            evidence_root=config.workspace.evidence_root,
        )
        summary = live_report_module._load_json(paths.summary) or {}
        live_report_module._atomic_json(
            paths.summary,
            {
                **dict(summary),
                "report_status": status,
                "phase_progress": dict(progress),
                "localization_locale": DEFAULT_LOCALE,
                "localization_memory_status": memory_status,
            },
        )
        current_json, current_csv = live_report_module._write_current_exports(
            output_root,
            batch_id=batch_id,
            records=records,
            status=status,
            progress=progress,
            evidence_root=config.workspace.evidence_root,
        )
        live_report_module._annotate_html(
            paths.html,
            records=records,
            status=status,
            progress=progress,
        )
        write_pending_translation_units(
            localization_pending_path(
                config.workspace.results_root,
                batch_id,
                locale=DEFAULT_LOCALE,
            ),
            canonical_records,
            memory,
            locale=DEFAULT_LOCALE,
            memory_status=memory_status,
            memory_error=memory_error,
        )
        return live_report_module.LiveReportResult(
            paths,
            current_json,
            current_csv,
            status,
            progress,
        )

    live_report_module._html_finding = html_finding
    live_report_module._CURRENT_EXPORT_FIELDS = tuple(
        dict.fromkeys(
            (*live_report_module._CURRENT_EXPORT_FIELDS, *_LOCALIZATION_EXPORT_FIELDS[:4])
        )
    )
    live_report_module._JSON_EXPORT_FIELDS = tuple(
        dict.fromkeys(
            (*live_report_module._JSON_EXPORT_FIELDS, *_LOCALIZATION_EXPORT_FIELDS)
        )
    )
    live_report_module.write_live_batch_report = write_live_batch_report
    live_report_module._localization_projection_installed = True


__all__ = ["install_localization_reporting"]
