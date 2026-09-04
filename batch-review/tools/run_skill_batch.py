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
    PartialDownload,
    PerSkillError,
    cleanup_repository_download,
    cleanup_skill_download,
    download_repository_skills,
    finalize_skill,
    prepare_skill,
    skill_task_id,
    write_skill_result_tables,
)
from skill_batch_review.preflight import review_preflight  # noqa: E402
from skill_batch_review.snapshot import CoverageIssue, PackageEntry, SnapshotResult  # noqa: E402
from skill_batch_review.workflow import (  # noqa: E402
    CURRENT_WORKFLOW_VERSION,
    legacy_state_is_pristine,
)


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
        "workflow_version": CURRENT_WORKFLOW_VERSION,
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
                "repo_name": row.repo_name,
                "branch": row.branch,
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
    document = _inventory(config)
    if value.get("inventory_csv_sha256") != document.raw_csv_sha256:
        raise LauncherError("批次创建后 CSV 内容发生变化；请保留原批次证据并创建新批次")
    workflow_version = value.get("workflow_version")
    migrated = False
    if workflow_version is None:
        if not legacy_state_is_pristine(value):
            raise LauncherError(
                "旧批次已经开始执行，不能切换到当前按仓库归档流程继续运行；"
                "请保留原批次证据并创建新批次"
            )
        value["workflow_version"] = CURRENT_WORKFLOW_VERSION
        migrated = True
    elif workflow_version != CURRENT_WORKFLOW_VERSION:
        raise LauncherError(f"批次执行模式不兼容: {workflow_version!r}；请创建新批次")
    row_lookup = {row.source_row_id: row for row in _rows(config, document)}
    for item in value["items"]:
        if not isinstance(item, dict):
            continue
        row = row_lookup.get(str(item.get("source_row_id") or ""))
        if row is None:
            continue
        if not item.get("repo_name"):
            item["repo_name"] = row.repo_name
            migrated = True
        if not item.get("branch"):
            item["branch"] = row.branch
            migrated = True
    if migrated:
        _save(config, value)
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


def _activate_waiting(config: ReviewConfig, state: dict[str, Any], item: dict[str, Any]) -> None:
    state["current_task_id"] = item["task_id"]
    state["status"] = "WAITING_FOR_AI"
    queue = {
        "batch_id": state["batch_id"],
        "task_id": item["task_id"],
        "skill_id": item["skill_id"],
        "skill_name": item["skill_name"],
        "repo_name": item["repo_name"],
        "branch": item["branch"],
        "handoff": item["handoff_path"],
        "expected_result": item["ai_result_path"],
        "skill_trigger": "/skill-security-review",
    }
    _atomic_json(
        config.workspace.manifest_root / str(state["batch_id"]) / "ai-review-current.json",
        queue,
    )
    _save(config, state)
    print(f"等待 AI 审查: {item['skill_id']}/{item['skill_name']}")


def _snapshot_from_item(item: Mapping[str, Any]) -> SnapshotResult:
    stored = item.get("download_snapshot")
    if not isinstance(stored, Mapping):
        raise LauncherError("当前 Skill 缺少仓库提取快照")
    manifest = stored.get("manifest")
    if not isinstance(manifest, Mapping):
        raise LauncherError("当前 Skill 的仓库提取快照无效")
    entries = tuple(
        PackageEntry(
            relative_path=str(entry["relative_path"]),
            file_type=str(entry["type"]),
            mode=str(entry["mode"]),
            size=int(entry["size"]),
            sha256=entry.get("sha256"),
            symlink_target=entry.get("symlink_target"),
            git_object_id=entry.get("git_object_id"),
        )
        for entry in manifest.get("entries", [])
        if isinstance(entry, Mapping)
    )
    issues = tuple(
        CoverageIssue(
            code=str(issue["code"]),
            path=issue.get("path"),
            detail=str(issue["detail"]),
            blocking=bool(issue.get("blocking", True)),
        )
        for issue in manifest.get("coverage_issues", [])
        if isinstance(issue, Mapping)
    )
    return SnapshotResult(
        repository=str(manifest["repository"]),
        source_revision=str(manifest["source_revision"]),
        skill_path=str(manifest["skill_path"]),
        snapshot_path=Path(str(stored["snapshot_path"])),
        entries=entries,
        skill_digest=str(manifest["skill_digest"]),
        coverage_issues=issues,
        package_size_bytes=int(manifest.get("package_size_bytes", 0)),
    )


