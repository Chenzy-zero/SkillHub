#!/usr/bin/env python3
"""Trusted watchdog for hung/failed isolated AI reviewer subagents."""

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

from skill_batch_review.ai_failure import AIFailureError, fail_waiting_ai_task  # noqa: E402
from skill_batch_review.config import load_config  # noqa: E402
from skill_batch_review.dispatch_lease import DispatchLeaseError, release_lease  # noqa: E402


OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
REVIEW_ASSISTANT = SCRIPT_DIR / "review_assistant.py"
DEFAULT_TIMEOUT_SECONDS = 1200
DEFAULT_PARALLEL = 5


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Reviewer lease watchdog")
    sub = parser.add_subparsers(dest="command", required=True)

    tick = sub.add_parser("tick", help="fail timed-out reviewer leases and refill capacity")
    tick.add_argument("--dispatch-session", required=True)
    tick.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    tick.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)

    failed = sub.add_parser("fail", help="record one native subagent failed/cancelled event")
    failed.add_argument("--dispatch-session", required=True)
    failed.add_argument("--task-id", required=True)
    failed.add_argument("--reason", default="native reviewer subagent failed or was cancelled")
    failed.add_argument("--ai-parallel", type=int, default=DEFAULT_PARALLEL)
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


def _load_live_leases(path: Path, *, batch_id: str, session_id: str) -> dict[str, Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read dispatch lease state: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("dispatch lease state must be an object")
    if value.get("batch_id") != batch_id:
        raise RuntimeError("dispatch lease batch does not match current batch")
    if value.get("session_id") != session_id:
        raise RuntimeError("DISPATCH_SESSION_MISMATCH: watchdog belongs to another coordinator session")
    raw = value.get("in_flight")
    if not isinstance(raw, Mapping):
        raise RuntimeError("dispatch lease state has invalid in_flight data")
    return {
        str(task_id): item
        for task_id, item in raw.items()
        if isinstance(item, Mapping)
    }


def _parse_time(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError("reviewer lease is missing leased_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"reviewer lease has invalid leased_at: {text!r}") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("reviewer lease leased_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _stale_task_ids(
    leases: Mapping[str, Mapping[str, Any]], *, timeout_seconds: int
) -> tuple[str, ...]:
    if timeout_seconds < 60:
        raise RuntimeError("timeout-seconds must be >= 60")
    now = datetime.now(timezone.utc)
    stale = [
        task_id
        for task_id, lease in leases.items()
        if (now - _parse_time(lease.get("leased_at"))).total_seconds() >= timeout_seconds
    ]
    return tuple(sorted(stale))


def _fail_and_release(
    config: Any,
    *,
    batch_id: str,
    lease_path: Path,
    session_id: str,
    task_id: str,
    failure_code: str,
    failure_reason: str,
) -> dict[str, Any]:
    # Validate ownership before changing the durable review state.
    leases = _load_live_leases(lease_path, batch_id=batch_id, session_id=session_id)
    if task_id not in leases:
        raise RuntimeError(f"TASK_NOT_IN_FLIGHT: task {task_id!r} is not leased by this session")
    result = fail_waiting_ai_task(
        config,
        batch_id=batch_id,
        task_id=task_id,
        failure_code=failure_code,
        failure_reason=failure_reason,
    )
    release_lease(
        lease_path,
        batch_id=batch_id,
        session_id=session_id,
        task_id=task_id,
    )
    return result.to_dict()


def _refill(*, session_id: str, parallel: int) -> dict[str, Any]:
    if parallel < 1:
        raise RuntimeError("ai-parallel must be >= 1")
    completed = subprocess.run(
        (
            sys.executable,
            str(REVIEW_ASSISTANT),
            "--auto",
            "--json",
            "--ai-parallel",
            str(parallel),
            "--dispatch-session",
            session_id,
        ),
        check=False,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "review refill failed")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("review refill returned no JSON")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("review refill returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("review refill JSON must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config, batch_id = _operator_context()
        lease_path = _lease_path(config, batch_id)
        failures: list[dict[str, Any]] = []

        if args.command == "fail":
            failures.append(
                _fail_and_release(
                    config,
                    batch_id=batch_id,
                    lease_path=lease_path,
                    session_id=args.dispatch_session,
                    task_id=args.task_id,
                    failure_code="AI_REVIEW_AGENT_FAILED",
                    failure_reason=args.reason,
                )
            )
        else:
            leases = _load_live_leases(
                lease_path,
                batch_id=batch_id,
                session_id=args.dispatch_session,
            )
            for task_id in _stale_task_ids(leases, timeout_seconds=args.timeout_seconds):
                failures.append(
                    _fail_and_release(
                        config,
                        batch_id=batch_id,
                        lease_path=lease_path,
                        session_id=args.dispatch_session,
                        task_id=task_id,
                        failure_code="AI_REVIEW_TIMEOUT",
                        failure_reason=f"reviewer lease exceeded {args.timeout_seconds} seconds",
                    )
                )

        response = _refill(session_id=args.dispatch_session, parallel=args.ai_parallel)
        response["watchdog"] = {
            "event": args.command,
            "failed_count": len(failures),
            "failed_tasks": failures,
        }
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except (AIFailureError, DispatchLeaseError, OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "exit_code": 2,
                    "next_action": "STOP",
                    "error": str(exc),
                    "watchdog": {"event": getattr(args, "command", "unknown")},
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
