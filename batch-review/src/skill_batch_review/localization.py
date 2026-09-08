"""Report-safe incremental localization primitives.

Localization is strictly downstream of canonical review results. Translation
units are built only from redacted report text; machine facts and raw evidence
never enter this layer. A translation is reusable only when locale, field type,
and the exact redacted source text all match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .reporting import redact


DEFAULT_LOCALE = "zh-CN"
MEMORY_SCHEMA_VERSION = "1.0"
PENDING_SCHEMA_VERSION = "1.0"

_TRANSLATABLE_FINDING_FIELDS = (
    "title",
    "description",
    "evidence_summary",
    "recommendation",
)
_TRANSLATABLE_RECORD_FIELDS = (
    "failure_reason",
    "manual_reason",
    "reuse_reason",
)
_SAFE_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


class LocalizationError(ValueError):
    """Localization state is malformed or cannot be updated safely."""


def _locale(value: str) -> str:
    locale = str(value or "").strip()
    if not _SAFE_LOCALE_RE.fullmatch(locale):
        raise LocalizationError(f"invalid locale: {value!r}")
    return locale


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    redacted = redact(str(value))
    return str(redacted).strip() if redacted is not None else ""


def source_sha256(source_text: str) -> str:
    return hashlib.sha256(str(source_text).encode("utf-8")).hexdigest()


def translation_key(locale: str, field: str, source_text: str) -> str:
    locale = _locale(locale)
    field = str(field or "").strip()
    if not field:
        raise LocalizationError("translation field must not be empty")
    source = str(source_text)
    encoded = json.dumps(
        [locale, field, source_sha256(source)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "translation-" + hashlib.sha256(encoded).hexdigest()


def make_translation_unit(
    *,
    locale: str,
    field: str,
    source_text: Any,
) -> dict[str, str] | None:
    source = _safe_text(source_text)
    if not source:
        return None
    locale = _locale(locale)
    digest = source_sha256(source)
    return {
        "translation_key": translation_key(locale, field, source),
        "locale": locale,
        "field": field,
        "source_sha256": digest,
        "source_text": source,
    }


def _safe_record(record: Mapping[str, Any]) -> dict[str, Any]:
    value = redact(record)
    if not isinstance(value, Mapping):
        raise LocalizationError("report record could not be redacted safely")
    return dict(value)


def extract_translation_units(
    records: Iterable[Mapping[str, Any]],
    *,
    locale: str = DEFAULT_LOCALE,
) -> list[dict[str, str]]:
    """Return de-duplicated translation units from report-safe free text only."""

    locale = _locale(locale)
    units: dict[str, dict[str, str]] = {}
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            raise LocalizationError("each localization input record must be a mapping")
        record = _safe_record(raw_record)
        for field in _TRANSLATABLE_RECORD_FIELDS:
            unit = make_translation_unit(
                locale=locale,
                field=f"record.{field}",
                source_text=record.get(field),
            )
            if unit is not None:
                units[unit["translation_key"]] = unit
        findings = record.get("findings")
        if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes, bytearray)):
            continue
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            for field in _TRANSLATABLE_FINDING_FIELDS:
                unit = make_translation_unit(
                    locale=locale,
                    field=f"finding.{field}",
                    source_text=finding.get(field),
                )
                if unit is not None:
                    units[unit["translation_key"]] = unit
    return [units[key] for key in sorted(units)]


def empty_translation_memory(locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    return {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "locale": _locale(locale),
        "entries": {},
    }


def translation_memory_path(
    results_root: Path,
    batch_id: str,
    *,
    locale: str = DEFAULT_LOCALE,
) -> Path:
    locale = _locale(locale)
    return Path(results_root) / str(batch_id) / f"translation-memory.{locale}.json"


def localization_pending_path(
    results_root: Path,
    batch_id: str,
    *,
    locale: str = DEFAULT_LOCALE,
) -> Path:
    locale = _locale(locale)
    return Path(results_root) / str(batch_id) / f"localization-pending.{locale}.json"


def _validate_memory(value: Mapping[str, Any], *, locale: str) -> dict[str, Any]:
    locale = _locale(locale)
    if value.get("schema_version") != MEMORY_SCHEMA_VERSION:
        raise LocalizationError("translation memory schema_version is unsupported")
    if value.get("locale") != locale:
        raise LocalizationError("translation memory locale does not match")
    entries = value.get("entries")
    if not isinstance(entries, Mapping):
        raise LocalizationError("translation memory entries must be a mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in entries.items():
        key = str(raw_key)
        if not isinstance(raw_entry, Mapping):
            raise LocalizationError(f"translation memory entry {key} is not a mapping")
        field = str(raw_entry.get("field") or "").strip()
        source = str(raw_entry.get("source_text") or "")
        digest = str(raw_entry.get("source_sha256") or "")
        translated = raw_entry.get("translated_text")
        if raw_entry.get("locale") != locale:
            raise LocalizationError(f"translation memory entry {key} has wrong locale")
        if digest != source_sha256(source):
            raise LocalizationError(f"translation memory entry {key} source hash differs")
        if key != translation_key(locale, field, source):
            raise LocalizationError(f"translation memory entry {key} key differs")
        if not isinstance(translated, str) or not translated.strip():
            raise LocalizationError(f"translation memory entry {key} has no translation")
        normalized[key] = dict(raw_entry)
    return {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "locale": locale,
        "entries": normalized,
    }


def load_translation_memory(
    path: Path,
    *,
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any]:
    """Load and strictly validate memory; a malformed existing file never resets."""

    path = Path(path)
    if not path.exists():
        return empty_translation_memory(locale)
    if path.is_symlink() or not path.is_file():
        raise LocalizationError(f"translation memory path is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalizationError(f"translation memory cannot be read: {exc}") from exc
    if not isinstance(value, Mapping):
        raise LocalizationError("translation memory root must be a mapping")
    return _validate_memory(value, locale=locale)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LocalizationError(f"localization output is not a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise LocalizationError(f"cannot write localization output {path}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def pending_translation_units(
    records: Iterable[Mapping[str, Any]],
    memory: Mapping[str, Any],
    *,
    locale: str = DEFAULT_LOCALE,
) -> list[dict[str, str]]:
    validated = _validate_memory(memory, locale=locale)
    existing = validated["entries"]
    return [
        unit
        for unit in extract_translation_units(records, locale=locale)
        if unit["translation_key"] not in existing
    ]


def write_pending_translation_units(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    memory: Mapping[str, Any],
    *,
    locale: str = DEFAULT_LOCALE,
    memory_status: str = "READY",
    memory_error: str = "",
) -> Path:
    units = pending_translation_units(records, memory, locale=locale)
    return _atomic_json(
        path,
        {
            "schema_version": PENDING_SCHEMA_VERSION,
            "locale": _locale(locale),
            "memory_status": str(memory_status),
            "memory_error": _safe_text(memory_error),
            "pending_count": len(units),
            "units": units,
        },
    )


def merge_translations(
    path: Path,
    *,
    expected_units: Iterable[Mapping[str, Any]],
    translations: Iterable[Mapping[str, Any]],
    locale: str = DEFAULT_LOCALE,
    translator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and atomically merge trusted translation results.

    Existing entries are immutable. Changed source text gets a different key,
    so iterative localization never has to overwrite historical translations.
    """

    locale = _locale(locale)
    memory = load_translation_memory(path, locale=locale)
    expected = {
        str(unit.get("translation_key")): dict(unit)
        for unit in expected_units
        if isinstance(unit, Mapping) and unit.get("translation_key")
    }
    entries = dict(memory["entries"])
    for result in translations:
        if not isinstance(result, Mapping):
            raise LocalizationError("translation result must be a mapping")
        key = str(result.get("translation_key") or "")
        unit = expected.get(key)
        if unit is None:
            raise LocalizationError(f"unexpected translation key: {key!r}")
        if result.get("locale") != locale:
            raise LocalizationError(f"translation {key} has wrong locale")
        if result.get("source_sha256") != unit.get("source_sha256"):
            raise LocalizationError(f"translation {key} source hash differs")
        translated = result.get("translated_text")
        if not isinstance(translated, str) or not translated.strip():
            raise LocalizationError(f"translation {key} translated_text is empty")
        if key in entries:
            continue
        entries[key] = {
            **unit,
            "translated_text": translated.strip(),
            "translator": dict(translator or {}),
        }
    updated = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "locale": locale,
        "entries": entries,
    }
    _validate_memory(updated, locale=locale)
    _atomic_json(path, updated)
    return updated


