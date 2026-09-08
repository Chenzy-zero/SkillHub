from __future__ import annotations

from skill_batch_review.indexed_html_reporting import write_html_report
from skill_batch_review.models import AIReviewStatus, FinalReviewStatus, StaticReviewStatus
from skill_batch_review.overall_decision import (
    canonical_security_decision,
    derive_overall_decision,
    overall_fields,
)
from skill_batch_review.reporting import DETAIL_FIELDS, build_detail_rows
from skill_batch_review.review_state import ReviewPhaseState, build_current_result


def test_overall_decision_matrix():
    cases = [
        ({"final_status": "PENDING", "security_decision": "", "quality_decision": ""}, "PENDING"),
        ({"final_status": "INCOMPLETE", "security_decision": "INCOMPLETE", "quality_decision": "INCOMPLETE"}, "INCOMPLETE"),
        ({"final_status": "COMPLETED", "security_decision": "BLOCK", "quality_decision": "PASS"}, "REJECTED"),
        ({"final_status": "COMPLETED", "security_decision": "REVIEW_REQUIRED", "quality_decision": "PASS"}, "MANUAL_REVIEW"),
        ({"final_status": "COMPLETED", "security_decision": "PASS", "quality_decision": "FAIL"}, "REJECTED"),
        ({"final_status": "COMPLETED", "security_decision": "PASS", "quality_decision": "PASS", "candidate_eligible": True}, "APPROVED"),
        ({"final_status": "COMPLETED", "security_decision": "PASS", "quality_decision": "PASS", "candidate_eligible": False}, "REJECTED"),
    ]
    for values, expected in cases:
        assert derive_overall_decision(**values) == expected


def test_machine_codes_are_canonical_and_chinese_labels_are_deterministic():
    fields = overall_fields(
        final_status="COMPLETED",
        security_decision="BLOCK",
        quality_decision="PASS",
        candidate_eligible=False,
    )
    assert canonical_security_decision("BLOCK") == "BLOCKED"
    assert fields["security_decision"] == "BLOCKED"
    assert fields["security_decision_zh"] == "安全阻断"
    assert fields["quality_decision_zh"] == "质量通过"
    assert fields["overall_decision"] == "REJECTED"
    assert fields["overall_decision_zh"] == "不通过"
    assert fields["overall_reason_codes"] == ["SECURITY_BLOCKED"]


def test_current_result_persists_authoritative_overall_state():
    pending = build_current_result(
        {"skill_id": "skill-1"},
        phase=ReviewPhaseState(
            StaticReviewStatus.COMPLETED,
            AIReviewStatus.PENDING,
            FinalReviewStatus.PENDING,
        ),
        static_security_decision="PASS",
    )
    assert pending["overall_decision"] == "PENDING"
    assert pending["overall_decision_zh"] == "审查中"
    assert pending["candidate_eligible"] is None

    rejected = build_current_result(
        {"skill_id": "skill-1"},
        phase=ReviewPhaseState(
            StaticReviewStatus.COMPLETED,
            AIReviewStatus.COMPLETED,
            FinalReviewStatus.COMPLETED,
        ),
        static_security_decision="PASS",
        security_decision="PASS",
        quality_decision="FAIL",
        quality_score=60,
    )
    assert rejected["overall_decision"] == "REJECTED"
    assert rejected["overall_decision_zh"] == "不通过"
    assert rejected["security_decision_zh"] == "安全通过"
    assert rejected["quality_decision_zh"] == "质量不通过"
    assert rejected["candidate_eligible"] is False


def test_standard_report_rows_export_overall_fields():
    rows = build_detail_rows(
        [
            {
                "source_row_id": "row-1",
                "skill_id": "skill-1",
                "skill_name": "demo",
                "repo_name": "repo",
                "final_status": "COMPLETED",
                "review_status": "COMPLETED",
                "security_decision": "PASS",
                "quality_decision": "FAIL",
                "quality_score": 60,
                "candidate_eligible": False,
            }
        ],
        batch_id="batch-1",
    )
    assert "overall_decision" in DETAIL_FIELDS
    assert "overall_decision_zh" in DETAIL_FIELDS
    assert rows[0]["overall_decision"] == "REJECTED"
    assert rows[0]["overall_decision_zh"] == "不通过"
    assert rows[0]["security_decision"] == "PASS"
    assert rows[0]["quality_decision"] == "FAIL"


def test_html_uses_overall_decision_as_operator_facing_conclusion(tmp_path):
    output = write_html_report(
        [
            {
                "source_row_id": "row-1",
                "skill_id": "skill-1",
                "skill_name": "demo",
                "repo_name": "repo",
                "final_status": "COMPLETED",
                "review_status": "COMPLETED",
                "security_decision": "PASS",
                "quality_decision": "FAIL",
                "quality_score": 60,
                "candidate_eligible": False,
            }
        ],
        tmp_path / "report.html",
        batch_id="batch-overall",
    )
    page = output.read_text(encoding="utf-8")
    assert '<label for="filter-decision">最终结论</label>' in page
    assert "{label:'最终结论',render:item=>overallBadge(item)}" in page
    assert '"overall_decision":"REJECTED"' in page
    assert '"overall_decision_zh":"不通过"' in page
    assert '"security_decision":"PASS"' in page
    assert '"quality_decision":"FAIL"' in page
