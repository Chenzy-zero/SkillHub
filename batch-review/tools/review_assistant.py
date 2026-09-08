#!/usr/bin/env python3
"""State-aware operator and completion-driven AI dispatch entry point."""

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

from skill_batch_review.completion_import import (  # noqa: E402
    CompletionImportError,
    import_completed_task,
)
from skill_batch_review.config import load_config  # noqa: E402
from skill_batch_review.dispatch_lease import (  # noqa: E402
    DispatchLeaseError,
    allocate_dispatch,
    clear_dispatch_state,
)


OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
INIT_SCRIPT = SCRIPT_DIR / "init_project.py"
STATUS_SCRIPT = SCRIPT_DIR / "project_status.py"
LAUNCHER = SCRIPT_DIR / "run_skill_batch.py"
INSTALLER = SCRIPT_DIR / "install_scanners.py"


def _run(argv: Sequence[str]) -> int:
    return subprocess.run(tuple(argv), check=False).returncode


def _status() -> dict[str, Any]:
    completed = subprocess.run(
        (sys.executable, str(STATUS_SCRIPT), "--json"),
        check=False,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "项目状态检查失败")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("项目状态输出无效")
    return value


def _operator() -> dict[str, Any]:
    value = json.loads(OPERATOR_STATE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("初始化状态无效")
    return value


def _save_operator(value: dict[str, Any]) -> None:
    OPERATOR_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OPERATOR_STATE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OPERATOR_STATE)


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]：").strip().lower() in {"y", "yes"}


def _print_status(status: dict[str, Any]) -> None:
    print(f"\n当前状态：{status.get('summary')}")
    if status.get("batch_id"):
        print(f"当前批次：{status['batch_id']}")
    current = status.get("current_skill")
    if isinstance(current, dict):
        print(f"当前 Skill：{current.get('skill_id', '-')} / {current.get('skill_name', '-')}")
    if status.get("ai_queue_path"):
        print(f"AI 队列：{status['ai_queue_path']}")
    for issue in status.get("issues") or []:
        print(f"- [{issue.get('code')}] {issue.get('message')}")
    print(f"下一步：{status.get('next_instruction')}")


def _batch_id(operator: dict[str, Any]) -> str:
    prefix = "skill-review"
    try:
        config_text = Path(str(operator["config_path"])).read_text(encoding="utf-8")
        for line in config_text.splitlines():
            if line.strip().startswith("batch_id_prefix") and "=" in line:
                prefix = line.split("=", 1)[1].strip().strip('"').strip("'") or prefix
                break
    except OSError:
        pass
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Skill 安全审查免参数编排入口。")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="由已授权的自动审查 Skill 调用；不重复询问计划、启动和推进确认",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="脚本推进到 AI 等待或结束，仅返回精简调度 JSON",
    )
    parser.add_argument("--ai-parallel", type=int, help="本次 AI 调度并发上限，不改写配置或批次")
    parser.add_argument(
        "--completed-task-id",
        help="由自动审查协调器回传的单个 Reviewer completion event",
    )
    parser.add_argument(
        "--dispatch-session",
        help="首次 JSON checkpoint 返回的 dispatch session；completion event 必须原样带回",
    )
    return parser


def _dispatch_response(status: dict[str, Any], *, code: int, parallel: int | None) -> dict[str, Any]:
    """Compatibility pure queue projection used by older callers/tests."""

    response = {
        key: status.get(key)
        for key in (
            "state",
            "next_action",
            "batch_id",
            "summary",
            "next_instruction",
            "result_paths",
            "issues",
        )
    }
    response["exit_code"] = code
    response["ai_dispatch"] = None
    if code == 0 and status.get("next_action") == "AI_REVIEW":
        queue_path = status.get("ai_queue_path")
        if not queue_path:
            raise RuntimeError("AI_QUEUE_MISSING: 状态未提供 Batch AI 队列")
        queue = json.loads(Path(queue_path).read_text(encoding="utf-8"))
        pending = [
            item
            for item in queue.get("items", [queue])
            if not Path(item["expected_result"]).is_file()
        ]
        if not pending:
            raise RuntimeError("AI_QUEUE_EMPTY: 状态要求 AI 审查，但队列没有待审任务；请检查批次状态")
        pending.sort(key=lambda item: -item.get("review_size_bytes", 0))
        response["ai_dispatch"] = {
            "max_parallel": parallel if parallel is not None else queue.get("max_parallel", 1),
            "scheduling": "rolling_largest_first",
            "pending_count": len(pending),
            "items": [
                {key: item[key] for key in ("task_id", "handoff", "expected_result")}
                for item in pending
            ],
        }
    return response


