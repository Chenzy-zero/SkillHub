"""Tests for the scanner-neutral review gate."""

from __future__ import annotations

import copy
import unittest

from skill_batch_review.review_policy import (
    SECURITY_BLOCK,
    SECURITY_INCOMPLETE,
    SECURITY_PASS,
    SECURITY_REVIEW_REQUIRED,
    evaluate_policy,
    normalize_findings,
)


DIGEST = "a" * 64
REVISION = "b" * 40


def scan_result(
    scanner: str,
    *,
    decision: str = "PASS",
    findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "scanner": scanner,
        "status": "COMPLETED",
        "decision": decision,
        "tool_ok": True,
        "completed": True,
        "report_complete": True,
        "skill_digest": DIGEST,
        "errors": [],
        "directory_comparison": {"unchanged": True},
        "findings": findings or [],
    }


def ai_result(*, score: int = 90) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "review_id": "review-1",
        "policy_version": "policy-1",
        "reviewed_at": "2026-08-31T08:00:00Z",
        "reviewer": {"kind": "AI", "model": "intranet-model"},
        "subject": {
            "source_revision": REVISION,
            "skill_digest_sha256": DIGEST,
        },
        "input_coverage": {
            "package_complete": True,
            "manifest_status": "COMPLETE",
            "files_expected": 3,
            "files_reviewed": 3,
            "unreadable_or_skipped_files": [],
            "static_reports": [
                {
                    "scanner": "CISCO_AI_SKILL_SCANNER",
                    "status": "COMPLETED",
                    "scanned_digest_sha256": DIGEST,
                },
                {
                    "scanner": "NVIDIA_SKILLSPECTOR",
                    "status": "COMPLETED",
                    "scanned_digest_sha256": DIGEST,
                },
            ],
            "digest_consistent": True,
            "traceability_complete": True,
        },
        "security_review": {
            "verdict": "PASS",
            "max_severity": "NONE",
            "findings": [],
        },
        "quality_review": {
            "verdict": "PASS" if score >= 70 else "FAIL",
            "score": score,
            "findings": [],
        },
    }


class ReviewPolicyTests(unittest.TestCase):
    def scans(self) -> list[dict[str, object]]:
        return [scan_result("cisco"), scan_result("skillspector")]

    def test_complete_clean_review_is_candidate_eligible(self) -> None:
        result = evaluate_policy(self.scans(), ai_result(), skill_digest=DIGEST)
        self.assertEqual(result.security_decision, SECURITY_PASS)
        self.assertEqual(result.quality_score, 90)
        self.assertTrue(result.candidate_eligible)

    def test_high_quality_never_overrides_confirmed_block(self) -> None:
        scans = self.scans()
        scans[1] = scan_result("skillspector", decision="DO_NOT_INSTALL")
        result = evaluate_policy(scans, ai_result(score=100), skill_digest=DIGEST)
        self.assertEqual(result.security_decision, SECURITY_BLOCK)
        self.assertFalse(result.candidate_eligible)
        self.assertEqual(result.quality_score, 100)

    def test_incomplete_scanner_or_digest_mismatch_is_incomplete(self) -> None:
        scans = self.scans()
        scans[0]["tool_ok"] = False
        scans[1]["skill_digest"] = "c" * 64
        result = evaluate_policy(scans, ai_result(), skill_digest=DIGEST)
        self.assertEqual(result.security_decision, SECURITY_INCOMPLETE)
        self.assertFalse(result.candidate_eligible)
        self.assertTrue(any("digest differs" in reason for reason in result.incomplete_reasons))

    def test_critical_precedes_other_incomplete_inputs(self) -> None:
        scans = self.scans()
        scans.pop()
        scans[0]["findings"] = [
            {
                "id": "critical-1",
                "category": "command_execution",
                "severity": "critical",
                "description": "unbounded command execution",
                "path": "SKILL.md",
                "line": 8,
            }
        ]
        result = evaluate_policy(scans, ai_result(), skill_digest=DIGEST)
        self.assertEqual(result.security_decision, SECURITY_BLOCK)
        self.assertTrue(result.incomplete_reasons)

    def test_medium_and_branch_conflict_require_manual_review(self) -> None:
        scans = self.scans()
        scans[0]["findings"] = [
            {
                "id": "medium-1",
                "category": "network",
                "severity": "medium",
                "description": "network destination needs confirmation",
                "path": "scripts/fetch.py",
                "line": 12,
            }
        ]
        result = evaluate_policy(
            scans,
            ai_result(),
            skill_digest=DIGEST,
            branch_content_conflict=True,
        )
        self.assertEqual(result.security_decision, SECURITY_REVIEW_REQUIRED)
        self.assertFalse(result.candidate_eligible)

    def test_stale_inventory_is_incomplete(self) -> None:
        result = evaluate_policy(
            self.scans(), ai_result(), skill_digest=DIGEST, stale_inventory=True
        )
        self.assertEqual(result.security_decision, SECURITY_INCOMPLETE)

    def test_nonblocking_snapshot_coverage_issue_requires_review(self) -> None:
        result = evaluate_policy(
            self.scans(),
            ai_result(),
            skill_digest=DIGEST,
            coverage_requires_review=True,
        )
        self.assertEqual(result.security_decision, SECURITY_REVIEW_REQUIRED)
        self.assertFalse(result.candidate_eligible)

    def test_quality_is_independent_and_threshold_bound(self) -> None:
        result = evaluate_policy(self.scans(), ai_result(score=69), skill_digest=DIGEST)
        self.assertEqual(result.security_decision, SECURITY_PASS)
        self.assertEqual(result.quality_decision, "FAIL")
        self.assertEqual(result.quality_score, 69)
        self.assertFalse(result.candidate_eligible)

    def test_findings_are_deduplicated_and_keep_all_sources(self) -> None:
        common = {
            "category": "prompt_injection",
            "description": "Ignore previous instructions",
            "path": "SKILL.md",
            "line": 4,
        }
        scans = [
            scan_result("cisco", findings=[{**common, "id": "c-1", "severity": "medium"}]),
            scan_result(
                "skillspector",
                findings=[{**common, "id": "n-1", "severity": "high"}],
            ),
        ]
        findings = normalize_findings(scans, None, skill_digest=DIGEST)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "HIGH")
        self.assertEqual(
            findings[0]["source_scanners"],
            ["CISCO_AI_SKILL_SCANNER", "NVIDIA_SKILLSPECTOR"],
        )
        self.assertEqual(len(findings[0]["source_references"]), 2)

    def test_secret_like_evidence_is_redacted(self) -> None:
        scans = self.scans()
        scans[0]["findings"] = [
            {
                "id": "secret-1",
                "category": "secrets",
                "severity": "high",
                "description": "token=plain-text-value",
                "path": "SKILL.md",
                "line": 1,
            }
        ]
        finding = normalize_findings(scans, None, skill_digest=DIGEST)[0]
        self.assertNotIn("plain-text-value", finding["evidence_summary"])
        self.assertIn("<REDACTED>", finding["evidence_summary"])

    def test_ai_block_is_not_lost_when_findings_are_empty(self) -> None:
        review = copy.deepcopy(ai_result())
        review["security_review"]["verdict"] = "BLOCK"
        result = evaluate_policy(self.scans(), review, skill_digest=DIGEST)
        self.assertEqual(result.security_decision, SECURITY_BLOCK)


if __name__ == "__main__":
    unittest.main()
