from __future__ import annotations

import json
import re
import unittest

import skill_batch_review.html_reporting as html_reporting
from skill_batch_review.live_report import build_live_records, write_live_batch_report
from skill_batch_review.localization import (
    extract_translation_units,
    merge_translations,
    translation_memory_path,
)
from skill_batch_review.per_skill import prepare_skill
from skill_batch_review.scanners import CiscoSkillScannerAdapter, SkillSpectorAdapter
from test_per_skill import PerSkillWorkflowTests, ScannerRunner


_REPORT_RE = re.compile(
    r'<script type="application/json" id="report-data">(.*?)</script>', re.DOTALL
)


class BilingualHtmlTests(unittest.TestCase):
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

    def _prepare_finding(self) -> None:
        prepare_skill(
            self.config,
            batch_id="bilingual-batch",
            row=self.inventory.rows[0],
            downloader=self.fixture.downloader,
            adapters=self.adapters,
        )
        path = self.config.workspace.skills_root / "id-one/current-result.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        current["findings"] = [
            {
                "finding_id": "finding-1",
                "source_scanner": "CISCO_AI_SKILL_SCANNER",
                "source_scanners": ["CISCO_AI_SKILL_SCANNER"],
                "source_rule_id": "RULE-1",
                "category": "COMMAND_EXECUTION",
                "severity": "HIGH",
                "title": "Dangerous command execution",
                "description": "Arbitrary commands may be executed.",
                "file_path": "SKILL.md",
                "start_line": 7,
                "end_line": 7,
                "evidence_summary": "The command is not restricted.",
                "recommendation": "Restrict commands to an allow-list.",
                "confidence": 1.0,
                "fingerprint": "fingerprint-1",
                "status": "OPEN",
                "domain": "SECURITY",
                "locations": [{"path": "SKILL.md", "start_line": 7, "end_line": 7}],
                "source_references": [],
            }
        ]
        current["finding_count"] = 1
        current["max_severity"] = "HIGH"
        path.write_text(json.dumps(current), encoding="utf-8")

    def _payload(self, html: str):
        match = _REPORT_RE.search(html)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_bilingual_adapter_is_installed(self) -> None:
        self.assertTrue(getattr(html_reporting, "_bilingual_html_installed", False))

    def test_localized_report_uses_chinese_first_and_keeps_english_original(self) -> None:
        self._prepare_finding()
        canonical = build_live_records(self.config, self.inventory)
        units = extract_translation_units(canonical)
        translations = []
        for unit in units:
            translated = {
                "Dangerous command execution": "危险的命令执行",
                "Arbitrary commands may be executed.": "可能执行任意命令。",
                "The command is not restricted.": "该命令未受到限制。",
                "Restrict commands to an allow-list.": "将命令限制为允许列表。",
            }.get(unit["source_text"], "中文：" + unit["source_text"])
            translations.append(
                {
                    "translation_key": unit["translation_key"],
                    "locale": unit["locale"],
                    "source_sha256": unit["source_sha256"],
                    "translated_text": translated,
                }
            )
        merge_translations(
            translation_memory_path(
                self.config.workspace.results_root, "bilingual-batch"
            ),
            expected_units=units,
            translations=translations,
            translator={"kind": "TEST", "model": "fixture"},
        )

        report = write_live_batch_report(
            self.config, self.inventory, batch_id="bilingual-batch"
        )
        page = report.paths.html.read_text(encoding="utf-8")
        payload = self._payload(page)
        skill = next(item for item in payload["skills"] if item["skill_id"] == "id-one")
        finding = skill["findings"][0]

        self.assertEqual(finding["title"], "Dangerous command execution")
        self.assertEqual(finding["title_zh"], "危险的命令执行")
        self.assertEqual(finding["description"], "Arbitrary commands may be executed.")
        self.assertEqual(finding["description_zh"], "可能执行任意命令。")
        self.assertIn("查看英文原文", page)
        self.assertIn("row.finding.title_zh||row.finding.title", page)
        self.assertIn("localizedFindingText(finding,'description'", page)
        self.assertIn("originalFindingDetails(finding)", page)
        self.assertIn("description_zh:finding.description_zh", page)

    def test_untranslated_report_falls_back_to_english_without_blocking(self) -> None:
        self._prepare_finding()
        report = write_live_batch_report(
            self.config, self.inventory, batch_id="bilingual-batch"
        )
        page = report.paths.html.read_text(encoding="utf-8")
        payload = self._payload(page)
        skill = next(item for item in payload["skills"] if item["skill_id"] == "id-one")
        finding = skill["findings"][0]

        self.assertEqual(finding["title"], "Dangerous command execution")
        self.assertNotIn("title_zh", finding)
        self.assertIn("finding[field+'_zh']||finding[field]||fallback", page)
        self.assertTrue(report.paths.html.is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
