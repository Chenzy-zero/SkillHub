from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skill_batch_review import completion_import
from skill_batch_review.completion_import import CompletionImportError


class CompletionImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = SimpleNamespace()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def state(self):
        result_one = self.root / "result-one.json"
        result_two = self.root / "result-two.json"
        result_one.write_text("{}\n", encoding="utf-8")
        result_two.write_text("{}\n", encoding="utf-8")
        return {
            "batch_id": "batch-1",
            "ai_queue_mode": "batch_wide_v2",
            "status": "WAITING_FOR_AI",
            "items": [
                {
                    "task_id": "task-one",
                    "status": "WAITING_FOR_AI",
                    "index_path": str(self.root / "index-one.json"),
                    "ai_result_path": str(result_one),
                },
                {
                    "task_id": "task-two",
                    "status": "WAITING_FOR_AI",
                    "index_path": str(self.root / "index-two.json"),
                    "ai_result_path": str(result_two),
                },
            ],
        }

    def live(self):
        html = self.root / "report.html"
        html.write_text("html", encoding="utf-8")
        return SimpleNamespace(report_status="INTERIM", paths=SimpleNamespace(html=html))

    def test_import_finalizes_only_completed_task_and_refreshes_projection(self) -> None:
        state = self.state()
        remaining = [state["items"][1]]
        with (
            mock.patch.object(completion_import.batch_launcher, "_load_state", return_value=state),
            mock.patch.object(completion_import.batch_launcher, "_inventory", return_value=object()),
            mock.patch.object(completion_import.batch_launcher, "_waiting_items", return_value=remaining),
            mock.patch.object(completion_import.batch_launcher, "_activate_batch_queue") as activate,
            mock.patch.object(completion_import.batch_launcher, "_save"),
            mock.patch.object(completion_import, "finalize_skill") as finalize,
            mock.patch.object(
                completion_import,
                "write_skill_result_tables",
                return_value=(self.root / "results.csv", self.root / "results.json"),
            ) as tables,
            mock.patch.object(completion_import, "write_live_batch_report", return_value=self.live()) as live,
        ):
            result = completion_import.import_completed_task(
                self.config,
                batch_id="batch-1",
                task_id="task-one",
            )

        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.kwargs["index_path"], self.root / "index-one.json")
        self.assertEqual(state["items"][0]["status"], "COMPLETE")
        self.assertEqual(state["items"][0]["ai_import_status"], "COMPLETED")
        self.assertEqual(state["items"][1]["status"], "WAITING_FOR_AI")
        tables.assert_called_once()
        live.assert_called_once()
        activate.assert_called_once_with(self.config, state, remaining)
        self.assertEqual(result.status, "IMPORTED")

    def test_malformed_result_records_only_that_task_and_does_not_refresh_others(self) -> None:
        state = self.state()
        with (
            mock.patch.object(completion_import.batch_launcher, "_load_state", return_value=state),
            mock.patch.object(completion_import.batch_launcher, "_save") as save,
            mock.patch.object(completion_import, "finalize_skill", side_effect=ValueError("bad schema")),
            mock.patch.object(completion_import, "write_skill_result_tables") as tables,
            mock.patch.object(completion_import, "write_live_batch_report") as live,
            mock.patch.object(completion_import.batch_launcher, "_activate_batch_queue") as activate,
        ):
            with self.assertRaisesRegex(CompletionImportError, "AI_IMPORT_FAILED\[task-one\]"):
                completion_import.import_completed_task(
                    self.config,
                    batch_id="batch-1",
                    task_id="task-one",
                )

        self.assertEqual(state["items"][0]["status"], "WAITING_FOR_AI")
        self.assertEqual(state["items"][0]["ai_import_status"], "FAILED")
        self.assertIn("bad schema", state["items"][0]["last_import_error"])
        self.assertEqual(state["items"][1]["status"], "WAITING_FOR_AI")
        save.assert_called_once()
        tables.assert_not_called()
        live.assert_not_called()
        activate.assert_not_called()

    def test_already_completed_task_is_idempotent_and_not_finalized_twice(self) -> None:
        state = self.state()
        state["items"][0]["status"] = "COMPLETE"
        with (
            mock.patch.object(completion_import.batch_launcher, "_load_state", return_value=state),
            mock.patch.object(completion_import.batch_launcher, "_waiting_items", return_value=[state["items"][1]]),
            mock.patch.object(completion_import, "finalize_skill") as finalize,
        ):
            result = completion_import.import_completed_task(
                self.config,
                batch_id="batch-1",
                task_id="task-one",
            )
        self.assertEqual(result.status, "ALREADY_IMPORTED")
        finalize.assert_not_called()

    def test_recovery_continues_after_one_malformed_orphan_result(self) -> None:
        state = self.state()
        outcomes = [
            CompletionImportError("bad task-one"),
            SimpleNamespace(status="IMPORTED"),
        ]
        with (
            mock.patch.object(completion_import.batch_launcher, "_load_state", return_value=state),
            mock.patch.object(completion_import, "import_completed_task", side_effect=outcomes) as importer,
        ):
            recovered = completion_import.recover_ready_results(
                self.config,
                batch_id="batch-1",
            )
        self.assertEqual(importer.call_count, 2)
        self.assertEqual(recovered.imported, ("task-two",))
        self.assertIn("task-one", recovered.failed)


if __name__ == "__main__":
    unittest.main()
