import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from skill_batch_review.ai_review import (
    AIReviewExpectation,
    AIReviewSourceMetadata,
    AIReviewValidationError,
    build_ai_review_handoff,
    validate_ai_review_result,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPO_ROOT / ".claude/skills/skill-security-review/references/review-result.schema.json"
DIGEST = "a" * 64
REVISION = "b" * 40


def valid_result():
    dimensions = [
        ("PURPOSE_AND_TRIGGER", 18, 20),
        ("INSTRUCTION_CLARITY", 22, 25),
        ("SCOPE_AND_PERMISSION_FIT", 13, 15),
        ("ROBUSTNESS_AND_BOUNDARIES", 18, 20),
        ("MAINTAINABILITY_AND_VERIFIABILITY", 19, 20),
    ]
    return {
        "schema_version": "1.0",
        "review_id": "review-1",
        "policy_version": "policy-1",
        "reviewed_at": "2026-08-31T08:00:00Z",
        "reviewer": {"kind": "AI", "model": "intranet-model"},
        "subject": {
            "skill_name": "sample",
            "repo_name": "team/repo",
            "branch": "main",
            "skill_path": "skills/sample",
            "inventory_revision": REVISION,
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
                    "tool_version": "1.0",
                    "rules_or_config_version": "policy-1",
                    "scanned_digest_sha256": DIGEST,
                    "report_path": "reports/cisco.json",
                },
                {
                    "scanner": "NVIDIA_SKILLSPECTOR",
                    "status": "COMPLETED",
                    "tool_version": "1.0",
                    "rules_or_config_version": "policy-1",
                    "scanned_digest_sha256": DIGEST,
                    "report_path": "reports/skillspector.json",
                },
            ],
            "digest_consistent": True,
            "traceability_complete": True,
            "limitations": [],
        },
        "security_review": {
            "verdict": "PASS",
            "max_severity": "NONE",
            "summary": "No blocking issue found by the static review.",
            "findings": [],
        },
        "quality_review": {
            "basis": "STATIC_PACKAGE_REVIEW",
            "verdict": "PASS",
            "score": 90,
            "summary": "Clear and maintainable.",
            "dimensions": [
                {
                    "name": name,
                    "anchor": "STRONG",
                    "score": score,
                    "max_score": maximum,
                    "reason": "Evidence reviewed.",
                }
                for name, score, maximum in dimensions
            ],
            "findings": [],
        },
        "overall": {
            "disposition": "APPROVE_CANDIDATE",
            "private_candidate_eligible": True,
            "reasons": ["All required reviews completed."],
        },
    }


class AIReviewValidationTests(unittest.TestCase):
    def validate(self, payload):
        validate_ai_review_result(
            payload,
            schema_path=SCHEMA,
            expectation=AIReviewExpectation(
                skill_digest_sha256=DIGEST,
                source_revision=REVISION,
                review_id="review-1",
                policy_version="policy-1",
            ),
        )

    def test_accepts_complete_consistent_result(self):
        self.validate(valid_result())

    def test_rejects_unknown_schema_field(self):
        payload = valid_result()
        payload["unexpected"] = True
        with self.assertRaises(AIReviewValidationError):
            self.validate(payload)

    def test_incomplete_static_report_cannot_pass(self):
        payload = valid_result()
        payload["input_coverage"]["static_reports"][0]["status"] = "TIMEOUT"
        with self.assertRaisesRegex(AIReviewValidationError, "must be INCOMPLETE"):
            self.validate(payload)

    def test_score_must_equal_dimension_sum(self):
        payload = valid_result()
        payload["quality_review"]["score"] = 89
        with self.assertRaisesRegex(AIReviewValidationError, "dimension score sum"):
            self.validate(payload)

    def test_frozen_digest_must_match(self):
        payload = copy.deepcopy(valid_result())
        payload["subject"]["skill_digest_sha256"] = "c" * 64
        for report in payload["input_coverage"]["static_reports"]:
            report["scanned_digest_sha256"] = "c" * 64
        with self.assertRaisesRegex(AIReviewValidationError, "frozen package"):
            self.validate(payload)

    def test_disposition_precedence_is_enforced(self):
        payload = valid_result()
        payload["security_review"]["verdict"] = "REVIEW_REQUIRED"
        with self.assertRaisesRegex(AIReviewValidationError, "MANUAL_REVIEW"):
            self.validate(payload)

    def test_handoff_requires_two_successful_digest_bound_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_root = root / "skill"
            skill_root.mkdir()
            snapshot = SimpleNamespace(
                skill_digest=DIGEST,
                source_revision=REVISION,
                snapshot_path=skill_root,
                coverage_complete=True,
            )
            version = SimpleNamespace(version="1.0")
            scans = [
                SimpleNamespace(
                    scanner=name,
                    status="COMPLETED",
                    completed=True,
                    tool_ok=True,
                    skill_digest=DIGEST,
                    tool_version=version,
                    config_digest=f"{name}-config",
                    raw_report_path=str(root / f"{name}.json"),
                    report_path=None,
                )
                for name in ("cisco", "skillspector")
            ]
            context = build_ai_review_handoff(
                snapshot=snapshot,
                scans=scans,
                source=AIReviewSourceMetadata(
                    skill_name="sample",
                    repo_name="team/repo",
                    branch="main",
                    skill_path="skills/sample",
                    inventory_revision=REVISION,
                ),
                review_id="review-1",
                policy_version="policy-1",
                assigned_reviewed_at="2026-08-31T08:00:00Z",
                reviewer_model="intranet-model",
                manifest_path=root / "manifest.json",
                result_schema_path=SCHEMA,
                result_output_path=root / "ai-result.json",
            )
            self.assertEqual(context["subject"]["skill_digest_sha256"], DIGEST)
            self.assertEqual(context["execution_boundary"]["allowed_tools"], ["Read", "Glob", "Grep"])

            scans[0].tool_ok = False
            with self.assertRaisesRegex(AIReviewValidationError, "not complete"):
                build_ai_review_handoff(
                    snapshot=snapshot,
                    scans=scans,
                    source=AIReviewSourceMetadata("sample", "team/repo", "main", "skills/sample", REVISION),
                    review_id="review-1",
                    policy_version="policy-1",
                    assigned_reviewed_at="2026-08-31T08:00:00Z",
                    reviewer_model="intranet-model",
                    manifest_path=root / "manifest.json",
                    result_schema_path=SCHEMA,
                    result_output_path=root / "ai-result.json",
                )


if __name__ == "__main__":
    unittest.main()
