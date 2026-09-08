import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools/review_assistant.py"
spec = importlib.util.spec_from_file_location("review_assistant_test_target", SCRIPT)
assistant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assistant)


class ReviewAssistantTests(unittest.TestCase):
    def test_dispatch_filters_existing_and_prioritizes_size_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [
                {"task_id": str(i), "handoff": str(root / f"handoff-{i}.json"),
                 "expected_result": str(root / f"result-{i}.json"),
                 "review_size_bytes": size, "extra_evidence": "must not be returned"}
                for i, size in enumerate((10, 100, 50))
            ]
            Path(items[1]["expected_result"]).write_text("do not parse me", encoding="utf-8")
            queue = root / "queue.json"
            queue.write_text(json.dumps({"max_parallel": 1, "items": items}), encoding="utf-8")
            status = {"next_action": "AI_REVIEW", "ai_queue_path": str(queue), "other": "hidden"}
            response = assistant._dispatch_response(status, code=0, parallel=5)
            dispatch = response["ai_dispatch"]
            self.assertEqual(dispatch["max_parallel"], 5)
            self.assertEqual(dispatch["pending_count"], 2)
            self.assertEqual([i["task_id"] for i in dispatch["items"]], ["2", "0"])
            self.assertEqual(set(dispatch["items"][0]), {"task_id", "handoff", "expected_result"})
            self.assertNotIn("other", response)
            self.assertEqual(assistant._dispatch_response(status, code=0, parallel=None)["ai_dispatch"]["max_parallel"], 1)
            self.assertIsNone(assistant._dispatch_response(status, code=2, parallel=5)["ai_dispatch"])
            for item in items:
                Path(item["expected_result"]).touch()
            with self.assertRaisesRegex(RuntimeError, "AI_QUEUE_EMPTY"):
                assistant._dispatch_response(status, code=0, parallel=5)

    def test_json_checkpoint_captures_script_noise_in_local_log(self):
        with tempfile.TemporaryDirectory() as directory:
            def fake_run(argv, **kwargs):
                self.assertEqual(argv[-1], "--auto")
                self.assertNotIn("--json", argv)
                kwargs["stdout"].write("scanner output that must not enter parent context\n")
                return SimpleNamespace(returncode=0)

            stream = io.StringIO()
            with (
                mock.patch.object(assistant, "BATCH_REVIEW_DIR", Path(directory)),
                mock.patch.object(assistant.subprocess, "run", side_effect=fake_run),
                mock.patch.object(assistant, "_status", return_value={"next_action": "VIEW_RESULTS"}),
                contextlib.redirect_stdout(stream),
            ):
                self.assertEqual(assistant.main(["--auto", "--json", "--ai-parallel", "5"]), 0)
            result = json.loads(stream.getvalue())
            self.assertEqual(result["exit_code"], 0)
            self.assertNotIn("scanner output", stream.getvalue())
            self.assertIn("scanner output", Path(result["log_path"]).read_text())

    def test_script_advances_multiple_stages_without_ai_or_confirmation(self):
        statuses = [{"next_action": action} for action in ("PLAN", "START", "ADVANCE", "REPORT", "VIEW_RESULTS")]
        with (
            mock.patch.object(assistant, "_status", side_effect=statuses),
            mock.patch.object(assistant, "_operator", return_value={"config_path": "test.toml", "batch_id": "test"}),
            mock.patch.object(assistant, "_batch_id", return_value="test"),
            mock.patch.object(assistant, "_save_operator"),
            mock.patch.object(assistant, "_run", return_value=0) as run,
            mock.patch.object(assistant, "_confirm", side_effect=AssertionError("no prompt allowed")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(assistant.main(["--auto"]), 0)
        self.assertEqual([call.args[0][2] for call in run.call_args_list], ["plan", "start", "advance", "report"])

    def test_auto_setup_stops_and_does_not_install(self):
        for action in ("INITIALIZE", "EDIT_CONFIG", "INSTALL_SCANNERS"):
            with (
                self.subTest(action=action),
                mock.patch.object(assistant, "_status", return_value={"next_action": action}),
                mock.patch.object(assistant, "_run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(assistant.main(["--auto"]), 2)
                run.assert_not_called()

    def test_checkpoint_failure_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                mock.patch.object(assistant, "BATCH_REVIEW_DIR", Path(directory)),
                mock.patch.object(assistant.subprocess, "run", side_effect=OSError("access denied")),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(assistant._automation_step(5), 2)
            self.assertEqual(json.loads(output.getvalue())["next_action"], "STOP")


if __name__ == "__main__":
    unittest.main()
