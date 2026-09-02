import csv
import json
import tempfile
import unittest
from pathlib import Path

from skill_batch_review.reporting import (
    build_batch_summary,
    build_candidate_records,
    build_failure_records,
    redact,
    write_batch_reports,
)


DIGEST = "a" * 64
REVISION = "b" * 40


def complete_record(source_row_id: str = "row-1") -> dict:
    return {
        "source_row_id": source_row_id,
        "source_row_numbers": [2],
        "skill_name": "sample",
        "repo_name": "team/repo",
        "branch": "main",
        "skill_path": "skills/sample",
        "inventory_revision": REVISION,
        "source_revision": REVISION,
        "skill_last_change_revision": REVISION,
        "skill_digest": DIGEST,
        "source_selection_status": "SELECTED",
        "static_reports": [
            {
                "scanner": "CISCO_AI_SKILL_SCANNER",
                "status": "COMPLETED",
                "max_severity": "LOW",
            },
            {
                "scanner": "NVIDIA_SKILLSPECTOR",
                "status": "COMPLETED",
                "max_severity": "NONE",
            },
        ],
        "ai_review": {
            "status": "COMPLETED",
            "security_review": {"max_severity": "LOW", "verdict": "PASS"},
        },
        "security_decision": "PASS",
        "quality_review": {
            "score": 92,
            "dimensions": [{"name": "PURPOSE_AND_TRIGGER", "score": 20}],
        },
        "candidate_status": "READY_TO_EXPORT",
        "review_policy_version": "policy-1",
        "reviewed_at": "2026-08-31T08:00:00Z",
        "evidence_ref": "restricted-evidence/row-1",
    }


class ReportingTests(unittest.TestCase):
    def test_summary_counts_are_consistent(self) -> None:
        complete = complete_record()
        review = {
            "source_row_id": "row-2",
            "repo_name": "team/repo",
            "skill_name": "blocked",
            "branch": "main",
            "skill_path": "skills/blocked",
            "source_selection_status": "CONFLICT",
            "security_decision": "REVIEW_REQUIRED",
            "quality_score": 61,
            "candidate_status": "NOT_ELIGIBLE",
            "status": "TIMEOUT",
            "failure_reason": "scanner timeout; token=do-not-copy",
        }
        summary = build_batch_summary(
            [review, complete],
            batch_id="batch-1",
            input_csv_sha256="c" * 64,
            policy_version="policy-1",
        )
        self.assertEqual(summary["repository_count"], 1)
        self.assertEqual(summary["source_row_count"], 2)
        self.assertEqual(summary["result_record_count"], 2)
        self.assertEqual(summary["selected_content_version_count"], 1)
        self.assertEqual(summary["branch_conflict_count"], 1)
        self.assertEqual(summary["security_decision_counts"], {"PASS": 1, "REVIEW_REQUIRED": 1, "BLOCKED": 0, "INCOMPLETE": 0})
        self.assertEqual(summary["quality_level_distribution"]["EXCELLENT"], 1)
        self.assertEqual(summary["quality_level_distribution"]["UNQUALIFIED"], 1)
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["retry_pending_count"], 1)

    def test_reports_have_fixed_outputs_and_deterministic_content(self) -> None:
        records = [complete_record("row-b"), complete_record("row-a")]
        with tempfile.TemporaryDirectory() as temp_dir:
            first = write_batch_reports(records, Path(temp_dir) / "first", batch_id="batch-1")
            second = write_batch_reports(records, Path(temp_dir) / "second", batch_id="batch-1")
            self.assertEqual(first.as_dict().keys(), {"batch_summary", "details", "failures", "candidates", "html_report"})
            self.assertEqual(first.summary.read_bytes(), second.summary.read_bytes())
            self.assertEqual(first.details.read_bytes(), second.details.read_bytes())
            self.assertEqual(first.failures.read_bytes(), second.failures.read_bytes())
            self.assertEqual(first.candidates.read_bytes(), second.candidates.read_bytes())
            self.assertEqual(first.html.read_bytes(), second.html.read_bytes())
            self.assertIn("Skill 安全审查报告", first.html.read_text(encoding="utf-8"))

            summary = json.loads(first.summary.read_text(encoding="utf-8"))
            self.assertEqual(summary["result_record_count"], 2)
            with first.details.open("r", encoding="utf-8", newline="") as handle:
                details = list(csv.DictReader(handle))
            self.assertEqual([row["source_row_id"] for row in details], ["row-a", "row-b"])
            self.assertEqual(details[0]["skill_digest"], DIGEST)

            candidates = json.loads(first.candidates.read_text(encoding="utf-8"))
            self.assertEqual(len(candidates["candidates"]), 2)
            self.assertEqual(candidates["candidates"][0]["reviewed_source_revision"], REVISION)

    def test_failures_and_candidates_are_disjoint_derived_lists(self) -> None:
        good = complete_record("good")
        failed = complete_record("failed")
        failed["candidate_status"] = "NOT_ELIGIBLE"
        failed["status"] = "ERROR"
        failed["error_message"] = "password=do-not-copy"
        self.assertEqual([item["source_row_id"] for item in build_candidate_records([good, failed], batch_id="b")], ["good"])
        failures = build_failure_records([good, failed], batch_id="b")
        self.assertEqual([item["source_row_id"] for item in failures], ["failed"])
        self.assertNotIn("do-not-copy", json.dumps(failures))

    def test_redaction_preserves_digest_but_hides_secret_fields_and_patterns(self) -> None:
        payload = {
            "skill_digest": DIGEST,
            "password": "correct-horse-battery-staple",
            "evidence": "Authorization: Bearer abc.def.ghi; token=full-token",
        }
        redacted = redact(payload)
        self.assertEqual(redacted["skill_digest"], DIGEST)
        self.assertEqual(redacted["password"], "[REDACTED]")
        self.assertNotIn("full-token", redacted["evidence"])
        self.assertNotIn("abc.def.ghi", redacted["evidence"])

    def test_source_input_is_not_modified(self) -> None:
        original = complete_record()
        snapshot = json.dumps(original, sort_keys=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            write_batch_reports([original], Path(temp_dir), batch_id="batch-1")
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)

    def test_html_uses_only_redacted_findings_from_the_evidence_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence" / "batch" / "task"
            evidence.mkdir(parents=True)
            (evidence / "final-result.json").write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "HIGH",
                                "title": "Credential pattern",
                                "description": "token=do-not-render",
                                "path": "SKILL.md",
                                "line": 8,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            record = complete_record()
            record["evidence_ref"] = str(evidence)
            paths = write_batch_reports(
                [record],
                root / "reports",
                batch_id="batch-1",
                evidence_root=root / "evidence",
            )
            page = paths.html.read_text(encoding="utf-8")
            self.assertIn("Credential pattern", page)
            self.assertIn("[REDACTED]", page)
            self.assertNotIn("do-not-render", page)


if __name__ == "__main__":
    unittest.main()
