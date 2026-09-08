"""Safety fixes installed around localization job public APIs.

The job module intentionally normalizes pending units before freezing them. This
boundary preserves the locale field required by Translation Memory and validates
batch identifiers at the package API itself, rather than relying only on the CLI.
"""

from __future__ import annotations

import re
from typing import Any


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _batch_id(value: Any, error_type: type[Exception]) -> str:
    text = str(value or "").strip()
    if not _BATCH_ID_RE.fullmatch(text):
        raise error_type("batch-id must be 1-64 safe filename characters")
    return text


def install_localization_job_safety(module: Any) -> None:
    if getattr(module, "_job_safety_installed", False):
        return

    original_validate = module._validate_unit
    original_prepare = module.prepare_localization_job
    original_import = module.import_localization_result

    def validate_unit(raw: Any, *, locale: str):
        value = dict(original_validate(raw, locale=locale))
        value["locale"] = locale
        return value

    def prepare_localization_job(results_root: Any, batch_id: Any, **kwargs: Any):
        safe_batch = _batch_id(batch_id, module.LocalizationJobError)
        return original_prepare(results_root, safe_batch, **kwargs)

    def import_localization_result(results_root: Any, batch_id: Any, **kwargs: Any):
        safe_batch = _batch_id(batch_id, module.LocalizationJobError)
        return original_import(results_root, safe_batch, **kwargs)

    module._validate_unit = validate_unit
    module.prepare_localization_job = prepare_localization_job
    module.import_localization_result = import_localization_result
    module._job_safety_installed = True


__all__ = ["install_localization_job_safety"]
