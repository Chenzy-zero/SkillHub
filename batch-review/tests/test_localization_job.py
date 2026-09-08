from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from skill_batch_review.localization import (
    empty_translation_memory,
    load_translation_memory,
    localization_pending_path,
    write_pending_translation_units,
)
from skill_batch_review.localization_job import (
    LocalizationJobError,
    import_localization_result,
    prepare_localization_job,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "report-zh-localizer"
    / "references"
    / "localization-result.schema.json"
)


def _records():
    return [
        {
            "skill_id": "skill-1",
            "findings": [
                {
                    "finding_id": "f-1",
                    "severity": "HIGH",
                    "title": "Dangerous command execution",
                    "description": "Arbitrary commands may be executed.",
                    "evidence_summary": "token=plain-text-secret near command",
                    "recommendation": "Restrict commands to an allow-list.",
                    "file_path": "SKILL.md",
                    "source_rule_id": "R-1",
                }
            ],
        }
    ]


def _prepare_pending(results_root: Path, batch_id: str = "batch-1"):
    memory = empty_translation_memory()
    path = localization_pending_path(results_root, batch_id)
    write_pending_translation_units(path, _records(), memory, memory_status="MISSING")
    return json.loads(path.read_text(encoding="utf-8"))


def _result_for(job, *, count: int | None = None):
    job_input = json.loads(job.input_path.read_text(encoding="utf-8"))
    units = job_input["units"] if count is None else job_input["units"][:count]
    return {
        "schema_version": "1.0",
        "job_id": job.job_id,
        "locale": "zh-CN",
        "translator": {"kind": "AI", "model": "test-model"},
        "translations": [
            {
                "translation_key": unit["translation_key"],
                "source_sha256": unit["source_sha256"],
                "translated_text": "中文：" + unit["source_text"],
            }
            for unit in units
        ],
    }


def test_schema_rejects_machine_fact_mutation_fields():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    payload = {
        "schema_version": "1.0",
        "job_id": "localization-" + "a" * 32,
        "locale": "zh-CN",
        "translator": {"kind": "AI", "model": "test-model"},
        "translations": [
            {
                "translation_key": "translation-" + "b" * 64,
                "source_sha256": "c" * 64,
                "translated_text": "中文",
                "severity": "LOW",
            }
        ],
    }
    errors = list(validator.iter_errors(payload))
    assert errors
    assert any("Additional properties are not allowed" in error.message for error in errors)


