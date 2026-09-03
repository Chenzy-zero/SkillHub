#!/usr/bin/env python3
"""Operator launcher for the repository-at-a-time review workflow.

The launcher advances only one operator-visible boundary at a time.  It never
invokes Claude Code, never fabricates an AI result, and never cleans a
repository workspace without ``--confirm-cleanup``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skill_batch_review.config import ReviewConfig, load_config  # noqa: E402
from skill_batch_review.inventory import load_inventory_csv  # noqa: E402
from skill_batch_review.orchestrator import (  # noqa: E402
    cleanup_repository_workspace,
    finalize_repository,
    plan_repositories,
    prepare_repository,
)
from skill_batch_review.preflight import review_preflight  # noqa: E402
from skill_batch_review.reporting import write_batch_reports  # noqa: E402


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STATE_NAME = "launcher-state.json"


class LauncherError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_batch_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _validate_batch_id(value: str) -> str:
    if not _BATCH_ID_RE.fullmatch(value):
        raise LauncherError("批次号必须为 1-64 位字母、数字、点、下划线或连字符")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _state_path(config: ReviewConfig, batch_id: str) -> Path:
    return config.workspace.manifest_root / batch_id / _STATE_NAME


def _load_state(config: ReviewConfig, batch_id: str) -> dict[str, Any]:
    path = _state_path(config, batch_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LauncherError(f"批次不存在，请先执行 start: {batch_id}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LauncherError(f"无法读取批次状态 {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("repositories"), list):
        raise LauncherError(f"批次状态格式无效: {path}")
    return value


def _save_state(config: ReviewConfig, state: dict[str, Any]) -> Path:
    state["updated_at"] = _utc_now()
    path = _state_path(config, str(state["batch_id"]))
    _atomic_json(path, state)
    return path


def _check_config_binding(config: ReviewConfig, state: Mapping[str, Any]) -> None:
    current = _sha256(config.path)
    if state.get("config_sha256") != current:
        raise LauncherError(
            "配置文件在批次创建后发生变化。请恢复原配置，或创建新批次；"
            "不要让同一批次混用不同配置。"
        )


def _preflight_or_raise(config: ReviewConfig) -> None:
    issues = review_preflight(config)
    if issues:
        details = "\n".join(f"- [{item.code}] {item.message}" for item in issues)
        raise LauncherError("运行前检查未通过：\n" + details)


def _new_state(config: ReviewConfig, batch_id: str) -> dict[str, Any]:
    inventory = load_inventory_csv(
        config.batch.inventory_csv,
        status_mapping=config.status_mapping.aliases,
    )
    plans = plan_repositories(inventory, included_statuses=config.batch.included_statuses)
    repositories = [
        {
            "name": plan.repository,
            "included_row_count": len(plan.included_rows),
            "excluded_row_count": len(plan.excluded_rows),
            "status": "PENDING" if plan.included_rows else "EXCLUDED",
            "repository_index": None,
            "result_index": None,
            "task_ids": [],
            "reused_task_ids": [],
        }
        for plan in plans
    ]
    state: dict[str, Any] = {
        "schema_version": "0.1",
        "batch_id": batch_id,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "READY",
        "config_path": str(config.path),
        "config_sha256": _sha256(config.path),
        "inventory_csv": str(config.batch.inventory_csv),
        "inventory_csv_sha256": inventory.raw_csv_sha256,
        "inventory_csv_encoding": inventory.source_encoding,
        "source_row_count": inventory.raw_row_count,
        "execution_record_count": inventory.row_count,
        "current_repository": None,
        "repositories": repositories,
    }
    return state


def _repository_entry(state: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in state["repositories"]:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    raise LauncherError(f"批次状态中不存在仓库: {name}")


def _next_pending(state: Mapping[str, Any]) -> str | None:
    for entry in state["repositories"]:
        if isinstance(entry, Mapping) and entry.get("status") == "PENDING":
            return str(entry["name"])
    return None


def _ai_results_dir(config: ReviewConfig, batch_id: str, override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return (config.workspace.manifest_root / batch_id / "ai-results").resolve()


def _write_ai_queue(
    config: ReviewConfig,
    state: Mapping[str, Any],
    repository: str,
    tasks: Sequence[Mapping[str, Any]],
    ai_results_dir: Path,
) -> tuple[Path, Path]:
    batch_root = config.workspace.manifest_root / str(state["batch_id"])
    queue_path = batch_root / "ai-review-queue.json"
    items = []
    for task in tasks:
        task_id = str(task["task_id"])
        items.append(
            {
                "task_id": task_id,
                "repository": repository,
                "handoff": task["handoff_path"],
                "skill_trigger": "/skill-security-review",
                "expected_result": str(ai_results_dir / f"{task_id}.json"),
            }
        )
    _atomic_json(
        queue_path,
        {
            "batch_id": state["batch_id"],
            "repository": repository,
            "ai_results_dir": str(ai_results_dir),
            "items": items,
        },
    )
    instructions_path = batch_root / "AI_REVIEW_NEXT_STEPS.md"
    lines = [
        "# 当前仓库 AI 审查待办",
        "",
        f"- 批次：`{state['batch_id']}`",
        f"- 仓库：`{repository}`",
        f"- AI 结果目录：`{ai_results_dir}`",
        "",
        "在本项目根目录启动公司批准的 Claude Code，然后对每一项调用：",
        "",
        "```text",
        "/skill-security-review",
        "```",
        "",
        "把对应 handoff JSON 路径作为任务输入，并将返回的纯 JSON 保存到 expected_result。",
        "",
    ]
    if not items:
        lines.append("当前仓库没有需要 AI 审查的任务；再次执行 advance 可完成该仓库。")
    for index, item in enumerate(items, 1):
        lines.extend(
            [
                f"## {index}. `{item['task_id']}`",
                "",
                f"- handoff：`{item['handoff']}`",
                f"- 输出：`{item['expected_result']}`",
                "",
            ]
        )
    instructions_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return queue_path, instructions_path


def _prepare_next(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    ai_results_dir: Path,
) -> None:
    repository = _next_pending(state)
    if repository is None:
        _finish_batch(config, state)
        return
    inventory = load_inventory_csv(
        config.batch.inventory_csv,
        status_mapping=config.status_mapping.aliases,
    )
    plan_by_name = {
        item.repository: item
        for item in plan_repositories(inventory, included_statuses=config.batch.included_statuses)
    }
    plan = plan_by_name[repository]
    prepared = prepare_repository(
        config,
        batch_id=str(state["batch_id"]),
        repository=repository,
        rows=plan.included_rows,
    )
    entry = _repository_entry(state, repository)
    entry["status"] = "WAITING_FOR_AI"
    entry["repository_index"] = str(prepared.index_path)
    entry["task_ids"] = [task.task_id for task in prepared.tasks]
    entry["reused_task_ids"] = [task.task_id for task in prepared.reused_tasks]
    state["current_repository"] = repository
    state["status"] = "WAITING_FOR_AI"
    _save_state(config, state)
    queue, instructions = _write_ai_queue(
        config,
        state,
        repository,
        [task.to_dict() for task in prepared.tasks],
        ai_results_dir,
    )
    print(f"已准备仓库: {repository}")
    print(f"AI 审查队列: {queue}")
    print(f"下一步说明: {instructions}")


def _missing_ai_results(entry: Mapping[str, Any], ai_results_dir: Path) -> list[Path]:
    return [
        ai_results_dir / f"{task_id}.json"
        for task_id in entry.get("task_ids", [])
        if not (ai_results_dir / f"{task_id}.json").is_file()
    ]


def _finish_current(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    ai_results_dir: Path,
    confirm_cleanup: bool,
) -> bool:
    repository = state.get("current_repository")
    if not repository:
        return False
    entry = _repository_entry(state, str(repository))
    if not confirm_cleanup:
        raise LauncherError(
            "为避免多个仓库内容同时滞留在工作区，进入下一仓库前必须提供 "
            "--confirm-cleanup；脚本会先确认结果已持久化，再清理当前仓库。"
        )
    missing = _missing_ai_results(entry, ai_results_dir)
    if missing:
        print("当前仓库仍缺少 AI 结果：")
        for path in missing:
            print(f"- {path}")
        print("触发指令：/skill-security-review")
        return False
    index_path = Path(str(entry["repository_index"])).resolve()
    finalize_repository(
        config,
        batch_id=str(state["batch_id"]),
        repository_index=index_path,
        ai_results_dir=ai_results_dir,
    )
    result_index = index_path.with_name(index_path.stem + ".results.json")
    entry["result_index"] = str(result_index)
    entry["status"] = "COMPLETE"
    cleanup_repository_workspace(
        config,
        batch_id=str(state["batch_id"]),
        repository=str(repository),
        repository_index=index_path,
    )
    entry["workspace_cleaned"] = True
    state["current_repository"] = None
    state["status"] = "READY"
    _save_state(config, state)
    print(f"已完成仓库: {repository}")
    return True


def _finish_batch(config: ReviewConfig, state: dict[str, Any]) -> None:
    result_paths = [
        Path(str(entry["result_index"]))
        for entry in state["repositories"]
        if isinstance(entry, Mapping) and entry.get("result_index")
    ]
    records: list[Mapping[str, Any]] = []
    for path in result_paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        records.extend(item for item in value.get("results", []) if isinstance(item, Mapping))
    if records:
        report_dir = config.workspace.manifest_root / str(state["batch_id"]) / "reports"
        paths = write_batch_reports(
            records,
            report_dir,
            batch_id=str(state["batch_id"]),
            input_csv_sha256=str(state.get("inventory_csv_sha256") or ""),
            policy_version=config.ai.policy_version,
            generated_at=_utc_now(),
            candidate_threshold=config.quality.candidate_threshold,
            evidence_root=config.workspace.evidence_root,
        )
        state["report_paths"] = paths.as_dict()
    state["status"] = "COMPLETE"
    state["current_repository"] = None
    _save_state(config, state)
    print(f"批次已完成: {state['batch_id']}")
    if state.get("report_paths"):
        print(json.dumps(state["report_paths"], ensure_ascii=False, indent=2))


def _cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    batch_id = _validate_batch_id(args.batch_id or _default_batch_id(config.batch.batch_id_prefix))
    path = _state_path(config, batch_id)
    if path.exists():
        raise LauncherError(f"批次已存在: {batch_id}")
    state = _new_state(config, batch_id)
    _save_state(config, state)
    print(f"已生成本地执行计划（未联网、未扫描）: {path}")
    print(f"批次号: {batch_id}")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    if not args.execute:
        raise LauncherError("start 会连接 Gerrit 并运行扫描器，必须显式提供 --execute")
    config = load_config(args.config)
    _preflight_or_raise(config)
    batch_id = _validate_batch_id(args.batch_id or _default_batch_id(config.batch.batch_id_prefix))
    path = _state_path(config, batch_id)
    if path.exists():
        state = _load_state(config, batch_id)
        _check_config_binding(config, state)
        if state.get("status") != "READY" or state.get("current_repository"):
            raise LauncherError(f"批次已经启动，请使用 advance: {batch_id}")
    else:
        state = _new_state(config, batch_id)
        _save_state(config, state)
    _prepare_next(
        config,
        state,
        ai_results_dir=_ai_results_dir(config, batch_id, args.ai_results_dir),
    )
    print(f"批次号: {batch_id}")
    return 0


def _cmd_advance(args: argparse.Namespace) -> int:
    if not args.execute:
        raise LauncherError("advance 可能连接 Gerrit并运行扫描器，必须显式提供 --execute")
    config = load_config(args.config)
    _preflight_or_raise(config)
    batch_id = _validate_batch_id(args.batch_id)
    state = _load_state(config, batch_id)
    _check_config_binding(config, state)
    results_dir = _ai_results_dir(config, batch_id, args.ai_results_dir)
    if state.get("status") == "COMPLETE":
        print(f"批次已经完成: {batch_id}")
        return 0
    if state.get("current_repository"):
        if not _finish_current(
            config,
            state,
            ai_results_dir=results_dir,
            confirm_cleanup=args.confirm_cleanup,
        ):
            return 3
    _prepare_next(config, state, ai_results_dir=results_dir)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    batch_id = _validate_batch_id(args.batch_id)
    state = _load_state(config, batch_id)
    counts: dict[str, int] = {}
    for entry in state["repositories"]:
        status = str(entry.get("status"))
        counts[status] = counts.get(status, 0) + 1
    print(
        json.dumps(
            {
                "batch_id": batch_id,
                "status": state.get("status"),
                "current_repository": state.get("current_repository"),
                "repository_status_counts": counts,
                "state_path": str(_state_path(config, batch_id)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="逐仓库推进 Skill 批量安全审查。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "只生成本地计划，不联网、不扫描"),
        ("start", "校验环境并准备第一个仓库"),
        ("advance", "完成当前仓库并准备下一个仓库"),
        ("status", "查看批次状态"),
    ):
        item = subparsers.add_parser(name, help=help_text)
        item.add_argument("--config", required=True, type=Path)
        item.add_argument("--batch-id", required=name in {"advance", "status"})
        if name in {"start", "advance"}:
            item.add_argument("--execute", action="store_true")
            item.add_argument("--ai-results-dir")
        if name == "advance":
            item.add_argument("--confirm-cleanup", action="store_true")
    subparsers.choices["plan"].set_defaults(handler=_cmd_plan)
    subparsers.choices["start"].set_defaults(handler=_cmd_start)
    subparsers.choices["advance"].set_defaults(handler=_cmd_advance)
    subparsers.choices["status"].set_defaults(handler=_cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (LauncherError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
