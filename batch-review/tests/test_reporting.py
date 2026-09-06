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
from skill_batch_review.html_reporting import build_html_report_payload


DIGEST = "a" * 64
REVISION = "b" * 40


def complete_record(source_row_id: str = "row-1") -> dict:
    return {
        "source_row_id": source_row_id,
        "source_row_numbers": [2],
        "skill_id": f"skill-{source_row_id}",
        "skill_name": "sample",
        "repo_name": "team/repo",
        "product_line": "product-a",
        "user_name": "Alice",
        "user_email": "alice@example.com",
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
        self.assertEqual(summary["product_line_count"], 1)
        self.assertEqual(summary["submitter_count"], 1)
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
            page = first.html.read_text(encoding="utf-8")
            self.assertIn('id="filter-repo"', page)
            self.assertIn('id="filter-product"', page)
            self.assertIn('id="filter-person"', page)
            self.assertIn("导出当前视图 CSV", page)
            self.assertIn("仓库视图", page)
            self.assertIn("提交人视图", page)

            summary = json.loads(first.summary.read_text(encoding="utf-8"))
            self.assertEqual(summary["result_record_count"], 2)
            with first.details.open("r", encoding="utf-8", newline="") as handle:
                details = list(csv.DictReader(handle))
            self.assertEqual([row["source_row_id"] for row in details], ["row-a", "row-b"])
            self.assertEqual(details[0]["skill_digest"], DIGEST)
            self.assertEqual(details[0]["skill_id"], "skill-row-a")
            self.assertEqual(details[0]["product_line"], "product-a")
            self.assertEqual(details[0]["user_email"], "alice@example.com")

            candidates = json.loads(first.candidates.read_text(encoding="utf-8"))
            self.assertEqual(len(candidates["candidates"]), 2)
            self.assertEqual(candidates["candidates"][0]["reviewed_source_revision"], REVISION)

    def test_reused_result_is_counted_and_explained_in_html(self) -> None:
        record = complete_record()
        record.update(
            {
                "reuse_status": "RESULT_REUSED",
                "reused_from_batch_id": "batch-source",
                "reused_from_task_id": "task-source",
                "comparison_method": "CANONICAL_SKILL_PACKAGE_SHA256",
                "timestamp_ignored": True,
                "reason": "Skill Root 名称相同，且规范化包内容摘要完全一致；文件时间戳不参与比较。",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_batch_reports([record], Path(temp_dir), batch_id="batch-reuse")
            summary = json.loads(paths.summary.read_text(encoding="utf-8"))
            page = paths.html.read_text(encoding="utf-8")
            self.assertEqual(summary["reused_result_count"], 1)
            self.assertIn("RESULT_REUSED", page)
            self.assertIn("batch-source", page)
            self.assertIn("忽略时间戳", page)

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

    def test_html_payload_preserves_traceable_finding_and_indexes_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence" / "batch" / "task"
            scanner = evidence / "scanners" / "cisco"
            scanner.mkdir(parents=True)
            final_result = {
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "source_scanner": "CISCO_AI_SKILL_SCANNER",
                        "source_rule_id": "RULE-7",
                        "severity": "HIGH",
                        "domain": "SECURITY",
                        "title": "Dynamic execution",
                        "description": "Untrusted command may execute.",
                        "evidence_summary": "scripts/run.py invokes a command.",
                        "recommendation": "Remove dynamic execution.",
                        "file_path": "scripts/run.py",
                        "start_line": 7,
                        "end_line": 9,
                        "fingerprint": "f" * 64,
                        "source_references": [
                            {"scanner": "cisco", "rule_id": "RULE-7"}
                        ],
                    }
                ]
            }
            (evidence / "final-result.json").write_text(
                json.dumps(final_result), encoding="utf-8"
            )
            (scanner / "raw-report.json").write_text(
                json.dumps({"raw": "scanner output"}), encoding="utf-8"
            )
            record = complete_record()
            record["evidence_ref"] = str(evidence)
            payload = build_html_report_payload(
                [record], batch_id="batch-1", evidence_root=root / "evidence"
            )
            skill = payload["skills"][0]
            finding = skill["findings"][0]
            self.assertEqual(skill["evidence_ref"], "batch/task")
            self.assertNotIn(str(root), json.dumps(payload, ensure_ascii=False))
            self.assertEqual(finding["finding_id"], "finding-1")
            self.assertEqual(finding["path"], "scripts/run.py")
            self.assertEqual(finding["start_line"], "7")
            self.assertEqual(finding["evidence_summary"], "scripts/run.py invokes a command.")
            artifacts = {item["task_path"]: item for item in skill["evidence_artifacts"]}
            self.assertIn("scanners/cisco/raw-report.json", artifacts)
            self.assertEqual(artifacts["scanners/cisco/raw-report.json"]["type"], "RAW")
            self.assertEqual(len(artifacts["scanners/cisco/raw-report.json"]["sha256"]), 64)

    def test_untrusted_report_text_cannot_break_out_of_embedded_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = root / "evidence" / "batch" / "task"
            evidence.mkdir(parents=True)
            injection = "</script><script>alert('x')</script>"
            (evidence / "final-result.json").write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "HIGH",
                                "title": injection,
                                "description": "safe evidence",
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
                batch_id="batch-xss",
                evidence_root=root / "evidence",
            )
            page = paths.html.read_text(encoding="utf-8")
            self.assertNotIn(injection, page)
            self.assertIn("\\u003c/script\\u003e", page)


if __name__ == "__main__":
    unittest.main()
