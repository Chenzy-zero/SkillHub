from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from skill_batch_review import batch_launcher
from skill_batch_review.fast_completion import FastImportResult, import_ready_attempts


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "tools" / "review_pool_core.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("review_pool_core_test", CORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fast_completion_imports_ready_burst_with_one_queue_refresh(tmp_path: Path):
    items = []
    candidates = {}
    for index in range(6):
        task_id = f"task-{index}"
        items.append(
            {
                "task_id": task_id,
                "status": "WAITING_FOR_AI",
                "index_path": str(tmp_path / f"index-{index}.json"),
            }
        )
        if index < 5:
            result = tmp_path / f"attempt-{index}.json"
            result.write_text("{}", encoding="utf-8")
            candidates[task_id] = [result]

    state = {
        "batch_id": "batch-1",
        "status": "WAITING_FOR_AI",
        "ai_queue_mode": batch_launcher._AI_QUEUE_MODE,
        "items": items,
    }
    config = SimpleNamespace()

    with (
        mock.patch.object(batch_launcher, "_load_state", return_value=state),
        mock.patch("skill_batch_review.fast_completion.finalize_skill", return_value={}) as finalize,
        mock.patch.object(batch_launcher, "_activate_batch_queue") as activate,
        mock.patch.object(batch_launcher, "_save") as save,
        mock.patch.object(batch_launcher, "_prepare_next") as prepare_next,
    ):
        result = import_ready_attempts(
            config,
            batch_id="batch-1",
            candidates=candidates,
        )

    assert result.imported == tuple(f"task-{index}" for index in range(5))
    assert result.remaining_ai == 1
    assert result.projection_deferred is True
    assert finalize.call_count == 5
    activate.assert_called_once()
    save.assert_not_called()
    prepare_next.assert_not_called()
    assert state["report_projection_dirty"] is True
    assert state["report_projection_dirty_count"] == 5


def test_ai_refill_uses_direct_checkpoint_without_subprocess():
    core = _load_core()
    config = object()
    expected = {"control_plane_mode": "in_process", "next_action": "AI_REVIEW"}

    with (
        mock.patch.object(core, "_operator_context", return_value=(config, "batch-1")),
        mock.patch.object(core, "_direct_checkpoint", return_value=expected) as direct,
        mock.patch.object(core, "_legacy_checkpoint") as legacy,
        mock.patch.object(core.subprocess, "run", side_effect=AssertionError("subprocess must not run")),
    ):
        actual = core._checkpoint(5, "session-1")

    assert actual is expected
    direct.assert_called_once_with(config, "batch-1", parallel=5, session_id="session-1")
    legacy.assert_not_called()


def test_complete_event_batches_every_ready_inflight_attempt(tmp_path: Path, capsys):
    core = _load_core()
    in_flight = {}
    for index in range(5):
        path = tmp_path / f"attempt-{index}.json"
        path.write_text("{}", encoding="utf-8")
        in_flight[f"task-{index}"] = {
            "task_id": f"task-{index}",
            "expected_result": str(path),
            "attempt": 1,
        }

    lease_state = {"session_id": "session-1", "in_flight": in_flight}
    imported = FastImportResult(
        batch_id="batch-1",
        imported=tuple(in_flight),
        failed={},
        remaining_ai=3,
        batch_status="WAITING_FOR_AI",
        report_html=None,
        projection_deferred=True,
    )
    config = SimpleNamespace(workspace=SimpleNamespace(manifest_root=tmp_path))

    with (
        mock.patch.object(core, "_operator_context", return_value=(config, "batch-1")),
        mock.patch.object(core, "load_dispatch_state", return_value=lease_state),
        mock.patch.object(core, "result_path_for_task", return_value=Path(in_flight["task-0"]["expected_result"])),
        mock.patch.object(core, "import_ready_attempts", return_value=imported) as importer,
        mock.patch.object(core, "_release_imported") as release,
        mock.patch.object(core, "_refill", return_value={"next_action": "AI_REVIEW"}),
    ):
        code = core.main(
            [
                "complete",
                "--dispatch-session",
                "session-1",
                "--task-id",
                "task-0",
                "--ai-parallel",
                "5",
            ]
        )

    assert code == 0
    candidates = importer.call_args.kwargs["candidates"]
    assert set(candidates) == set(in_flight)
    release.assert_called_once_with(config, "batch-1", "session-1", tuple(in_flight))
    payload = json.loads(capsys.readouterr().out)
    assert payload["completion"]["batched_ready_count"] == 5


def test_pool_wrapper_is_thin_and_core_keeps_legacy_only_for_non_ai_transition():
    wrapper = (ROOT / "tools" / "review_pool.py").read_text(encoding="utf-8")
    core = CORE_PATH.read_text(encoding="utf-8")
    assert "from review_pool_core import main" in wrapper
    assert "control_plane_mode\": \"in_process" in core
    assert "legacy_non_ai_transition" in core
    assert "batch-import all ready attempts" in core