def _control_paths(status: Mapping[str, Any]) -> tuple[Path, Path, int]:
    operator = _operator()
    batch_id = str(status.get("batch_id") or operator.get("batch_id") or "").strip()
    if not batch_id:
        raise RuntimeError("当前没有可调度的 batch_id")
    config = load_config(Path(str(operator["config_path"])))
    root = config.workspace.manifest_root / batch_id
    return root / "ai-review-queue.json", root / "ai-dispatch-state.json", config.concurrency.ai_reviews


def _queue_items(path: Path) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    if not path.is_file():
        return {}, []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("AI queue 顶层必须是对象")
    raw = value.get("items", [])
    if not isinstance(raw, list):
        raise RuntimeError("AI queue items 必须是数组")
    items = [item for item in raw if isinstance(item, Mapping)]
    return value, items


def _leased_dispatch_response(
    status: dict[str, Any],
    *,
    code: int,
    parallel: int | None,
    dispatch_session: str | None,
    completed_task_id: str | None,
) -> dict[str, Any]:
    """Allocate only newly free reviewer slots and persist trusted leases."""

    response = {
        key: status.get(key)
        for key in (
            "state",
            "next_action",
            "batch_id",
            "summary",
            "next_instruction",
            "result_paths",
            "issues",
        )
    }
    response["exit_code"] = code
    response["ai_dispatch"] = None
    if code != 0:
        response["next_action"] = "STOP"
        response["dispatch_session"] = dispatch_session
        if completed_task_id:
            response["failed_task_id"] = completed_task_id
            response["continue_existing_reviewers"] = True
        return response

    action = str(status.get("next_action") or "")
    if action == "VIEW_RESULTS" or status.get("state") == "COMPLETE":
        try:
            _, lease_path, _ = _control_paths(status)
            clear_dispatch_state(lease_path)
        except (OSError, RuntimeError, ValueError):
            pass
        return response

    # project_status may say ADVANCE when another in-flight result already
    # exists.  Completion-driven mode must not bulk-import it without that
    # task's explicit event, so ADVANCE remains an AI scheduling boundary here.
    if action not in {"AI_REVIEW", "ADVANCE"} and not completed_task_id:
        return response

    queue_path, lease_path, configured_parallel = _control_paths(status)
    queue, items = _queue_items(queue_path)
    limit = parallel if parallel is not None else int(queue.get("max_parallel") or configured_parallel)
    allocation = allocate_dispatch(
        lease_path,
        batch_id=str(status.get("batch_id") or ""),
        queue_items=items,
        max_parallel=limit,
        session_id=dispatch_session,
        completed_task_id=completed_task_id,
    )
    response["ai_dispatch"] = allocation.to_dict()
    response["dispatch_session"] = allocation.session_id
    if allocation.in_flight:
        response["next_action"] = "AI_REVIEW"
        response["summary"] = (
            f"AI completion-driven 调度中：{len(allocation.in_flight)} 个 in-flight，"
            f"本次补派 {len(allocation.new_items)} 个。"
        )
    return response


def _import_completion_event(task_id: str) -> None:
    operator = _operator()
    config_path = Path(str(operator.get("config_path") or ""))
    batch_id = str(operator.get("batch_id") or "").strip()
    if not config_path.is_file() or not batch_id:
        raise RuntimeError("completion event 缺少当前 config/batch 上下文")
    result = import_completed_task(
        load_config(config_path),
        batch_id=batch_id,
        task_id=task_id,
    )
    print(
        f"AI completion imported: {result.task_id} / {result.status}; "
        f"remaining={result.remaining_ai}"
    )