def test_prepare_freezes_bounded_report_safe_job(tmp_path):
    pending = _prepare_pending(tmp_path)
    job = prepare_localization_job(
        tmp_path,
        "batch-1",
        result_schema_path=SCHEMA,
        max_units=4,
    )
    assert job is not None
    assert job.unit_count == 4
    assert job.remaining_count == pending["pending_count"] - 4
    first_id = job.job_id

    payload = json.loads(job.input_path.read_text(encoding="utf-8"))
    assert payload["execution_boundary"] == {
        "report_safe_only": True,
        "raw_evidence_allowed": False,
        "network_allowed": False,
        "target_skill_access_allowed": False,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "plain-text-secret" not in encoded
    assert "[REDACTED]" in encoded
    assert "severity" not in encoded
    assert "source_rule_id" not in encoded
    assert "file_path" not in encoded

    repeated = prepare_localization_job(
        tmp_path,
        "batch-1",
        result_schema_path=SCHEMA,
        max_units=4,
    )
    assert repeated is not None
    assert repeated.job_id == first_id
    assert repeated.input_path == job.input_path


def test_invalid_pending_memory_status_refuses_dispatch(tmp_path):
    pending = _prepare_pending(tmp_path)
    pending["memory_status"] = "INVALID"
    localization_pending_path(tmp_path, "batch-1").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    with pytest.raises(LocalizationJobError, match="translation memory is invalid"):
        prepare_localization_job(tmp_path, "batch-1", result_schema_path=SCHEMA)


def test_package_api_rejects_unsafe_batch_id(tmp_path):
    with pytest.raises(LocalizationJobError, match="batch-id"):
        prepare_localization_job(tmp_path, "../escape", result_schema_path=SCHEMA)


def test_wrong_job_id_and_source_hash_are_rejected_without_memory_pollution(tmp_path):
    _prepare_pending(tmp_path)
    job = prepare_localization_job(tmp_path, "batch-1", result_schema_path=SCHEMA)
    assert job is not None

    wrong_job = _result_for(job)
    wrong_job["job_id"] = "localization-" + "0" * 32
    job.expected_result.write_text(json.dumps(wrong_job), encoding="utf-8")
    with pytest.raises(LocalizationJobError, match="does not match the frozen job"):
        import_localization_result(
            tmp_path,
            "batch-1",
            job_id=job.job_id,
            result_schema_path=SCHEMA,
        )
    assert not (tmp_path / "batch-1" / "translation-memory.zh-CN.json").exists()

    wrong_hash = _result_for(job)
    wrong_hash["translations"][0]["source_sha256"] = "0" * 64
    job.expected_result.write_text(json.dumps(wrong_hash), encoding="utf-8")
    with pytest.raises(LocalizationJobError, match="source hash differs"):
        import_localization_result(
            tmp_path,
            "batch-1",
            job_id=job.job_id,
            result_schema_path=SCHEMA,
        )
    assert not (tmp_path / "batch-1" / "translation-memory.zh-CN.json").exists()


def test_unexpected_and_duplicate_translation_keys_are_rejected(tmp_path):
    _prepare_pending(tmp_path)
    job = prepare_localization_job(tmp_path, "batch-1", result_schema_path=SCHEMA)
    assert job is not None

    unexpected = _result_for(job, count=1)
    unexpected["translations"][0]["translation_key"] = "translation-" + "f" * 64
    job.expected_result.write_text(json.dumps(unexpected), encoding="utf-8")
    with pytest.raises(LocalizationJobError, match="unexpected translation key"):
        import_localization_result(
            tmp_path, "batch-1", job_id=job.job_id, result_schema_path=SCHEMA
        )

    duplicate = _result_for(job, count=1)
    duplicate["translations"].append(dict(duplicate["translations"][0]))
    job.expected_result.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(LocalizationJobError, match="duplicate translation keys"):
        import_localization_result(
            tmp_path, "batch-1", job_id=job.job_id, result_schema_path=SCHEMA
        )


def test_valid_subset_imports_only_returned_keys_and_preserves_remaining_work(tmp_path):
    pending = _prepare_pending(tmp_path)
    job = prepare_localization_job(
        tmp_path, "batch-1", result_schema_path=SCHEMA, max_units=3
    )
    assert job is not None
    result = _result_for(job, count=2)
    job.expected_result.write_text(json.dumps(result), encoding="utf-8")

    imported = import_localization_result(
        tmp_path,
        "batch-1",
        job_id=job.job_id,
        result_schema_path=SCHEMA,
    )
    assert imported.imported_count == 2
    memory = load_translation_memory(
        tmp_path / "batch-1" / "translation-memory.zh-CN.json"
    )
    assert len(memory["entries"]) == 2
    audit = json.loads(
        (job.input_path.parent / "imported.json").read_text(encoding="utf-8")
    )
    assert audit["imported_count"] == 2

    # Pending is a projection artifact and is intentionally unchanged until the
    # trusted report refresh. Re-project it now, exactly as the CLI import path does.
    write_pending_translation_units(
        localization_pending_path(tmp_path, "batch-1"),
        _records(),
        memory,
        memory_status="READY",
    )
    refreshed = json.loads(
        localization_pending_path(tmp_path, "batch-1").read_text(encoding="utf-8")
    )
    assert refreshed["pending_count"] == pending["pending_count"] - 2