def _translated_text(
    memory: Mapping[str, Any],
    *,
    locale: str,
    field: str,
    source_text: Any,
) -> str:
    unit = make_translation_unit(locale=locale, field=field, source_text=source_text)
    if unit is None:
        return ""
    entry = memory.get("entries", {}).get(unit["translation_key"])
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("translated_text") or "")


def build_localized_projection(
    records: Iterable[Mapping[str, Any]],
    memory: Mapping[str, Any],
    *,
    locale: str = DEFAULT_LOCALE,
) -> list[dict[str, Any]]:
    """Return redacted report records with optional ``*_zh`` presentation text."""

    validated = _validate_memory(memory, locale=locale)
    projected: list[dict[str, Any]] = []
    for raw_record in records:
        record = _safe_record(raw_record)
        hits = total = 0
        for field in _TRANSLATABLE_RECORD_FIELDS:
            source = record.get(field)
            if not _safe_text(source):
                continue
            total += 1
            translated = _translated_text(
                validated,
                locale=locale,
                field=f"record.{field}",
                source_text=source,
            )
            if translated:
                record[f"{field}_zh"] = translated
                hits += 1
        findings = record.get("findings")
        localized_findings: list[dict[str, Any]] = []
        if isinstance(findings, Sequence) and not isinstance(findings, (str, bytes, bytearray)):
            for raw_finding in findings:
                if not isinstance(raw_finding, Mapping):
                    continue
                finding = dict(raw_finding)
                for field in _TRANSLATABLE_FINDING_FIELDS:
                    source = finding.get(field)
                    if not _safe_text(source):
                        continue
                    total += 1
                    translated = _translated_text(
                        validated,
                        locale=locale,
                        field=f"finding.{field}",
                        source_text=source,
                    )
                    if translated:
                        finding[f"{field}_zh"] = translated
                        hits += 1
                localized_findings.append(finding)
            record["findings"] = localized_findings
        record["localization_locale"] = _locale(locale)
        record["localization_total"] = total
        record["localization_translated"] = hits
        record["localization_status"] = (
            "NOT_REQUIRED" if total == 0 else "COMPLETE" if hits == total else "PARTIAL" if hits else "PENDING"
        )
        projected.append(record)
    return projected


__all__ = [
    "DEFAULT_LOCALE",
    "LocalizationError",
    "build_localized_projection",
    "empty_translation_memory",
    "extract_translation_units",
    "load_translation_memory",
    "localization_pending_path",
    "make_translation_unit",
    "merge_translations",
    "pending_translation_units",
    "source_sha256",
    "translation_key",
    "translation_memory_path",
    "write_pending_translation_units",
]
