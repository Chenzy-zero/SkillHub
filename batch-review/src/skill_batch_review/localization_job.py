"""Trusted job preparation and import for report localization.

The model never receives canonical review state or raw evidence.  This module
freezes a bounded subset of the already-redacted pending translation units,
validates the localizer's strict JSON result, and merges only key/hash-bound text
into Translation Memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from .localization import (
    DEFAULT_LOCALE,
    LocalizationError,
    localization_pending_path,
    merge_translations,
    source_sha256,
    translation_key,
    translation_memory_path,
)


JOB_SCHEMA_VERSION = "1.0"
DEFAULT_MAX_UNITS = 100
_MAX_SCHEMA_UNITS = 200
_JOB_ID_RE = re.compile(r"^localization-[0-9a-f]{32}$")
_TRANSLATION_KEY_RE = re.compile(r"^translation-[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalizationJobError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalizationJob:
    batch_id: str
    job_id: str
    locale: str
    unit_count: int
    remaining_count: int
    input_path: Path
    expected_result: Path
    result_schema_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "job_id": self.job_id,
            "locale": self.locale,
            "unit_count": self.unit_count,
            "remaining_count": self.remaining_count,
            "input": str(self.input_path),
            "expected_result": str(self.expected_result),
            "result_schema": str(self.result_schema_path),
            "skill": "report-zh-localizer",
            "skill_invocations": {
                "claude_code": "/report-zh-localizer",
                "codex_cli": "$report-zh-localizer",
            },
        }


@dataclass(frozen=True, slots=True)
class LocalizationImportResult:
    batch_id: str
    job_id: str
    locale: str
    imported_count: int
    imported_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "job_id": self.job_id,
            "locale": self.locale,
            "imported_count": self.imported_count,
            "imported_keys": list(self.imported_keys),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LocalizationJobError(f"localization job output is not a regular file: {path}")
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
        raise LocalizationJobError(f"cannot write localization job {path}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _load_regular_json(path: Path, *, label: str) -> Mapping[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise LocalizationJobError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalizationJobError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise LocalizationJobError(f"{label} root must be an object")
    return value


def _validate_unit(raw: Mapping[str, Any], *, locale: str) -> dict[str, str]:
    key = str(raw.get("translation_key") or "")
    field = str(raw.get("field") or "").strip()
    digest = str(raw.get("source_sha256") or "").lower()
    source = raw.get("source_text")
    if not _TRANSLATION_KEY_RE.fullmatch(key):
        raise LocalizationJobError("pending unit has an invalid translation_key")
    if not field:
        raise LocalizationJobError(f"pending unit {key} has no field")
    if not isinstance(source, str) or not source.strip():
        raise LocalizationJobError(f"pending unit {key} has no source_text")
    if not _SHA256_RE.fullmatch(digest) or digest != source_sha256(source):
        raise LocalizationJobError(f"pending unit {key} has an invalid source hash")
    if key != translation_key(locale, field, source):
        raise LocalizationJobError(f"pending unit {key} does not match locale/field/source")
    return {
        "translation_key": key,
        "field": field,
        "source_sha256": digest,
        "source_text": source,
    }


def _load_pending(results_root: Path, batch_id: str, *, locale: str) -> list[dict[str, str]]:
    path = localization_pending_path(results_root, batch_id, locale=locale)
    value = _load_regular_json(path, label="localization pending file")
    if value.get("schema_version") != "1.0" or value.get("locale") != locale:
        raise LocalizationJobError("localization pending file schema/locale mismatch")
    if str(value.get("memory_status") or "") == "INVALID":
        raise LocalizationJobError(
            "translation memory is invalid; repair or remove it before dispatching localization"
        )
    units = value.get("units")
    if not isinstance(units, list):
        raise LocalizationJobError("localization pending units must be an array")
    if value.get("pending_count") != len(units):
        raise LocalizationJobError("localization pending_count does not match units")
    normalized = [_validate_unit(item, locale=locale) for item in units if isinstance(item, Mapping)]
    if len(normalized) != len(units):
        raise LocalizationJobError("localization pending contains a non-object unit")
    keys = [item["translation_key"] for item in normalized]
    if len(set(keys)) != len(keys):
        raise LocalizationJobError("localization pending contains duplicate translation keys")
    return normalized


def _job_id(locale: str, units: Sequence[Mapping[str, Any]]) -> str:
    material = json.dumps(
        [
            locale,
            [
                [str(item["translation_key"]), str(item["source_sha256"])]
                for item in units
            ],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "localization-" + hashlib.sha256(material).hexdigest()[:32]


def _job_root(results_root: Path, batch_id: str, locale: str, job_id: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise LocalizationJobError(f"invalid localization job id: {job_id!r}")
    return Path(results_root) / str(batch_id) / "localization-jobs" / locale / job_id


def prepare_localization_job(
    results_root: Path,
    batch_id: str,
    *,
    result_schema_path: Path,
    locale: str = DEFAULT_LOCALE,
    max_units: int = DEFAULT_MAX_UNITS,
) -> LocalizationJob | None:
    if not isinstance(max_units, int) or isinstance(max_units, bool) or not 1 <= max_units <= _MAX_SCHEMA_UNITS:
        raise LocalizationJobError(f"max_units must be between 1 and {_MAX_SCHEMA_UNITS}")
    schema = Path(result_schema_path).expanduser().resolve(strict=True)
    if schema.is_symlink() or not schema.is_file():
        raise LocalizationJobError("localization result schema must be a regular file")
    pending = _load_pending(results_root, batch_id, locale=locale)
    if not pending:
        return None
    selected = pending[:max_units]
    job_id = _job_id(locale, selected)
    root = _job_root(results_root, batch_id, locale, job_id)
    input_path = root / "input.json"
    expected_result = root / "result.json"
    payload = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "locale": locale,
        "units": selected,
        "execution_boundary": {
            "report_safe_only": True,
            "raw_evidence_allowed": False,
            "network_allowed": False,
            "target_skill_access_allowed": False,
        },
    }
    if input_path.exists():
        existing = _load_regular_json(input_path, label="localization job input")
        if dict(existing) != payload:
            raise LocalizationJobError(f"existing localization job input differs: {job_id}")
    else:
        _atomic_json(input_path, payload)
    return LocalizationJob(
        batch_id=str(batch_id),
        job_id=job_id,
        locale=locale,
        unit_count=len(selected),
        remaining_count=len(pending) - len(selected),
        input_path=input_path.resolve(),
        expected_result=expected_result.resolve(),
        result_schema_path=schema,
    )


def _load_schema(path: Path) -> Mapping[str, Any]:
    value = _load_regular_json(path, label="localization result schema")
    Draft202012Validator.check_schema(value)
    return value


def _validate_result_schema(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        details = []
        for error in errors[:12]:
            location = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}"
                for part in error.absolute_path
            )
            details.append(f"{location}: {error.message}")
        raise LocalizationJobError("localization result schema validation failed: " + "; ".join(details))


def import_localization_result(
    results_root: Path,
    batch_id: str,
    *,
    job_id: str,
    result_schema_path: Path,
    locale: str = DEFAULT_LOCALE,
) -> LocalizationImportResult:
    root = _job_root(results_root, batch_id, locale, job_id)
    job = _load_regular_json(root / "input.json", label="localization job input")
    result = _load_regular_json(root / "result.json", label="localization result")
    schema = _load_schema(Path(result_schema_path).expanduser().resolve(strict=True))
    _validate_result_schema(result, schema)

    if job.get("schema_version") != JOB_SCHEMA_VERSION or job.get("job_id") != job_id or job.get("locale") != locale:
        raise LocalizationJobError("localization job input identity is invalid")
    if result.get("job_id") != job_id or result.get("locale") != locale:
        raise LocalizationJobError("localization result does not match the frozen job")
    raw_units = job.get("units")
    if not isinstance(raw_units, list):
        raise LocalizationJobError("localization job units are invalid")
    expected_units = [_validate_unit(item, locale=locale) for item in raw_units if isinstance(item, Mapping)]
    if len(expected_units) != len(raw_units):
        raise LocalizationJobError("localization job contains a non-object unit")
    expected = {item["translation_key"]: item for item in expected_units}

    translations = result.get("translations")
    if not isinstance(translations, list):
        raise LocalizationJobError("localization result translations must be an array")
    keys: list[str] = []
    normalized_results: list[dict[str, Any]] = []
    for raw in translations:
        if not isinstance(raw, Mapping):
            raise LocalizationJobError("localization result contains a non-object translation")
        key = str(raw.get("translation_key") or "")
        unit = expected.get(key)
        if unit is None:
            raise LocalizationJobError(f"unexpected translation key: {key!r}")
        if str(raw.get("source_sha256") or "") != unit["source_sha256"]:
            raise LocalizationJobError(f"translation {key} source hash differs")
        keys.append(key)
        normalized_results.append({**dict(raw), "locale": locale})
    if len(set(keys)) != len(keys):
        raise LocalizationJobError("localization result contains duplicate translation keys")

    translator = result.get("translator")
    if not isinstance(translator, Mapping):
        raise LocalizationJobError("localization result translator is invalid")
    try:
        merge_translations(
            translation_memory_path(results_root, batch_id, locale=locale),
            expected_units=expected_units,
            translations=normalized_results,
            locale=locale,
            translator=translator,
        )
    except LocalizationError as exc:
        raise LocalizationJobError(str(exc)) from exc

    imported = tuple(keys)
    _atomic_json(
        root / "imported.json",
        {
            "schema_version": "1.0",
            "job_id": job_id,
            "locale": locale,
            "imported_at": _utc_now(),
            "imported_count": len(imported),
            "imported_keys": list(imported),
        },
    )
    return LocalizationImportResult(
        batch_id=str(batch_id),
        job_id=job_id,
        locale=locale,
        imported_count=len(imported),
        imported_keys=imported,
    )


__all__ = [
    "DEFAULT_MAX_UNITS",
    "LocalizationImportResult",
    "LocalizationJob",
    "LocalizationJobError",
    "import_localization_result",
    "prepare_localization_job",
]
