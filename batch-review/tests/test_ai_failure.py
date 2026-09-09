from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skill_batch_review import ai_failure


class AIFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = SimpleNamespace()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _state(self):
        return {
            "batch_id": "batch-1",
            "ai_queue_mode": "batch_wide_v2",
            "status": "WAITING_FOR_AI",
            "items": [
                {
                    "task_id": "task-one",
                    "status": "WAITING_FOR_AI",
                    "index_path": str(self.root / "index-one.json"),
                },
                {
                    "task_id": "task-two",
                    "status": "WAITING_FOR_AI",
                    "index_path": str(self.root / "index-two.json"),
                },
            ],
        }

    def _live(self):
        html = self.root / "report.html"
        html.write_text("html", encoding="utf-8")
        return SimpleNamespace(report_status="INTERIM", paths=SimpleNamespace(html=html))

    def test_failed_reviewer_only_fails_that_task_and_keeps_batch_moving(self) -> None:
        state = self._state()
        remaining = [state["items"][1]]
        with (
            mock.patch.object(ai_failure.batch_launcher, "_load_state", return_value=state),
            mock.patch.object(ai_failure.batch_launcher, "_inventory", return_value=object()),
            mock.patch.object(ai_failure.batch_launcher, "_waiting_items", side_effect=[remaining, remaining]),
            mock.patch.object(ai_failure.batch_launcher, "_activate_batch_queue") as activate,
            mock.patch.object(ai_failure, "_finalize_failed_skill") as finalize,
            mock.patch.object(
                ai_failure,
                "write_skill_result_tables",
                return_value=(self.root / "results.csv", self.root / "results.json"),
            ),
            mock.patch.object(ai_failure, "write_live_batch_report", return_value=self._live()),
        ):
            result = ai_failure.fail_waiting_ai_task(
                self.config,
                batch_id="batch-1",
                task_id="task-one",
                failure_code="AI_REVIEW_TIMEOUT",
                failure_reason="lease expired",
            )

        finalize.assert_called_once()
        self.assertEqual(state["items"][0]["status"], "COMPLETE")
        self.assertEqual(state["items"][0]["ai_import_status"], "FAILED")
        self.assertEqual(state["items"][0]["ai_failure_code"], "AI_REVIEW_TIMEOUT")
        self.assertEqual(state["items"][1]["status"], "WAITING_FOR_AI")
        activate.assert_called_once_with(self.config, state, remaining)
        self.assertEqual(result.failure_code, "AI_REVIEW_TIMEOUT")
        self.assertEqual(result.remaining_ai, 1)

    def test_last_failed_reviewer_allows_batch_to_finalize(self) -> None:
        state = self._state()
        state["items"][1]["status"] = "COMPLETE"
        with (
            mock.patch.object(ai_failure.batch_launcher, "_load_state", return_value=state),
            mock.patch.object(ai_failure.batch_launcher, "_inventory", return_value=object()),
            mock.patch.object(ai_failure.batch_launcher, "_waiting_items", return_value=[]),
            mock.patch.object(ai_failure.batch_launcher, "_save") as save,
            mock.patch.object(ai_failure.batch_launcher, "_prepare_next") as prepare_next,
            mock.patch.object(ai_failure, "_finalize_failed_skill"),
            mock.patch.object(
                ai_failure,
                "write_skill_result_tables",
                return_value=(self.root / "results.csv", self.root / "results.json"),
            ),
            mock.patch.object(ai_failure, "write_live_batch_report", return_value=self._live()),
        ):
            ai_failure.fail_waiting_ai_task(
                self.config,
                batch_id="batch-1",
                task_id="task-one",
                failure_code="AI_REVIEW_AGENT_FAILED",
                failure_reason="native agent failed",
            )

        self.assertEqual(state["items"][0]["status"], "COMPLETE")
        self.assertEqual(state["status"], "READY")
        save.assert_called_once()
        prepare_next.assert_called_once_with(self.config, state)


if __name__ == "__main__":
    unittest.main()
