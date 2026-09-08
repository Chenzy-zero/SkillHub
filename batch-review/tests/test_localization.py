from __future__ import annotations

import json

import pytest

import skill_batch_review.live_report as live_report
from skill_batch_review.localization import (
    LocalizationError,
    build_localized_projection,
    empty_translation_memory,
    extract_translation_units,
    load_translation_memory,
    merge_translations,
    pending_translation_units,
    source_sha256,
    translation_key,
)


def _record(description: str = "Skill requests unrestricted shell access."):
    return {
        "skill_id": "skill-1",
        "repo_name": "repo",
        "findings": [
            {
                "finding_id": "finding-1",
                "severity": "HIGH",
                "file_path": "SKILL.md",
                "source_rule_id": "RULE-1",
                "title": "Unrestricted shell execution",
                "description": description,
                "evidence_summary": "token=plain-text-secret was found near the command.",
                "recommendation": "Restrict commands to an allow-list.",
            }
        ],
    }


def _translations(units):
    return [
        {
            "translation_key": unit["translation_key"],
            "locale": unit["locale"],
            "source_sha256": unit["source_sha256"],
            "translated_text": f"中文：{unit['source_text']}",
        }
        for unit in units
    ]


def test_package_installs_localization_projection():
    assert getattr(live_report, "_localization_projection_installed", False) is True


def test_translation_units_are_report_safe_and_never_include_machine_facts():
    units = extract_translation_units([_record()])
    fields = {unit["field"] for unit in units}
    assert fields == {
        "finding.title",
        "finding.description",
        "finding.evidence_summary",
        "finding.recommendation",
    }
    combined = "\n".join(unit["source_text"] for unit in units)
    assert "plain-text-secret" not in combined
    assert "[REDACTED]" in combined
    assert "RULE-1" not in combined
    assert "SKILL.md" not in combined
    assert "HIGH" not in combined


def test_second_pass_has_zero_pending_after_memory_merge(tmp_path):
    records = [_record()]
    units = extract_translation_units(records)
    path = tmp_path / "translation-memory.zh-CN.json"
    memory = empty_translation_memory()
    assert len(pending_translation_units(records, memory)) == len(units)

    merged = merge_translations(
        path,
        expected_units=units,
        translations=_translations(units),
        translator={"kind": "TEST"},
    )
    assert len(merged["entries"]) == len(units)
    assert pending_translation_units(records, merged) == []


def test_changed_source_text_generates_a_new_translation_key(tmp_path):
    original = [_record("Original description")]
    units = extract_translation_units(original)
    path = tmp_path / "translation-memory.zh-CN.json"
    merged = merge_translations(
        path,
        expected_units=units,
        translations=_translations(units),
    )

    changed = [_record("Changed description")]
    pending = pending_translation_units(changed, merged)
    changed_description = [unit for unit in pending if unit["field"] == "finding.description"]
    assert len(changed_description) == 1
    old_description = [unit for unit in units if unit["field"] == "finding.description"][0]
    assert changed_description[0]["translation_key"] != old_description["translation_key"]


def test_bad_translation_hash_is_rejected_without_writing_memory(tmp_path):
    records = [_record()]
    units = extract_translation_units(records)
    path = tmp_path / "translation-memory.zh-CN.json"
    bad = _translations(units)
    bad[0]["source_sha256"] = "0" * 64

    with pytest.raises(LocalizationError, match="source hash differs"):
        merge_translations(path, expected_units=units, translations=bad)
    assert not path.exists()


def test_malformed_existing_memory_is_not_replaced(tmp_path):
    path = tmp_path / "translation-memory.zh-CN.json"
    original = '{"schema_version":"broken","entries":{}}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(LocalizationError):
        load_translation_memory(path)
    assert path.read_text(encoding="utf-8") == original


def test_localized_projection_preserves_original_and_adds_zh_fields(tmp_path):
    records = [_record()]
    units = extract_translation_units(records)
    path = tmp_path / "translation-memory.zh-CN.json"
    memory = merge_translations(
        path,
        expected_units=units,
        translations=_translations(units),
    )
    projection = build_localized_projection(records, memory)
    finding = projection[0]["findings"][0]

    assert finding["description"] == "Skill requests unrestricted shell access."
    assert finding["description_zh"].startswith("中文：")
    assert projection[0]["localization_status"] == "COMPLETE"
    assert projection[0]["localization_translated"] == projection[0]["localization_total"]


def test_translation_key_binds_locale_field_and_source_hash():
    text = "same text"
    digest = source_sha256(text)
    first = translation_key("zh-CN", "finding.title", text)
    assert first == translation_key("zh-CN", "finding.title", text)
    assert first != translation_key("zh-CN", "finding.description", text)
    assert first != translation_key("en-US", "finding.title", text)
    assert digest == source_sha256(text)