def _prepare_next(config: ReviewConfig, state: dict[str, Any]) -> None:
    """Download one repository, then review its extracted Skills one by one."""

    document = _inventory(config)
    rows = _rows(config, document)
    row_lookup = {row.source_row_id: row for row in rows}
    waiting = next(
        (item for item in state["items"] if item.get("status") == "WAITING_FOR_AI"),
        None,
    )
    if waiting is not None:
        _activate_waiting(config, state, waiting)
        return

    while True:
        next_item = next(
            (item for item in state["items"] if item.get("status") == "PENDING"),
            None,
        )
        if next_item is None:
            active = state.get("active_repository")
            if isinstance(active, Mapping):
                repository = str(active["repo_name"])
                branch = str(active["branch"])
                cleanup_repository_download(
                    config,
                    batch_id=str(state["batch_id"]),
                    repository=repository,
                    branch=branch,
                )
                for item in state["items"]:
                    if item.get("repo_name") == repository and item.get("branch") == branch:
                        item["workspace_cleaned"] = True
                state.setdefault("completed_repositories", []).append(
                    {**dict(active), "completed_at": _utc_now()}
                )
                state["active_repository"] = None
                _save(config, state)
            csv_path, json_path = write_skill_result_tables(
                config, document, batch_id=str(state["batch_id"])
            )
            state["status"] = "COMPLETE"
            state["current_task_id"] = None
            state["result_csv"] = str(csv_path)
            state["result_json"] = str(json_path)
            _save(config, state)
            print(f"批次已完成: {state['batch_id']}")
            return

        active = state.get("active_repository")
        if isinstance(active, Mapping):
            repository = str(active["repo_name"])
            branch = str(active["branch"])
            active_pending = next(
                (
                    item
                    for item in state["items"]
                    if item.get("status") == "PENDING"
                    and item.get("repo_name") == repository
                    and item.get("branch") == branch
                ),
                None,
            )
            if active_pending is None:
                cleanup_repository_download(
                    config,
                    batch_id=str(state["batch_id"]),
                    repository=repository,
                    branch=branch,
                )
                for item in state["items"]:
                    if item.get("repo_name") == repository and item.get("branch") == branch:
                        item["workspace_cleaned"] = True
                state.setdefault("completed_repositories", []).append(
                    {
                        **dict(active),
                        "completed_at": _utc_now(),
                    }
                )
                state["active_repository"] = None
                _save(config, state)
                print(f"仓库已完成，自动进入下一仓库: {repository}")
                continue
            next_item = active_pending
        else:
            repository = str(next_item["repo_name"])
            branch = str(next_item["branch"])
            group_items = [
                item
                for item in state["items"]
                if item.get("status") == "PENDING"
                and item.get("repo_name") == repository
                and item.get("branch") == branch
            ]
            group_rows = [row_lookup[str(item["source_row_id"])] for item in group_items]
            print(f"开始仓库: {repository} ({branch})，Skill 数量: {len(group_rows)}")
            downloaded = download_repository_skills(
                config,
                batch_id=str(state["batch_id"]),
                rows=group_rows,
            )
            for item, row in zip(group_items, group_rows):
                skill_download = downloaded.skills[row.source_row_id]
                if skill_download.snapshot is None:
                    raise LauncherError("仓库归档没有生成 Skill 快照")
                item["task_id"] = skill_task_id(row)
                item["download_snapshot"] = {
                    "snapshot_path": str(skill_download.snapshot.snapshot_path),
                    "task_root": str(skill_download.task_root),
                    "manifest": skill_download.snapshot.manifest_dict(),
                }
                item["source_revision"] = downloaded.revision
                item["download_transport"] = downloaded.transport
            state["active_repository"] = {
                "repo_name": repository,
                "branch": branch,
                "source_revision": downloaded.revision,
                "skill_count": len(group_rows),
            }
            _save(config, state)
            next_item = group_items[0]

        row = row_lookup[str(next_item["source_row_id"])]
        stored = next_item["download_snapshot"]
        snapshot = _snapshot_from_item(next_item)
        prepared = prepare_skill(
            config,
            batch_id=str(state["batch_id"]),
            row=row,
            downloaded=PartialDownload(
                Path(str(stored["task_root"])),
                Path(str(stored["task_root"])).parent,
                snapshot.source_revision,
                snapshot=snapshot,
                transport=str(next_item["download_transport"]),
            ),
        )
        next_item["index_path"] = str(prepared.index_path)
        next_item.pop("download_snapshot", None)
        if prepared.requires_ai:
            result_path = (
                config.workspace.manifest_root
                / str(state["batch_id"])
                / "ai-results"
                / f"{prepared.task_id}.json"
            )
            next_item["status"] = "WAITING_FOR_AI"
            next_item["handoff_path"] = str(prepared.handoff_path)
            next_item["ai_result_path"] = str(result_path)
            _activate_waiting(config, state, next_item)
            return

        next_item["status"] = "COMPLETE"
        next_item["workspace_cleaned"] = False
        csv_path, json_path = write_skill_result_tables(
            config, document, batch_id=str(state["batch_id"])
        )
        state["result_csv"] = str(csv_path)
        state["result_json"] = str(json_path)
        _save(config, state)
        print(f"Skill 无需 AI 等待，自动继续: {prepared.skill_id}/{row.skill_name}")


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
    if (
        not item.get("workspace_cleaned")
        and item.get("download_transport") != "whole_repository_archive"
    ):
        cleanup_skill_download(
            config,
            batch_id=str(state["batch_id"]),
            task_id=str(task_id),
        )
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
