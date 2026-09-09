#!/usr/bin/env python3
"""Low-overhead reviewer-pool control plane.

AI-phase commands stay in one Python process. The legacy review_assistant path is
used only when a project still needs non-AI transitions such as initialization,
planning, or static preparation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_REVIEW_DIR = SCRIPT_DIR.parent
SRC_DIR = BATCH_REVIEW_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import project_status as project_status_tool  # noqa: E402
from skill_batch_review import batch_launcher  # noqa: E402
from skill_batch_review.ai_failure import fail_waiting_ai_task  # noqa: E402
from skill_batch_review.config import load_config  # noqa: E402
from skill_batch_review.dispatch_lease import (  # noqa: E402
    DispatchLeaseError,
    allocate_dispatch,
    clear_dispatch_state,
    dispatch_snapshot,
    heartbeat_leases,
    load_dispatch_state,
    release_lease,
    result_path_for_task,
)
from skill_batch_review.fast_completion import (  # noqa: E402
    FastCompletionError,
    import_ready_attempts,
)

OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
REVIEW_ASSISTANT = SCRIPT_DIR / "review_assistant.py"
DEFAULT_PARALLEL = 5
DEFAULT_TIMEOUT_SECONDS = 1200
DEFAULT_MAX_ATTEMPTS = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Reviewer Pool control")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show project and reviewer-pool status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    resume = sub.add_parser("resume", help="recover durable results and refill pool")
    resume.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)

    launched = sub.add_parser("launched", help="mark one or more leased tasks as actually launched")
    launched.add_argument("--dispatch-session", required=True)
    launched.add_argument("--task-id", action="append", required=True)

    heartbeat = sub.add_parser("heartbeat", help="refresh native reviewer liveness")
    heartbeat.add_argument("--dispatch-session", required=True)
    heartbeat.add_argument("--task-id", action="append", required=True)

    complete = sub.add_parser("complete", help="batch-import all ready attempts and refill pool")
    complete.add_argument("--dispatch-session", required=True)
    complete.add_argument("--task-id", required=True)
    complete.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)

    retry = sub.add_parser("retry", help="abandon one failed attempt and safely retry it")
    retry.add_argument("--dispatch-session", required=True)
    retry.add_argument("--task-id", required=True)
    retry.add_argument("--reason", default="native reviewer failed or was cancelled")
    retry.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)
    retry.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)

    tick = sub.add_parser("tick", help="retry stale reviewer attempts and refill pool")
    tick.add_argument("--dispatch-session", required=True)
    tick.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    tick.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)
    tick.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    return parser


def _operator_context() -> tuple[Any, str]:
    try:
        value = json.loads(OPERATOR_STATE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read operator state: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("operator state must be an object")
    config_path = Path(str(value.get("config_path") or ""))
    batch_id = str(value.get("batch_id") or "").strip()
    if not config_path.is_file() or not batch_id:
        raise RuntimeError("current operator state has no active config/batch")
    return load_config(config_path), batch_id


def _lease_path(config: Any, batch_id: str) -> Path:
    return config.workspace.manifest_root / batch_id / "ai-dispatch-state.json"


def _queue_path(config: Any, batch_id: str) -> Path:
    return config.workspace.manifest_root / batch_id / "ai-review-queue.json"


def _read_queue(config: Any, batch_id: str) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    path = _queue_path(config, batch_id)
    if not path.is_file():
        return {}, []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("AI queue must be an object")
    raw = value.get("items", [])
    if not isinstance(raw, list):
        raise RuntimeError("AI queue items must be an array")
    return value, [item for item in raw if isinstance(item, Mapping)]


def _project_status() -> dict[str, Any]:
    value = project_status_tool.inspect_project(operator_state_path=OPERATOR_STATE.resolve())
    return value.to_dict()


def _legacy_checkpoint(parallel: int) -> dict[str, Any]:
    """Compatibility path for non-AI transitions only."""

    command = [
        sys.executable,
        str(REVIEW_ASSISTANT),
        "--auto",
        "--json",
        "--ai-parallel",
        str(parallel),
    ]
    completed = subprocess.run(
        tuple(command),
        check=False,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "review checkpoint failed")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("review checkpoint returned no JSON")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise RuntimeError("review checkpoint JSON must be an object")
    value["control_plane_mode"] = "legacy_non_ai_transition"
    return value


def _result_paths(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        "csv": str(state.get("result_csv") or state.get("interim_result_csv") or ""),
        "json": str(state.get("result_json") or state.get("interim_result_json") or ""),
        "html": str(state.get("result_html") or state.get("interim_report_html") or ""),
    }


def _direct_checkpoint(
    config: Any,
    batch_id: str,
    *,
    parallel: int,
    session_id: str | None,
) -> dict[str, Any] | None:
    if parallel < 1:
        raise RuntimeError("ai-parallel must be >= 1")
    state = batch_launcher._load_state(config, batch_id)
    status = str(state.get("status") or "UNKNOWN")

    if status == "COMPLETE":
        clear_dispatch_state(_lease_path(config, batch_id))
        return {
            "exit_code": 0,
            "state": "COMPLETE",
            "next_action": "VIEW_RESULTS",
            "batch_id": batch_id,
            "summary": "当前批次已完成。",
            "result_paths": _result_paths(state),
            "ai_dispatch": None,
            "dispatch_session": None,
            "control_plane_mode": "in_process",
        }

    if state.get("ai_queue_mode") != batch_launcher._AI_QUEUE_MODE or status != "WAITING_FOR_AI":
        return None

    queue, items = _read_queue(config, batch_id)
    if not items:
        return None

    limit = parallel or int(queue.get("max_parallel") or config.concurrency.ai_reviews)
    allocation = allocate_dispatch(
        _lease_path(config, batch_id),
        batch_id=batch_id,
        queue_items=items,
        max_parallel=limit,
        session_id=session_id,
    )
    return {
        "exit_code": 0,
        "state": "WAITING_FOR_AI",
        "next_action": "AI_REVIEW",
        "batch_id": batch_id,
        "summary": (
            f"AI Reviewer Pool：{len(allocation.in_flight)} 个 in-flight，"
            f"本次补派 {len(allocation.new_items)} 个。"
        ),
        "result_paths": _result_paths(state),
        "ai_dispatch": allocation.to_dict(),
        "dispatch_session": allocation.session_id,
        "control_plane_mode": "in_process",
    }


def _checkpoint(parallel: int, session_id: str | None = None) -> dict[str, Any]:
    try:
        config, batch_id = _operator_context()
    except RuntimeError:
        return _legacy_checkpoint(parallel)
    direct = _direct_checkpoint(config, batch_id, parallel=parallel, session_id=session_id)
    if direct is not None:
        return direct
    if session_id is not None:
        raise RuntimeError("active AI session reached a non-AI transition unexpectedly")
    return _legacy_checkpoint(parallel)


def _attempt_candidates(state: Mapping[str, Any] | None) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    if not isinstance(state, Mapping):
        return result
    for source in (state.get("orphan_attempts") or [], (state.get("in_flight") or {}).values()):
        for item in source:
            if not isinstance(item, Mapping):
                continue
            task_id = str(item.get("task_id") or "").strip()
            path = str(item.get("expected_result") or "").strip()
            if task_id and path:
                result.setdefault(task_id, []).append(Path(path))
    return result


def _ready_inflight(state: Mapping[str, Any], requested_task: str | None = None) -> dict[str, list[Path]]:
    raw = state.get("in_flight")
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, list[Path]] = {}
    for task_id, lease in raw.items():
        if not isinstance(lease, Mapping):
            continue
        path = Path(str(lease.get("expected_result") or ""))
        if path.is_file():
            result.setdefault(str(task_id), []).append(path)
    if requested_task:
        requested = raw.get(requested_task)
        if not isinstance(requested, Mapping):
            raise RuntimeError(f"TASK_NOT_IN_FLIGHT: {requested_task}")
        requested_path = Path(str(requested.get("expected_result") or ""))
        if not requested_path.is_file():
            raise RuntimeError(f"AI_RESULT_MISSING: requested task {requested_task} has no durable result yet")
    return result


def _release_imported(
    config: Any,
    batch_id: str,
    session_id: str,
    imported: Sequence[str],
) -> None:
    lease_path = _lease_path(config, batch_id)
    for task_id in imported:
        try:
            release_lease(
                lease_path,
                batch_id=batch_id,
                session_id=session_id,
                task_id=task_id,
            )
        except DispatchLeaseError as exc:
            if "TASK_NOT_IN_FLIGHT" not in str(exc):
                raise


def _resume(parallel: int) -> dict[str, Any]:
    try:
        config, batch_id = _operator_context()
    except RuntimeError:
        return _legacy_checkpoint(parallel)

    state = batch_launcher._load_state(config, batch_id)
    if str(state.get("status") or "") not in {"WAITING_FOR_AI", "COMPLETE"}:
        return _legacy_checkpoint(parallel)

    prior = load_dispatch_state(_lease_path(config, batch_id))
    recovery = {"imported": [], "failed": {}, "projection_deferred": False}
    if prior is not None:
        recovered = import_ready_attempts(
            config,
            batch_id=batch_id,
            candidates=_attempt_candidates(prior),
        )
        recovery = recovered.to_dict()

    response = _checkpoint(parallel)
    response["resume"] = {
        "replaced_prior_session": bool(prior),
        "recovery": recovery,
        "instruction": "launch every ai_dispatch.items entry before waiting",
    }
    return response


def _refill(session_id: str, parallel: int) -> dict[str, Any]:
    return _checkpoint(parallel, session_id)


def _fail_or_retry(
    *,
    config: Any,
    batch_id: str,
    session_id: str,
    task_id: str,
    reason: str,
    parallel: int,
    max_attempts: int,
) -> dict[str, Any]:
    lease_path = _lease_path(config, batch_id)
    state = load_dispatch_state(lease_path)
    if not isinstance(state, Mapping) or state.get("session_id") != session_id:
        raise RuntimeError("DISPATCH_SESSION_MISMATCH")
    raw = state.get("in_flight")
    lease = raw.get(task_id) if isinstance(raw, Mapping) else None
    if not isinstance(lease, Mapping):
        raise RuntimeError(f"TASK_NOT_IN_FLIGHT: {task_id}")
    attempt = int(lease.get("attempt") or 1)

    if attempt < max_attempts:
        release_lease(
            lease_path,
            batch_id=batch_id,
            session_id=session_id,
            task_id=task_id,
            orphan_reason=f"retry: {reason}",
        )
        response = _refill(session_id, parallel)
        response["retry"] = {
            "task_id": task_id,
            "prior_attempt": attempt,
            "reason": reason,
            "terminal": False,
        }
        return response

    fail_waiting_ai_task(
        config,
        batch_id=batch_id,
        task_id=task_id,
        failure_code="AI_REVIEW_RETRY_EXHAUSTED",
        failure_reason=f"attempt {attempt}/{max_attempts}: {reason}",
    )
    release_lease(
        lease_path,
        batch_id=batch_id,
        session_id=session_id,
        task_id=task_id,
        orphan_reason=f"terminal: {reason}",
    )
    response = _refill(session_id, parallel) if load_dispatch_state(lease_path) else _checkpoint(parallel)
    response["retry"] = {
        "task_id": task_id,
        "prior_attempt": attempt,
        "reason": reason,
        "terminal": True,
    }
    return response


def _status(timeout_seconds: int) -> dict[str, Any]:
    project = _project_status()
    try:
        config, batch_id = _operator_context()
        pool = dispatch_snapshot(_lease_path(config, batch_id), timeout_seconds=timeout_seconds)
        queue, items = _read_queue(config, batch_id)
        del queue
        pool["queue_pending_count"] = len(items)
        pool["needs_resume"] = bool(
            project.get("next_action") in {"AI_REVIEW", "ADVANCE"}
            and (pool.get("in_flight_count", 0) == 0 or pool.get("stale_count", 0) > 0)
        )
        try:
            state = batch_launcher._load_state(config, batch_id)
            pool["report_projection_dirty"] = bool(state.get("report_projection_dirty"))
            pool["report_projection_dirty_count"] = int(state.get("report_projection_dirty_count") or 0)
        except Exception:
            pass
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError):
        pool = {"status": "UNAVAILABLE", "tasks": [], "needs_resume": False}
    return {"project": project, "reviewer_pool": pool}


def _human_status(value: Mapping[str, Any]) -> str:
    project = value.get("project") if isinstance(value.get("project"), Mapping) else {}
    pool = value.get("reviewer_pool") if isinstance(value.get("reviewer_pool"), Mapping) else {}
    lines = [
        f"当前状态：{project.get('summary', '-')}",
        f"批次：{project.get('batch_id') or '-'}",
        "Reviewer Pool："
        f"{pool.get('launched_count', 0)} running / {pool.get('reserved_count', 0)} reserved / "
        f"{pool.get('stale_count', 0)} stale / max {pool.get('max_parallel', 0)}",
        f"队列待审：{pool.get('queue_pending_count', 0)}",
    ]
    if pool.get("report_projection_dirty"):
        lines.append(
            f"报告投影：延迟刷新中（累计 {pool.get('report_projection_dirty_count', 0)} 个 AI 结果；Batch 收尾时一次性重建）"
        )
    tasks = pool.get("tasks") if isinstance(pool.get("tasks"), list) else []
    for item in tasks:
        if not isinstance(item, Mapping):
            continue
        age = item.get("age_seconds")
        age_text = f"{int(age)//60}m{int(age)%60:02d}s" if isinstance(age, int) else "?"
        lines.append(
            f"- {item.get('task_id')} | {item.get('state')} | attempt {item.get('attempt')} | "
            f"age {age_text}{' | STALE' if item.get('stale') else ''}{' | result ready' if item.get('result_ready') else ''}"
        )
    if pool.get("needs_resume"):
        lines.append("建议：执行 /auto-skill-review；新 invocation 会从 durable state 重新接管并补满 Reviewer Pool。")
    else:
        lines.append(f"下一步：{project.get('next_instruction', '-')}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            value = _status(args.timeout_seconds)
            print(json.dumps(value, ensure_ascii=False, indent=2) if args.json else _human_status(value))
            return 0
        if args.command == "resume":
            print(json.dumps(_resume(args.ai_parallel), ensure_ascii=False))
            return 0

        config, batch_id = _operator_context()
        lease_path = _lease_path(config, batch_id)

        if args.command == "launched":
            tasks = heartbeat_leases(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_ids=args.task_id,
            )
            print(json.dumps({"status": "LAUNCHED", "task_ids": list(tasks)}, ensure_ascii=False))
            return 0

        if args.command == "heartbeat":
            tasks = heartbeat_leases(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_ids=args.task_id,
            )
            print(json.dumps({"status": "HEARTBEAT", "task_ids": list(tasks)}, ensure_ascii=False))
            return 0

        if args.command == "complete":
            result_path_for_task(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_id=args.task_id,
            )
            lease_state = load_dispatch_state(lease_path)
            if not isinstance(lease_state, Mapping) or lease_state.get("session_id") != args.dispatch_session:
                raise RuntimeError("DISPATCH_SESSION_MISMATCH")
            ready = _ready_inflight(lease_state, args.task_id)
            imported = import_ready_attempts(config, batch_id=batch_id, candidates=ready)
            _release_imported(config, batch_id, args.dispatch_session, imported.imported)
            response = _checkpoint(args.ai_parallel) if imported.remaining_ai == 0 else _refill(args.dispatch_session, args.ai_parallel)
            response["completion"] = {
                "event_task_id": args.task_id,
                **imported.to_dict(),
                "batched_ready_count": len(imported.imported),
            }
            print(json.dumps(response, ensure_ascii=False))
            return 0

        if args.command == "retry":
            response = _fail_or_retry(
                config=config,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_id=args.task_id,
                reason=args.reason,
                parallel=args.ai_parallel,
                max_attempts=args.max_attempts,
            )
            print(json.dumps(response, ensure_ascii=False))
            return 0

        if args.command == "tick":
            if args.timeout_seconds < 60:
                raise RuntimeError("timeout-seconds must be >= 60")
            snapshot = dispatch_snapshot(lease_path, timeout_seconds=args.timeout_seconds)
            stale = [
                str(item["task_id"])
                for item in snapshot.get("tasks", [])
                if isinstance(item, Mapping) and item.get("stale")
            ]
            response: dict[str, Any] | None = None
            for task_id in stale:
                response = _fail_or_retry(
                    config=config,
                    batch_id=batch_id,
                    session_id=args.dispatch_session,
                    task_id=task_id,
                    reason=f"no heartbeat for {args.timeout_seconds} seconds",
                    parallel=args.ai_parallel,
                    max_attempts=args.max_attempts,
                )
            if response is None:
                response = _refill(args.dispatch_session, args.ai_parallel)
            response["watchdog"] = {"stale_processed": len(stale)}
            print(json.dumps(response, ensure_ascii=False))
            return 0

        raise RuntimeError(f"unsupported command: {args.command}")
    except (DispatchLeaseError, FastCompletionError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"exit_code": 2, "next_action": "STOP", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
