from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_batch_review import approval_policy
from skill_batch_review.indexed_html_reporting import write_html_report
from skill_batch_review.risk_scoring import calculate_security_score, scoring_rules


def finding(
    *,
    source: str,
    severity: str,
    confidence: str,
    category: str = "FILES_AND_SECRETS",
    path: str = "config.yaml",
    rule: str = "CFG-1",
):
    return {
        "domain": "SECURITY",
        "source_scanner": source,
        "source_scanners": [source],
        "severity": severity,
        "confidence": confidence,
        "category": category,
        "file_path": path,
        "source_rule_id": rule,
    }


class SecurityRiskScoringTests(unittest.TestCase):
    def test_repeated_low_confidence_cisco_findings_decay_heavily(self) -> None:
        findings = [
            finding(
                source="CISCO_AI_SKILL_SCANNER",
                severity="MEDIUM",
                confidence="LOW",
                path=f"config-{index}.yaml",
            )
            for index in range(20)
        ]
        result = calculate_security_score(findings)
        self.assertGreaterEqual(result.security_score, 90)
        self.assertFalse(result.hard_block)
        self.assertEqual(result.deductions[0]["repetition_factor"], 1.0)
        self.assertEqual(result.deductions[1]["repetition_factor"], 0.5)
        self.assertEqual(result.deductions[2]["repetition_factor"], 0.25)
        self.assertEqual(result.deductions[3]["repetition_factor"], 0.10)

    def test_ai_high_confidence_critical_is_hard_block(self) -> None:
        result = calculate_security_score(
            [finding(source="AI_REVIEW", severity="CRITICAL", confidence="HIGH")]
        )
        self.assertTrue(result.hard_block)
        self.assertEqual(result.security_score, 0)
        self.assertEqual(result.risk_deduction, 100)

    def test_cross_source_same_behavior_has_higher_weight_than_scanner_only(self) -> None:
        scanner_only = calculate_security_score(
            [finding(source="CISCO_AI_SKILL_SCANNER", severity="HIGH", confidence="HIGH")]
        )
        corroborated = calculate_security_score(
            [
                finding(source="CISCO_AI_SKILL_SCANNER", severity="HIGH", confidence="HIGH"),
                finding(source="AI_REVIEW", severity="HIGH", confidence="HIGH"),
            ]
        )
        self.assertGreater(corroborated.risk_deduction, scanner_only.risk_deduction)
        self.assertEqual(corroborated.deductions[0]["confirmation_status"], "CORROBORATED")

    def test_scanner_review_signal_does_not_force_manual_review(self) -> None:
        base = mock.Mock()
        base.security_findings = (
            finding(source="CISCO_AI_SKILL_SCANNER", severity="MEDIUM", confidence="LOW"),
        )
        base.quality_decision = "PASS"
        base.quality_score = 90
        base.quality_threshold = 70
        base.quality_eligible = True
        base.candidate_eligible = False
        base.findings = base.security_findings
        base.quality_findings = ()
        base.incomplete_reasons = ()
        base.review_reasons = ("CISCO_AI_SKILL_SCANNER requires review",)
        with mock.patch.object(approval_policy, "_LEGACY_EVALUATE_POLICY", return_value=base):
            result = approval_policy.evaluate_policy([], {"security_review": {"verdict": "PASS"}})
        self.assertEqual(result.security_decision, "PASS")
        self.assertTrue(result.candidate_eligible)
        self.assertEqual(result.review_reasons, ())

    def test_rules_are_explicit_and_stable(self) -> None:
        rules = scoring_rules()
        self.assertEqual(rules["start_score"], 100)
        self.assertEqual(rules["pass_threshold"], 60)
        self.assertEqual(rules["severity_deduction"]["CRITICAL"], 50.0)
        self.assertEqual(rules["source_factor"]["CISCO_ONLY"], 0.45)
        self.assertEqual(rules["repetition_factor"]["fourth_and_later"], 0.10)


class SecurityScoreHtmlTests(unittest.TestCase):
    def test_html_contains_rules_and_skill_deduction_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.html"
            record = {
                "source_row_id": "row-1",
                "skill_id": "skill-1",
                "skill_name": "demo",
                "repo_name": "repo",
                "branch": "main",
                "skill_path": "demo",
                "review_status": "COMPLETED",
                "final_status": "COMPLETED",
                "security_decision": "PASS",
                "quality_decision": "PASS",
                "quality_score": 90,
                "security_score": 96,
                "security_risk_deduction": 4,
                "security_risk_level": "LOW",
                "security_hard_block": False,
                "security_scoring_rules_version": "security-score-v1",
                "security_deductions": [
                    {
                        "category": "FILES_AND_SECRETS",
                        "severity": "MEDIUM",
                        "sources": ["CISCO_AI_SKILL_SCANNER"],
                        "base_deduction": 10.0,
                        "confidence_factor": 0.25,
                        "source_factor": 0.45,
                        "repetition_factor": 1.0,
                        "deduction": 1.13,
                        "path": "config.yaml",
                    }
                ],
            }
            write_html_report([record], output, batch_id="batch-1")
            page = output.read_text(encoding="utf-8")
        self.assertIn("自动审批与安全扣分规则", page)
        self.assertIn("严重度基础扣分", page)
        self.assertIn("重复告警衰减", page)
        self.assertIn('"security_score":96', page)
        self.assertIn('"security_risk_deduction":4', page)
        self.assertIn("扣分明细", page)


if __name__ == "__main__":
    unittest.main()
