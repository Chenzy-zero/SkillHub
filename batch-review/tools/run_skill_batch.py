#!/usr/bin/env python3
"""Operator launcher for the one-Skill-at-a-time workflow."""

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
from skill_batch_review.inventory import InventoryDocument, InventoryRow, load_inventory_csv  # noqa: E402
from skill_batch_review.per_skill import (  # noqa: E402
    PerSkillError,
    cleanup_skill_download,
    finalize_skill,
    prepare_skill,
    skill_task_id,
    write_skill_result_tables,
)
from skill_batch_review.preflight import review_preflight  # noqa: E402


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STATE_NAME = "per-skill-launcher-state.json"


class LauncherError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _batch_id(value: str) -> str:
    if not _BATCH_ID_RE.fullmatch(value):
        raise LauncherError("批次号必须为 1-64 位字母、数字、点、下划线或连字符")
    return value


def _default_batch_id(prefix: str) -> str:
    return _batch_id(f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
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


def _inventory(config: ReviewConfig) -> InventoryDocument:
    return load_inventory_csv(
        config.batch.inventory_csv,
        status_mapping=config.status_mapping.aliases,
    )


def _rows(config: ReviewConfig, document: InventoryDocument) -> tuple[InventoryRow, ...]:
    included = tuple(row for row in document.rows if row.status in config.batch.included_statuses)
    seen: dict[str, InventoryRow] = {}
    for row in included:
        identifier = row.trace_values.get("skill_id", "")
        if not identifier:
            raise LauncherError(f"CSV 第 {row.row_number} 行缺少 skill_id")
        if identifier in seen:
            raise LauncherError(
                f"CSV 中 skill_id 重复: {identifier}（第 {seen[identifier].row_number}、{row.row_number} 行）；"
                "逐 Skill 模式要求上游只保留最终版本"
            )
        seen[identifier] = row
    return included


def _new_state(config: ReviewConfig, batch_id: str) -> dict[str, Any]:
    document = _inventory(config)
    rows = _rows(config, document)
    return {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "READY",
        "config_sha256": _sha256(config.path),
        "inventory_csv_sha256": document.raw_csv_sha256,
        "inventory_csv_encoding": document.source_encoding,
        "current_task_id": None,
        "items": [
            {
                "source_row_id": row.source_row_id,
                "skill_id": row.trace_values["skill_id"],
                "skill_name": row.skill_name,
                "status": "PENDING",
                "index_path": None,
            }
            for row in rows
        ],
    }


def _load_state(config: ReviewConfig, batch_id: str) -> dict[str, Any]:
    path = _state_path(config, batch_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LauncherError(f"批次不存在，请先执行 plan 或 start: {batch_id}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise LauncherError(f"批次状态无效: {path}")
    if value.get("config_sha256") != _sha256(config.path):
        raise LauncherError("批次创建后配置文件发生变化，请恢复原配置或创建新批次")
    return value


def _save(config: ReviewConfig, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _atomic_json(_state_path(config, str(state["batch_id"])), state)


def _item(state: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for item in state["items"]:
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return item
    raise LauncherError(f"批次中不存在任务: {task_id}")


def _row_by_id(rows: Sequence[InventoryRow], source_row_id: str) -> InventoryRow:
    for row in rows:
        if row.source_row_id == source_row_id:
            return row
    raise LauncherError(f"当前 CSV 中找不到原始行: {source_row_id}")


def _preflight(config: ReviewConfig) -> None:
    issues = review_preflight(config)
    if issues:
        raise LauncherError("运行前检查未通过：\n" + "\n".join(f"- [{i.code}] {i.message}" for i in issues))


def _prepare_next(config: ReviewConfig, state: dict[str, Any]) -> None:
    next_item = next((item for item in state["items"] if item.get("status") == "PENDING"), None)
    if next_item is None:
        document = _inventory(config)
        csv_path, json_path = write_skill_result_tables(
            config, document, batch_id=str(state["batch_id"])
        )
        state["status"] = "COMPLETE"
        state["result_csv"] = str(csv_path)
        state["result_json"] = str(json_path)
        _save(config, state)
        print(f"批次已完成: {state['batch_id']}")
        return
    rows = _rows(config, _inventory(config))
    row = _row_by_id(rows, str(next_item["source_row_id"]))
    next_item["task_id"] = skill_task_id(row)
    try:
        prepared = prepare_skill(config, batch_id=str(state["batch_id"]), row=row)
    except Exception:
        if not config.workspace.keep_failed_workspace:
            cleanup_skill_download(
                config,
                batch_id=str(state["batch_id"]),
                task_id=str(next_item["task_id"]),
            )
        raise
    next_item["task_id"] = prepared.task_id
    next_item["index_path"] = str(prepared.index_path)
    next_item["download_root"] = str(prepared.download_root)
    next_item["status"] = "WAITING_FOR_AI" if prepared.requires_ai else "READY_TO_ADVANCE"
    state["current_task_id"] = prepared.task_id
    state["status"] = next_item["status"]
    if prepared.handoff_path:
        result_path = config.workspace.manifest_root / str(state["batch_id"]) / "ai-results" / f"{prepared.task_id}.json"
        next_item["ai_result_path"] = str(result_path)
        queue = {
            "batch_id": state["batch_id"],
            "task_id": prepared.task_id,
            "skill_id": prepared.skill_id,
            "skill_name": row.skill_name,
            "handoff": str(prepared.handoff_path),
            "expected_result": str(result_path),
            "skill_trigger": "/skill-security-review",
        }
        _atomic_json(config.workspace.manifest_root / str(state["batch_id"]) / "ai-review-current.json", queue)
    _save(config, state)
    print(f"已准备 Skill: {prepared.skill_id}/{row.skill_name}")
    print(f"状态: {next_item['status']}")


def _finish_current(config: ReviewConfig, state: dict[str, Any], *, confirm_cleanup: bool) -> None:
    task_id = state.get("current_task_id")
    if not task_id:
        return
    if not confirm_cleanup:
        raise LauncherError("进入下一个 Skill 前必须提供 --confirm-cleanup")
    item = _item(state, str(task_id))
    if item["status"] == "WAITING_FOR_AI":
        ai_path = Path(str(item["ai_result_path"]))
        if not ai_path.is_file():
            raise LauncherError(f"缺少 AI 结果: {ai_path}\n触发指令: /skill-security-review")
        finalize_skill(config, index_path=Path(str(item["index_path"])), ai_result_path=ai_path)
    elif item["status"] != "READY_TO_ADVANCE":
        raise LauncherError(f"当前 Skill 状态不能完成: {item['status']}")
    document = _inventory(config)
    csv_path, json_path = write_skill_result_tables(
        config, document, batch_id=str(state["batch_id"])
    )
    cleanup_skill_download(config, batch_id=str(state["batch_id"]), task_id=str(task_id))
    item["status"] = "COMPLETE"
    item["result_csv"] = str(csv_path)
    item["result_json"] = str(json_path)
    item["workspace_cleaned"] = True
    state["current_task_id"] = None
    state["status"] = "READY"
    _save(config, state)
    print(f"已完成 Skill: {item['skill_id']}/{item['skill_name']}")


def _cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    identifier = _batch_id(args.batch_id or _default_batch_id(config.batch.batch_id_prefix))
    if _state_path(config, identifier).exists():
        raise LauncherError(f"批次已存在: {identifier}")
    state = _new_state(config, identifier)
    _save(config, state)
    print(f"已生成逐 Skill 执行计划: {_state_path(config, identifier)}")
    print(f"Skill 数量: {len(state['items'])}")
    print(f"批次号: {identifier}")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    if not args.execute:
        raise LauncherError("start 会连接 Gerrit 并执行静态扫描，必须提供 --execute")
    config = load_config(args.config)
    _preflight(config)
    identifier = _batch_id(args.batch_id or _default_batch_id(config.batch.batch_id_prefix))
    if _state_path(config, identifier).exists():
        state = _load_state(config, identifier)
    else:
        state = _new_state(config, identifier)
        _save(config, state)
    if state.get("current_task_id"):
        raise LauncherError(f"批次已有进行中的 Skill，请使用 advance: {identifier}")
    _prepare_next(config, state)
    print(f"批次号: {identifier}")
    return 0


def _cmd_advance(args: argparse.Namespace) -> int:
    if not args.execute:
        raise LauncherError("advance 会推进审查并可能连接 Gerrit，必须提供 --execute")
    config = load_config(args.config)
    _preflight(config)
    state = _load_state(config, _batch_id(args.batch_id))
    if state.get("status") == "COMPLETE":
        print(f"批次已经完成: {state['batch_id']}")
        return 0
    _finish_current(config, state, confirm_cleanup=args.confirm_cleanup)
    _prepare_next(config, state)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = _load_state(config, _batch_id(args.batch_id))
    counts: dict[str, int] = {}
    for item in state["items"]:
        status = str(item.get("status"))
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"batch_id": state["batch_id"], "status": state["status"], "current_task_id": state.get("current_task_id"), "skill_status_counts": counts}, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="逐 Skill 下载、归档和安全审查。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "start", "advance", "status"):
        item = commands.add_parser(name)
        item.add_argument("--config", required=True, type=Path)
        item.add_argument("--batch-id", required=name in {"advance", "status"})
        if name in {"start", "advance"}:
            item.add_argument("--execute", action="store_true")
        if name == "advance":
            item.add_argument("--confirm-cleanup", action="store_true")
    commands.choices["plan"].set_defaults(handler=_cmd_plan)
    commands.choices["start"].set_defaults(handler=_cmd_start)
    commands.choices["advance"].set_defaults(handler=_cmd_advance)
    commands.choices["status"].set_defaults(handler=_cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (LauncherError, PerSkillError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
