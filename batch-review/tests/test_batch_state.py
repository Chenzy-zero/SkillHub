import json
import tempfile
import unittest
from pathlib import Path

from skill_batch_review.batch_state import (
    BatchStateError,
    BatchStateStore,
    Checkpoint,
    StateConflictError,
    StateTransitionError,
    make_task_key,
)


class BatchStateTests(unittest.TestCase):
    def test_atomic_state_and_legal_batch_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "batch-state.json"
            store = BatchStateStore.create(path, "batch-1", metadata={"owner": "cm"})
            self.assertEqual(store.read()["status"], "CREATED")
            store.transition_batch("VALIDATING")
            store.transition_batch("READY")
            store.transition_batch("RUNNING")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["status"], "RUNNING")
            self.assertGreaterEqual(document["revision"], 3)
            self.assertEqual([item["sequence"] for item in document["events"]], [1, 2, 3])
            self.assertFalse(list(Path(temp_dir).glob(".*.tmp")))

    def test_illegal_batch_transition_is_rejected_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = BatchStateStore.create(path, "batch-1")
            before = path.read_bytes()
            with self.assertRaises(StateTransitionError):
                store.transition_batch("COMPLETED")
            self.assertEqual(path.read_bytes(), before)

    def test_task_key_and_upsert_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = BatchStateStore.create(Path(temp_dir) / "state.json", "batch-1")
            key = make_task_key("a" * 64, "CISCO", "1.0", "policy-1", "static")
            self.assertEqual(key, make_task_key("a" * 64, "CISCO", "1.0", "policy-1", "static"))
            first = store.upsert_task(key, stage="cisco", payload={"output_ref": "evidence/cisco.json"})
            second = store.upsert_task(key, stage="cisco", payload={"output_ref": "evidence/cisco.json"})
            self.assertEqual(first, second)
            self.assertEqual(len(store.read()["events"]), 1)
            store.transition_task(key, status="RUNNING", stage="cisco")
            store.transition_task(key, status="SUCCEEDED", stage="cisco", payload={"output_ref": "evidence/cisco.json"})
            with self.assertRaises(StateTransitionError):
                store.transition_task(key, status="RUNNING", stage="cisco")

    def test_expected_state_prevents_resume_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = BatchStateStore.create(Path(temp_dir) / "state.json", "batch-1")
            with self.assertRaises(StateConflictError):
                store.transition_batch("VALIDATING", expected_status="READY")
            key = make_task_key("task")
            with self.assertRaises(StateConflictError):
                store.upsert_task(key, expected_status="RUNNING")

    def test_checkpoint_and_recovery_candidates_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = BatchStateStore.create(path, "batch-1")
            key = make_task_key("task")
            store.upsert_task(key, stage="snapshot")
            checkpoint = store.save_checkpoint(
                task_key=key,
                stage="snapshot",
                status="SUCCEEDED",
                output_refs=("evidence/manifest.json", "evidence/digest.txt"),
                resume_after="scanners",
            )
            self.assertEqual(store.get_checkpoint(key), checkpoint)
            store.transition_task(key, status="RUNNING", stage="snapshot")
            candidates = store.resumable_tasks()
            self.assertEqual([item["task_key"] for item in candidates], [key])
            reloaded = BatchStateStore.open(path, batch_id="batch-1")
            self.assertEqual(reloaded.get_checkpoint(key)["resume_after"], "scanners")

    def test_retry_limit_excludes_exhausted_errors_and_redacts_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = BatchStateStore.create(Path(temp_dir) / "state.json", "batch-1")
            key = make_task_key("task")
            store.upsert_task(key, payload={"password": "secret-value"})
            store.transition_task(key, status="ERROR", attempt=3, payload={"token": "full-token"})
            self.assertEqual(store.resumable_tasks(max_attempts=3), [])
            document = store.read()
            encoded = json.dumps(document, ensure_ascii=False)
            self.assertNotIn("secret-value", encoded)
            self.assertNotIn("full-token", encoded)
            self.assertIn("[REDACTED]", encoded)

    def test_invalid_checkpoint_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = BatchStateStore.create(Path(temp_dir) / "state.json", "batch-1")
            with self.assertRaises(BatchStateError):
                store.save_checkpoint(Checkpoint(make_task_key("task"), "snapshot", "COMPLETED"))


if __name__ == "__main__":
    unittest.main()
