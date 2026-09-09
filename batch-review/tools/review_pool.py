#!/usr/bin/env python3
"""Trusted control plane for observable, resumable AI reviewer attempts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_REVIEW_DIR = SCRIPT_DIR.parent
SRC_DIR = BATCH_REVIEW_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skill_batch_review.ai_failure import fail_waiting_ai_task  # noqa: E402
from skill_batch_review.completion_import import (  # noqa: E402
    CompletionImportError,
    import_completed_task,
    recover_ready_results,
)
from skill_batch_review.config import load_config  # noqa: E402
from skill_batch_review.dispatch_lease import (  # noqa: E402
    DispatchLeaseError,
    dispatch_snapshot,
    heartbeat_leases,
    load_dispatch_state,
    mark_launched,
    release_lease,
    result_path_for_task,
)

OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
REVIEW_ASSISTANT = SCRIPT_DIR / "review_assistant.py"
PROJECT_STATUS = SCRIPT_DIR / "project_status.py"
DEFAULT_PARALLEL = 5
DEFAULT_TIMEOUT_SECONDS = 1200
DEFAULT_MAX_ATTEMPTS = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Reviewer Pool control")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show project and reviewer-pool status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    resume = sub.add_parser("resume", help="recover results, replace stale coordinator, and refill pool")
    resume.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)

    launched = sub.add_parser("launched", help="mark a leased task as actually launched")
    launched.add_argument("--dispatch-session", required=True)
    launched.add_argument("--task-id", required=True)

    heartbeat = sub.add_parser("heartbeat", help="refresh native reviewer liveness")
    heartbeat.add_argument("--dispatch-session", required=True)
    heartbeat.add_argument("--task-id", action="append", required=True)

    complete = sub.add_parser("complete", help="import one completed attempt and refill the slot")
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


def _project_status() -> dict[str, Any]:
    completed = subprocess.run(
        (sys.executable, str(PROJECT_STATUS), "--json"),
        check=False,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "project status failed")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("project status must be an object")
    return value


def _run_checkpoint(*, parallel: int, session_id: str | None = None) -> dict[str, Any]:
    if parallel < 1:
        raise RuntimeError("ai-parallel must be >= 1")
    command = [
        sys.executable,
        str(REVIEW_ASSISTANT),
        "--auto",
        "--json",
        "--ai-parallel",
        str(parallel),
    ]
    if session_id:
        command.extend(("--dispatch-session", session_id))
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
    return value


def _resume(parallel: int) -> dict[str, Any]:
    recovery: dict[str, Any] = {"imported": [], "failed": {}}
    try:
        config, batch_id = _operator_context()
    except RuntimeError:
        # New projects may still need init/plan/static preparation. The normal
        # checkpoint owns those transitions and will return the precise action.
        return _run_checkpoint(parallel=parallel)
    lease_path = _lease_path(config, batch_id)
    prior = load_dispatch_state(lease_path)
    if prior is not None:
        recovered = recover_ready_results(
            config,
            batch_id=batch_id,
            attempt_candidates=_attempt_candidates(prior),
        )
        recovery = recovered.to_dict()
    response = _run_checkpoint(parallel=parallel)
    response["resume"] = {
        "replaced_prior_session": bool(prior),
        "recovery": recovery,
        "instruction": "launch every ai_dispatch.items entry before waiting",
    }
    return response


def _refill(session_id: str, parallel: int) -> dict[str, Any]:
    return _run_checkpoint(parallel=parallel, session_id=session_id)


def _fail_or_retry(
    *,
    config: Any,
    batch_id: str,
    lease_path: Path,
    session_id: str,
    task_id: str,
    reason: str,
    parallel: int,
    max_attempts: int,
) -> dict[str, Any]:
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
    response = _refill(session_id, parallel)
    response["retry"] = {
        "task_id": task_id,
        "prior_attempt": attempt,
        "reason": reason,
        "terminal": True,
    }
    return response


def _status(timeout_seconds: int) -> dict[str, Any]:
    project = _project_status()
    pool: dict[str, Any]
    try:
        config, batch_id = _operator_context()
        pool = dispatch_snapshot(_lease_path(config, batch_id), timeout_seconds=timeout_seconds)
        queue_path = config.workspace.manifest_root / batch_id / "ai-review-queue.json"
        queued = 0
        if queue_path.is_file():
            value = json.loads(queue_path.read_text(encoding="utf-8"))
            if isinstance(value, Mapping) and isinstance(value.get("items"), list):
                queued = len(value["items"])
        pool["queue_pending_count"] = queued
        pool["needs_resume"] = bool(
            project.get("next_action") in {"AI_REVIEW", "ADVANCE"}
            and (pool.get("in_flight_count", 0) == 0 or pool.get("stale_count", 0) > 0)
        )
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
            mark_launched(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_id=args.task_id,
            )
            print(json.dumps({"status": "LAUNCHED", "task_id": args.task_id}, ensure_ascii=False))
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
            result_path = result_path_for_task(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_id=args.task_id,
            )
            imported = import_completed_task(
                config,
                batch_id=batch_id,
                task_id=args.task_id,
                ai_result_path=result_path,
            )
            release_lease(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
                task_id=args.task_id,
            )
            response = _refill(args.dispatch_session, args.ai_parallel)
            response["completion"] = imported.to_dict()
            print(json.dumps(response, ensure_ascii=False))
            return 0
        if args.command == "retry":
            response = _fail_or_retry(
                config=config,
                batch_id=batch_id,
                lease_path=lease_path,
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
            responses: list[dict[str, Any]] = []
            for item in snapshot.get("tasks", []):
                if isinstance(item, Mapping) and item.get("stale"):
                    responses.append(
                        _fail_or_retry(
                            config=config,
                            batch_id=batch_id,
                            lease_path=lease_path,
                            session_id=args.dispatch_session,
                            task_id=str(item["task_id"]),
                            reason=f"no heartbeat for {args.timeout_seconds} seconds",
                            parallel=args.ai_parallel,
                            max_attempts=args.max_attempts,
                        )
                    )
            response = responses[-1] if responses else _refill(args.dispatch_session, args.ai_parallel)
            response["watchdog"] = {"stale_processed": len(responses)}
            print(json.dumps(response, ensure_ascii=False))
            return 0
        raise RuntimeError(f"unsupported command: {args.command}")
    except (CompletionImportError, DispatchLeaseError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"exit_code": 2, "next_action": "STOP", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