def _automation_step(
    parallel: int | None,
    completed_task_id: str | None = None,
    dispatch_session: str | None = None,
) -> int:
    """Run deterministic transitions, then allocate only free reviewer slots."""

    log_path = BATCH_REVIEW_DIR / ".batch-review" / "automation-last.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--auto"]
        if completed_task_id:
            command.extend(("--completed-task-id", completed_task_id))
        if dispatch_session:
            command.extend(("--dispatch-session", dispatch_session))
        with log_path.open("w", encoding="utf-8") as log:
            code = subprocess.run(
                tuple(command),
                stdout=log,
                stderr=log,
                check=False,
            ).returncode
        status = _status()
        response = _leased_dispatch_response(
            status,
            code=code,
            parallel=parallel,
            dispatch_session=dispatch_session,
            completed_task_id=completed_task_id,
        )
        response["log_path"] = str(log_path)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, DispatchLeaseError) as exc:
        code = 2
        response = {
            "exit_code": code,
            "next_action": "STOP",
            "error": str(exc),
            "log_path": str(log_path),
            "dispatch_session": dispatch_session,
        }
        if completed_task_id:
            response["failed_task_id"] = completed_task_id
            response["continue_existing_reviewers"] = True
    print(json.dumps(response, ensure_ascii=False))
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.ai_parallel is not None and args.ai_parallel < 1:
        raise SystemExit("--ai-parallel must be >= 1")
    if args.json:
        if not args.auto:
            raise SystemExit("--json requires --auto")
        if args.completed_task_id and not args.dispatch_session:
            raise SystemExit("--completed-task-id requires --dispatch-session")
        return _automation_step(
            args.ai_parallel,
            args.completed_task_id,
            args.dispatch_session,
        )
    if args.completed_task_id:
        if not args.auto or not args.dispatch_session:
            raise SystemExit("completion events require --auto and --dispatch-session")
        try:
            _import_completion_event(args.completed_task_id)
            return 0
        except (OSError, RuntimeError, ValueError, CompletionImportError) as exc:
            print(f"completion import failed: {exc}", file=sys.stderr)
            return 2

    run_authorized = bool(args.auto)
    try:
        while True:
            status = _status()
            _print_status(status)
            action = status.get("next_action")
            if action == "INITIALIZE":
                if args.auto:
                    print("自动模式不会替代首次初始化，请先运行 init.cmd。")
                    return 2
                if _confirm("是否现在初始化项目"):
                    return _run((sys.executable, str(INIT_SCRIPT)))
                return 0
            if action == "EDIT_CONFIG":
                print("请只修改上面显示的本机配置文件；保存后再次运行本入口。")
                return 2 if args.auto else 0
            if action == "INSTALL_SCANNERS":
                if args.auto:
                    print("自动模式不会静默安装扫描器，请先双击 review.cmd 完成安装确认。")
                    return 2
                if not _confirm("是否现在安装两套扫描器及必要的项目专用 Python 3.13"):
                    return 0
                code = _run(
                    (
                        sys.executable,
                        str(INSTALLER),
                        "--root",
                        str(BATCH_REVIEW_DIR / ".scanner-tools"),
                    )
                )
                if code == 0:
                    print("扫描器已安装；本入口将继续检查并启动批次。")
                    continue
                return code
            if action == "PLAN":
                if not run_authorized and not _confirm(
                    "是否启动全自动批次（生成计划后将直接下载并静态扫描）"
                ):
                    return 0
                run_authorized = True
                operator = _operator()
                batch_id = _batch_id(operator)
                code = _run(
                    (
                        sys.executable,
                        str(LAUNCHER),
                        "plan",
                        "--config",
                        str(operator["config_path"]),
                        "--batch-id",
                        batch_id,
                    )
                )
                if code != 0:
                    return code
                operator["batch_id"] = batch_id
                _save_operator(operator)
                continue
            if action == "START":
                if not run_authorized and not _confirm("是否开始自动下载仓库并逐一执行静态扫描"):
                    return 0
                run_authorized = True
                operator = _operator()
                code = _run(
                    (
                        sys.executable,
                        str(LAUNCHER),
                        "start",
                        "--config",
                        str(operator["config_path"]),
                        "--batch-id",
                        str(operator["batch_id"]),
                        "--execute",
                    )
                )
                if code != 0:
                    return code
                continue
            if action == "AI_REVIEW":
                print("请选择一个已批准的 AI 入口继续：")
                print("- Codex CLI：$auto-skill-review")
                print("- Claude Code：/auto-skill-review")
                print("它会对当前 Batch 使用隔离 Reviewer，并按 completion event 滚动补位。")
                return 0
            if action == "ADVANCE":
                # Legacy/manual compatibility only. Completion-driven auto mode
                # uses explicit task events and never bulk-imports unseen events.
                operator = _operator()
                code = _run(
                    (
                        sys.executable,
                        str(LAUNCHER),
                        "advance",
                        "--config",
                        str(operator["config_path"]),
                        "--batch-id",
                        str(operator["batch_id"]),
                        "--execute",
                        "--confirm-cleanup",
                    )
                )
                if code != 0:
                    return code
                continue
            if action == "REPORT":
                operator = _operator()
                code = _run(
                    (
                        sys.executable,
                        str(LAUNCHER),
                        "report",
                        "--config",
                        str(operator["config_path"]),
                        "--batch-id",
                        str(operator["batch_id"]),
                    )
                )
                if code != 0:
                    return code
                continue
            if action == "VIEW_RESULTS":
                results = status.get("result_paths") or {}
                print(f"结果 CSV：{results.get('csv', '')}")
                print(f"结果 JSON：{results.get('json', '')}")
                print(f"HTML 报告：{results.get('html', '')}")
                return 0
            print("当前状态需要人工检查，程序没有执行修改操作。")
            return 2
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"操作入口失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
