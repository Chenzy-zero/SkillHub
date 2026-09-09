from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "review_watchdog.py"
spec = importlib.util.spec_from_file_location("review_watchdog_test_module", TOOL)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load {TOOL}")
review_watchdog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = review_watchdog
spec.loader.exec_module(review_watchdog)


class ReviewWatchdogTests(unittest.TestCase):
    def test_windows_project_python_launcher_reuses_bootstrap(self) -> None:
        launcher = ROOT / "pytool.cmd"
        self.assertTrue(launcher.is_file())
        content = launcher.read_text(encoding="utf-8")
        self.assertIn("tools\\resolve_python.cmd", content)
        self.assertIn("SKILL_REVIEW_RESOLVED_PYTHON", content)

    def test_five_stale_reviewers_are_detected_together(self) -> None:
        leased_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        leases = {
            f"task-{index}": {"task_id": f"task-{index}", "leased_at": leased_at}
            for index in range(5)
        }
        stale = review_watchdog._stale_task_ids(leases, timeout_seconds=1200)
        self.assertEqual(stale, tuple(f"task-{index}" for index in range(5)))

    def test_recent_reviewer_is_not_timed_out(self) -> None:
        leases = {
            "task-one": {
                "task_id": "task-one",
                "leased_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        }
        self.assertEqual(review_watchdog._stale_task_ids(leases, timeout_seconds=1200), ())

    def test_watchdog_tick_fails_all_stale_tasks_then_refills_once(self) -> None:
        leased_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        leases = {
            f"task-{index}": {"task_id": f"task-{index}", "leased_at": leased_at}
            for index in range(5)
        }
        config = SimpleNamespace(workspace=SimpleNamespace(manifest_root=Path("manifests")))
        failed = [
            {"task_id": f"task-{index}", "failure_code": "AI_REVIEW_TIMEOUT"}
            for index in range(5)
        ]
        output = StringIO()
        with (
            mock.patch.object(review_watchdog, "_operator_context", return_value=(config, "batch-1")),
            mock.patch.object(review_watchdog, "_load_live_leases", return_value=leases),
            mock.patch.object(review_watchdog, "_fail_and_release", side_effect=failed) as fail,
            mock.patch.object(
                review_watchdog,
                "_refill",
                return_value={"exit_code": 0, "next_action": "VIEW_RESULTS", "ai_dispatch": None},
            ) as refill,
            redirect_stdout(output),
        ):
            code = review_watchdog.main(
                [
                    "tick",
                    "--dispatch-session",
                    "session-1",
                    "--timeout-seconds",
                    "1200",
                    "--ai-parallel",
                    "5",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(fail.call_count, 5)
        refill.assert_called_once_with(session_id="session-1", parallel=5)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["watchdog"]["failed_count"], 5)
        self.assertEqual(payload["next_action"], "VIEW_RESULTS")


if __name__ == "__main__":
    unittest.main()
