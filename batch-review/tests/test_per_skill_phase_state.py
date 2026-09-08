from __future__ import annotations

import json
import unittest

from skill_batch_review.per_skill import finalize_skill, prepare_skill
from skill_batch_review.scanners import CiscoSkillScannerAdapter, SkillSpectorAdapter
from test_orchestrator import valid_ai_result
from test_per_skill import PerSkillWorkflowTests, ScannerRunner


class PhaseAwarePerSkillIntegrationTests(unittest.TestCase):
    """Exercise the phase projection without duplicating the existing Git fixture."""

    def setUp(self) -> None:
        self.fixture = PerSkillWorkflowTests(
            methodName="test_archives_each_skill_and_reuses_approved_content"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _adapters(self, runner: ScannerRunner):
        return {
            "cisco": CiscoSkillScannerAdapter(runner=runner, tool_version="1.0"),
            "skillspector": SkillSpectorAdapter(runner=runner, tool_version="1.0"),
        }

    def _prepare(self):
        config = self.fixture.config()
        inventory = self.fixture.inventory()
        runner = ScannerRunner()
        prepared = prepare_skill(
            config,
            batch_id="phase-batch",
            row=inventory.rows[0],
            downloader=self.fixture.downloader,
            adapters=self._adapters(runner),
        )
        return config, inventory, prepared, runner

    def test_static_prepare_persists_interim_without_final_decision(self) -> None:
        config, _, prepared, runner = self._prepare()
        self.assertTrue(prepared.requires_ai)
        self.assertEqual(runner.call_count, 2)

        skill_root = config.workspace.skills_root / "id-one"
        current_path = skill_root / "current-result.json"
        final_path = skill_root / "review-result.json"
        self.assertTrue(current_path.is_file())
        self.assertFalse(final_path.exists())

        current = json.loads(current_path.read_text(encoding="utf-8"))
        self.assertEqual(current["result_kind"], "CURRENT")
        self.assertEqual(current["static_status"], "COMPLETED")
        self.assertEqual(current["ai_status"], "PENDING")
        self.assertEqual(current["final_status"], "PENDING")
        self.assertEqual(current["review_status"], "IN_PROGRESS")
        self.assertEqual(current["static_security_decision"], "PASS")
        self.assertEqual(current["security_decision"], "")
        self.assertEqual(current["quality_decision"], "")
        self.assertIsNone(current["quality_score"])
        self.assertEqual(current["ai_review_summary"]["status"], "PENDING")

        index = json.loads(prepared.index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["phase_schema_version"], "1.0")
        self.assertEqual(index["static_status"], "COMPLETED")
        self.assertEqual(index["ai_status"], "PENDING")
        self.assertEqual(index["final_status"], "PENDING")
        self.assertEqual(index["status"], "WAITING_FOR_AI")
        self.assertEqual(index["current_result_path"], str(current_path))

    def test_finalize_promotes_interim_projection_to_completed_final(self) -> None:
        config, _, prepared, _ = self._prepare()
        ai_path = self.fixture.root / "phase-ai.json"
        ai_result = valid_ai_result(
            prepared.task_id,
            self.fixture.revision,
            prepared.snapshot.skill_digest,
        )
        for dimension in ai_result["quality_review"]["dimensions"]:
            del dimension["max_score"]
        ai_path.write_text(json.dumps(ai_result), encoding="utf-8")

        result = finalize_skill(
            config,
            index_path=prepared.index_path,
            ai_result_path=ai_path,
        )

        skill_root = config.workspace.skills_root / "id-one"
        current = json.loads(
            (skill_root / "current-result.json").read_text(encoding="utf-8")
        )
        final = json.loads(
            (skill_root / "review-result.json").read_text(encoding="utf-8")
        )
        index = json.loads(prepared.index_path.read_text(encoding="utf-8"))

        self.assertEqual(result["result_kind"], "FINAL")
        self.assertEqual(current["result_kind"], "CURRENT")
        self.assertEqual(final["result_kind"], "FINAL")
        for document in (current, final):
            self.assertEqual(document["static_status"], "COMPLETED")
            self.assertEqual(document["ai_status"], "COMPLETED")
            self.assertEqual(document["final_status"], "COMPLETED")
            self.assertEqual(document["review_status"], "COMPLETED")
            self.assertEqual(document["static_security_decision"], "PASS")
            self.assertEqual(document["security_decision"], "PASS")
        self.assertEqual(index["static_status"], "COMPLETED")
        self.assertEqual(index["ai_status"], "COMPLETED")
        self.assertEqual(index["final_status"], "COMPLETED")
        self.assertEqual(index["status"], "COMPLETED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
