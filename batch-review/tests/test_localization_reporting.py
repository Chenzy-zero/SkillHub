from __future__ import annotations

import json
import re
import unittest

from skill_batch_review.live_report import build_live_records, write_live_batch_report
from skill_batch_review.localization import (
    extract_translation_units,
    localization_pending_path,
    merge_translations,
    translation_memory_path,
)
from skill_batch_review.per_skill import prepare_skill
from skill_batch_review.scanners import CiscoSkillScannerAdapter, SkillSpectorAdapter
from test_per_skill import PerSkillWorkflowTests, ScannerRunner


_REPORT_RE = re.compile(
    r'<script type="application/json" id="report-data">(.*?)</script>',
    re.DOTALL,
)


class LocalizationReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PerSkillWorkflowTests(
            methodName="test_archives_each_skill_and_reuses_approved_content"
        )
        self.fixture.setUp()
        self.config = self.fixture.config()
        self.inventory = self.fixture.inventory()
        runner = ScannerRunner()
        self.adapters = {
            "cisco": CiscoSkillScannerAdapter(runner=runner, tool_version="1.0"),
            "skillspector": SkillSpectorAdapter(runner=runner, tool_version="1.0"),
        }

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _prepare_with_finding(self, *, description: str = "visible before AI") -> None:
        prepare_skill(
            self.config,
            batch_id="localize-batch",
            row=self.inventory.rows[0],
            downloader=self.fixture.downloader,
            adapters=self.adapters,
        )
        current_path = self.config.workspace.skills_root / "id-one/current-result.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current["findings"] = [
            {
                "finding_id": "finding-static-1",
                "source_scanner": "CISCO_AI_SKILL_SCANNER",
                "source_scanners": ["CISCO_AI_SKILL_SCANNER"],
                "source_rule_id": "TEST-1",
                "category": "TEST",
                "severity": "HIGH",
                "title": "Static test finding",
                "description": description,
                "file_path": "SKILL.md",
                "start_line": 1,
                "end_line": 1,
                "evidence_summary": "token=plain-text-secret is present",
                "recommendation": "review the command scope",
                "confidence": 1.0,
                "fingerprint": "abc",
                "status": "OPEN",
                "domain": "SECURITY",
                "locations": [{"path": "SKILL.md", "start_line": 1, "end_line": 1}],
                "source_references": [],
            }
        ]
        current["finding_count"] = 1
        current["max_severity"] = "HIGH"
        current_path.write_text(json.dumps(current), encoding="utf-8")

    def test_live_report_materializes_report_safe_pending_units(self) -> None:
        self._prepare_with_finding()
        report = write_live_batch_report(
            self.config,
            self.inventory,
            batch_id="localize-batch",
        )
        pending_path = localization_pending_path(
            self.config.workspace.results_root,
            "localize-batch",
        )
        pending = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertTrue(report.paths.html.is_file())
        self.assertGreaterEqual(pending["pending_count"], 4)
        encoded = json.dumps(pending, ensure_ascii=False)
        self.assertNotIn("plain-text-secret", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertEqual(pending["memory_status"], "MISSING")

    def test_invalid_memory_does_not_block_or_overwrite_canonical_report(self) -> None:
        self._prepare_with_finding()
        memory_path = translation_memory_path(
            self.config.workspace.results_root,
            "localize-batch",
        )
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        original = '{"schema_version":"broken","entries":{}}\n'
        memory_path.write_text(original, encoding="utf-8")

        report = write_live_batch_report(
            self.config,
            self.inventory,
            batch_id="localize-batch",
        )
        self.assertTrue(report.paths.html.is_file())
        self.assertEqual(memory_path.read_text(encoding="utf-8"), original)
        summary = json.loads(report.paths.summary.read_text(encoding="utf-8"))
        self.assertEqual(summary["localization_memory_status"], "INVALID")
        pending = json.loads(
            localization_pending_path(
                self.config.workspace.results_root,
                "localize-batch",
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(pending["memory_status"], "INVALID")
        self.assertTrue(pending["memory_error"])

    def test_memory_hit_adds_localized_text_without_replacing_original(self) -> None:
        self._prepare_with_finding(description="English explanation")
        records = build_live_records(self.config, self.inventory)
        units = extract_translation_units(records)
        translations = [
            {
                "translation_key": unit["translation_key"],
                "locale": unit["locale"],
                "source_sha256": unit["source_sha256"],
                "translated_text": "中文-" + unit["source_text"],
            }
            for unit in units
        ]
        merge_translations(
            translation_memory_path(
                self.config.workspace.results_root,
                "localize-batch",
            ),
            expected_units=units,
            translations=translations,
            translator={"kind": "TEST"},
        )

        report = write_live_batch_report(
            self.config,
            self.inventory,
            batch_id="localize-batch",
        )
        exported = json.loads(report.current_json.read_text(encoding="utf-8"))
        first = next(item for item in exported["skills"] if item["skill_id"] == "id-one")
        finding = first["findings"][0]
        self.assertEqual(finding["description"], "English explanation")
        self.assertEqual(finding["description_zh"], "中文-English explanation")
        self.assertEqual(first["localization_status"], "COMPLETE")

        match = _REPORT_RE.search(report.paths.html.read_text(encoding="utf-8"))
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        skill = next(item for item in payload["skills"] if item["skill_id"] == "id-one")
        self.assertEqual(skill["findings"][0]["description"], "English explanation")
        self.assertEqual(skill["findings"][0]["description_zh"], "中文-English explanation")

        pending = json.loads(
            localization_pending_path(
                self.config.workspace.results_root,
                "localize-batch",
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(pending["pending_count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
