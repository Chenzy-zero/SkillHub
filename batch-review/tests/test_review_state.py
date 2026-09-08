import unittest

from skill_batch_review.models import AIReviewStatus, FinalReviewStatus, StaticReviewStatus
from skill_batch_review.review_state import (
    ReviewPhaseState,
    build_current_result,
    finding_summary,
    static_waiting_for_ai,
)


class ReviewPhaseStateTests(unittest.TestCase):
    def test_static_waiting_for_ai_is_interim_only(self) -> None:
        result = static_waiting_for_ai(
            {"skill_id": "123", "skill_name": "demo"},
            static_reports=[{"scanner": "cisco", "status": "COMPLETED"}],
            findings=[{"severity": "HIGH", "title": "x"}],
            static_security_decision="REVIEW_REQUIRED",
            evidence_ref="batch/task",
            review_policy_version="policy-1",
        )
        self.assertEqual(result["static_status"], "COMPLETED")
        self.assertEqual(result["ai_status"], "PENDING")
        self.assertEqual(result["final_status"], "PENDING")
        self.assertEqual(result["review_status"], "IN_PROGRESS")
        self.assertEqual(result["static_security_decision"], "REVIEW_REQUIRED")
        self.assertEqual(result["security_decision"], "")
        self.assertIsNone(result["quality_score"])
        self.assertEqual(result["finding_counts"]["HIGH"], 1)

    def test_pending_final_cannot_expose_final_pass(self) -> None:
        with self.assertRaises(ValueError):
            build_current_result(
                {"skill_id": "123"},
                phase=ReviewPhaseState(
                    StaticReviewStatus.COMPLETED,
                    AIReviewStatus.PENDING,
                    FinalReviewStatus.PENDING,
                ),
                security_decision="PASS",
            )

    def test_static_incomplete_must_be_final_incomplete(self) -> None:
        with self.assertRaises(ValueError):
            ReviewPhaseState(
                StaticReviewStatus.INCOMPLETE,
                AIReviewStatus.PENDING,
                FinalReviewStatus.PENDING,
            )
        valid = ReviewPhaseState(
            StaticReviewStatus.INCOMPLETE,
            AIReviewStatus.NOT_REQUIRED,
            FinalReviewStatus.INCOMPLETE,
        )
        self.assertEqual(valid.to_dict()["final_status"], "INCOMPLETE")

    def test_final_completed_requires_ai_completion_or_not_required(self) -> None:
        with self.assertRaises(ValueError):
            ReviewPhaseState(
                StaticReviewStatus.COMPLETED,
                AIReviewStatus.PENDING,
                FinalReviewStatus.COMPLETED,
            )
        valid = ReviewPhaseState(
            StaticReviewStatus.COMPLETED,
            AIReviewStatus.COMPLETED,
            FinalReviewStatus.COMPLETED,
        )
        self.assertEqual(valid.ai_status, AIReviewStatus.COMPLETED)

    def test_finding_summary_normalizes_unknown_severity_to_info(self) -> None:
        result = finding_summary(
            [
                {"severity": "critical", "title": "a"},
                {"severity": "unexpected", "title": "b"},
            ]
        )
        self.assertEqual(result["finding_counts"]["CRITICAL"], 1)
        self.assertEqual(result["finding_counts"]["INFO"], 1)
        self.assertEqual(result["max_severity"], "CRITICAL")
        self.assertEqual(result["finding_count"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
