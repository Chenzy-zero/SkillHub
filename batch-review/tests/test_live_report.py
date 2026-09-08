from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace

from skill_batch_review.live_report import write_live_batch_report
from skill_batch_review.per_skill import finalize_skill, prepare_skill
from skill_batch_review.scanners import CiscoSkillScannerAdapter, SkillSpectorAdapter
from test_orchestrator import valid_ai_result
from test_per_skill import PerSkillWorkflowTests, ScannerRunner


_REPORT_RE = re.compile(
    r'<script type="application/json" id="report-data">(.*?)</script>',
    re.DOTALL,
)


class LiveReportTests(unittest.TestCase):
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

    def _prepare_first(self):
        return prepare_skill(
            self.config,
            batch_id="live-batch",
            row=self.inventory.rows[0],
            downloader=self.fixture.downloader,
            adapters=self.adapters,
        )

    def _finalize(self, prepared) -> None:
        ai_path = self.fixture.root / f"{prepared.task_id}-ai.json"
        result = valid_ai_result(
            prepared.task_id,
            self.fixture.revision,
            prepared.snapshot.skill_digest,
        )
        for dimension in result["quality_review"]["dimensions"]:
            del dimension["max_score"]
        ai_path.write_text(json.dumps(result), encoding="utf-8")
        finalize_skill(
            self.config,
            index_path=prepared.index_path,
            ai_result_path=ai_path,
        )

    def test_static_result_renders_interim_report_before_any_ai_result(self) -> None:
        prepared = self._prepare_first()
        self.assertTrue(prepared.requires_ai)

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
                "description": "visible before AI",
                "file_path": "SKILL.md",
                "start_line": 1,
                "end_line": 1,
                "evidence_summary": "safe excerpt",
                "recommendation": "review",
                "confidence": 1.0,
                "fingerprint": "abc",
                "status": "OPEN",
                "domain": "SECURITY",
                "locations": [{"path": "SKILL.md", "start_line": 1, "end_line": 1}],
                "source_references": [],
            }
        ]
        current["finding_counts"] = {
            "CRITICAL": 0,
            "HIGH": 1,
            "MEDIUM": 0,
            "LOW": 0,
            "INFO": 0,
        }
        current["finding_count"] = 1
        current["max_severity"] = "HIGH"
        current_path.write_text(json.dumps(current), encoding="utf-8")

        report = write_live_batch_report(
            self.config,
            self.inventory,
            batch_id="live-batch",
        )
        self.assertEqual(report.report_status, "INTERIM")
        self.assertEqual(report.progress["selected"], 2)
        self.assertEqual(report.progress["static_completed"], 1)
        self.assertEqual(report.progress["ai_completed"], 0)
        self.assertEqual(report.progress["final_completed"], 0)
        self.assertTrue(report.paths.html.is_file())
        self.assertTrue(report.current_csv.is_file())
        self.assertTrue(report.current_json.is_file())

        current_export = json.loads(report.current_json.read_text(encoding="utf-8"))
        first = current_export["skills"][0]
        self.assertEqual(first["static_status"], "COMPLETED")
        self.assertEqual(first["ai_status"], "PENDING")
        self.assertEqual(first["final_status"], "PENDING")
        self.assertEqual(first["security_decision"], "")

        html = report.paths.html.read_text(encoding="utf-8")
        self.assertIn('data-report-status="INTERIM"', html)
        self.assertIn("AI 未完成的 Skill 不代表最终安全通过", html)
        payload_match = _REPORT_RE.search(html)
        self.assertIsNotNone(payload_match)
        payload = json.loads(payload_match.group(1))
        self.assertEqual(payload["metadata"]["report_status"], "INTERIM")
        skill = next(item for item in payload["skills"] if item["skill_id"] == "id-one")
        self.assertEqual(skill["ai_status"], "PENDING")
        self.assertEqual(skill["security_decision"], "")
        self.assertEqual(skill["findings"][0]["title"], "Static test finding")

    def test_report_switches_to_final_when_all_selected_skills_are_resolved(self) -> None:
        first = self._prepare_first()
        self._finalize(first)
        second = prepare_skill(
            self.config,
            batch_id="live-batch",
            row=self.inventory.rows[1],
            downloader=self.fixture.downloader,
            adapters=self.adapters,
        )
        if second.requires_ai:
            self._finalize(second)

        report = write_live_batch_report(
            self.config,
            self.inventory,
            batch_id="live-batch",
        )
        self.assertEqual(report.report_status, "FINAL")
        self.assertEqual(report.progress["selected"], 2)
        self.assertEqual(
            report.progress["final_completed"] + report.progress["final_incomplete"],
            2,
        )
        html = report.paths.html.read_text(encoding="utf-8")
        self.assertIn('data-report-status="FINAL"', html)
        summary = json.loads(report.paths.summary.read_text(encoding="utf-8"))
        self.assertEqual(summary["report_status"], "FINAL")

    def test_public_exports_do_not_leak_internal_absolute_result_paths(self) -> None:
        self._prepare_first()
        report = write_live_batch_report(
            self.config,
            self.inventory,
            batch_id="live-batch",
        )

        exported = report.current_json.read_text(encoding="utf-8")
        html = report.paths.html.read_text(encoding="utf-8")
        internal_root = str(self.config.workspace.skills_root.resolve())
        self.assertNotIn("current_result_path", exported)
        self.assertNotIn("review_result_path", exported)
        self.assertNotIn(internal_root, exported)
        self.assertNotIn(internal_root, html)

    def test_invalid_skill_id_cannot_escape_skills_root_when_reading_projection(self) -> None:
        source = self.inventory.rows[0]
        malicious_row = replace(
            source,
            trace_values={**source.trace_values, "skill_id": "../outside"},
        )
        malicious_inventory = replace(self.inventory, rows=(malicious_row,))

        outside = self.config.workspace.skills_root.parent / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "current-result.json").write_text(
            json.dumps(
                {
                    "source_row_id": malicious_row.source_row_id,
                    "skill_id": "../outside",
                    "static_status": "COMPLETED",
                    "ai_status": "COMPLETED",
                    "final_status": "COMPLETED",
                    "review_status": "COMPLETED",
                    "security_decision": "PASS",
                }
            ),
            encoding="utf-8",
        )

        report = write_live_batch_report(
            self.config,
            malicious_inventory,
            batch_id="malicious-batch",
        )
        exported = json.loads(report.current_json.read_text(encoding="utf-8"))
        skill = exported["skills"][0]
        self.assertEqual(skill["static_status"], "PENDING")
        self.assertEqual(skill["ai_status"], "PENDING")
        self.assertEqual(skill["final_status"], "PENDING")
        self.assertEqual(skill["security_decision"], "")
        self.assertEqual(report.report_status, "INTERIM")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
