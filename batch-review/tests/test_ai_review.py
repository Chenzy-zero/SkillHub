import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from skill_batch_review.ai_review import (
    AIReviewExpectation,
    AIReviewSourceMetadata,
    AIReviewValidationError,
    build_ai_review_handoff,
    load_and_validate_ai_review_result,
    validate_ai_review_result,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / ".agents/skills/skill-security-review/references/review-result.schema.json"
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
            "files_expected": 3,
            "files_reviewed": 3,
            "unreadable_or_skipped_files": [],
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
        return validate_ai_review_result(
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

    def test_missing_weights_are_filled_and_dimensions_ordered_without_mutation(self):
        payload = valid_result()
        for dimension in payload["quality_review"]["dimensions"]:
            del dimension["max_score"]
        payload["quality_review"]["dimensions"].reverse()
        original = copy.deepcopy(payload)
        result = self.validate(payload)
        self.assertEqual(result, valid_result())
        self.assertEqual(payload, original)

    def test_loader_preserves_raw_evidence_and_supports_old_required_weight_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = valid_result()
            for dimension in payload["quality_review"]["dimensions"]:
                del dimension["max_score"]
            raw = json.dumps(payload).encode("utf-8")
            source = root / "raw.json"
            source.write_bytes(raw)
            schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
            schema["$defs"]["qualityDimension"]["required"].append("max_score")
            old_schema = root / "old-schema.json"
            old_schema.write_text(json.dumps(schema), encoding="utf-8")
            result = load_and_validate_ai_review_result(source, schema_path=old_schema)
            self.assertEqual(result, valid_result())
            self.assertEqual(source.read_bytes(), raw)

    def test_normalization_does_not_hide_invalid_review_values(self):
        for invalid in (None, "20", 99):
            with self.subTest(max_score=invalid):
                payload = valid_result()
                payload["quality_review"]["dimensions"][0]["max_score"] = invalid
                with self.assertRaises(AIReviewValidationError):
                    self.validate(payload)
        for change in ("duplicate", "unknown", "overscore", "missing_score", "missing_dimension"):
            with self.subTest(change=change):
                payload = valid_result()
                dimensions = payload["quality_review"]["dimensions"]
                for dimension in dimensions:
                    del dimension["max_score"]
                if change == "duplicate":
                    dimensions[1]["name"] = dimensions[0]["name"]
                elif change == "unknown":
                    dimensions[0]["name"] = "UNKNOWN"
                elif change == "overscore":
                    dimensions[0]["score"] = 21
                    payload["quality_review"]["score"] = 93
                elif change == "missing_score":
                    del dimensions[0]["score"]
                else:
                    dimensions.pop()
                with self.assertRaises(AIReviewValidationError):
                    self.validate(payload)

    def test_rejects_unknown_schema_field(self):
        payload = valid_result()
        payload["unexpected"] = True
        with self.assertRaises(AIReviewValidationError):
            self.validate(payload)

    def test_incomplete_package_cannot_pass(self):
        payload = valid_result()
        payload["input_coverage"]["package_complete"] = False
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
                result_schema_path=SCHEMA,
            )
            self.assertEqual(context["subject"]["skill_digest_sha256"], DIGEST)
            self.assertEqual(context["package_summary"]["files_expected"], 0)
            self.assertNotIn("static_reports", context)
            self.assertNotIn("package_manifest_path", context)
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
                    result_schema_path=SCHEMA,
                )


if __name__ == "__main__":
    unittest.main()
