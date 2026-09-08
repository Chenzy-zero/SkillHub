from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skill_batch_review.dispatch_lease import DispatchLeaseError, allocate_dispatch


class DispatchLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "ai-dispatch-state.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def queue(self, values):
        return [
            {
                "task_id": f"task-{index}",
                "handoff": str(self.root / f"handoff-{index}.json"),
                "expected_result": str(self.root / f"result-{index}.json"),
                "review_size_bytes": size,
            }
            for index, size in values
        ]

    def test_initial_session_leases_only_parallel_slots_largest_first(self) -> None:
        queue = self.queue([(1, 10), (2, 100), (3, 50), (4, 5)])
        allocation = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=queue,
            max_parallel=2,
        )
        self.assertTrue(allocation.session_id)
        self.assertEqual([item["task_id"] for item in allocation.new_items], ["task-2", "task-3"])
        self.assertEqual(set(allocation.in_flight), {"task-2", "task-3"})
        self.assertEqual(allocation.queued_count, 2)

    def test_completion_event_releases_one_slot_and_refills_one(self) -> None:
        queue = self.queue([(1, 100), (2, 80), (3, 60), (4, 40)])
        first = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=queue,
            max_parallel=2,
        )
        # task-1 completed and was removed from the regenerated trusted queue.
        remaining_queue = [item for item in queue if item["task_id"] != "task-1"]
        second = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=remaining_queue,
            max_parallel=2,
            session_id=first.session_id,
            completed_task_id="task-1",
        )
        self.assertEqual([item["task_id"] for item in second.new_items], ["task-3"])
        self.assertEqual(set(second.in_flight), {"task-2", "task-3"})

    def test_result_file_or_queue_disappearance_does_not_release_without_event(self) -> None:
        queue = self.queue([(1, 100), (2, 80), (3, 60)])
        first = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=queue,
            max_parallel=2,
        )
        # The queue no longer lists task-1 (for example its expected file now
        # exists), but no completion event has been acknowledged yet.
        remaining_queue = [item for item in queue if item["task_id"] != "task-1"]
        second = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=remaining_queue,
            max_parallel=2,
            session_id=first.session_id,
        )
        self.assertEqual(second.new_items, ())
        self.assertEqual(set(second.in_flight), {"task-1", "task-2"})

    def test_new_session_releases_stale_inflight_leases_for_recovery(self) -> None:
        queue = self.queue([(1, 100), (2, 80), (3, 60)])
        first = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=queue,
            max_parallel=2,
        )
        recovered = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=queue,
            max_parallel=2,
        )
        self.assertNotEqual(recovered.session_id, first.session_id)
        self.assertEqual([item["task_id"] for item in recovered.new_items], ["task-1", "task-2"])

    def test_stale_session_and_unknown_completion_are_rejected(self) -> None:
        queue = self.queue([(1, 100), (2, 80)])
        first = allocate_dispatch(
            self.state_path,
            batch_id="batch-1",
            queue_items=queue,
            max_parallel=1,
        )
        with self.assertRaisesRegex(DispatchLeaseError, "DISPATCH_SESSION_MISMATCH"):
            allocate_dispatch(
                self.state_path,
                batch_id="batch-1",
                queue_items=queue,
                max_parallel=1,
                session_id="other-session",
                completed_task_id="task-1",
            )
        with self.assertRaisesRegex(DispatchLeaseError, "COMPLETION_NOT_IN_FLIGHT"):
            allocate_dispatch(
                self.state_path,
                batch_id="batch-1",
                queue_items=queue,
                max_parallel=1,
                session_id=first.session_id,
                completed_task_id="task-2",
            )


if __name__ == "__main__":
    unittest.main()
