"""Trusted state machine for the batch-review operator launcher.

New batches finish Static Preparation for every repository before exposing one
batch-wide AI queue.  The previous repository-scoped queue mode remains
readable/executable for already-created batches so in-flight state is never
silently reinterpreted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ReviewConfig, load_config
from .inventory import InventoryDocument, InventoryRow, load_inventory_csv
from .live_report import write_live_batch_report
from .per_skill import (
    PartialDownload,
    PerSkillError,
    cleanup_repository_download,
    download_repository_skills,
    finalize_skill,
    prepare_skill,
    skill_task_id,
    write_skill_html_report,
    write_skill_result_tables,
)
from .preflight import review_preflight
from .snapshot import CoverageIssue, PackageEntry, SnapshotResult
from .workflow import CURRENT_WORKFLOW_VERSION, legacy_state_is_pristine


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STATE_NAME = "per-skill-launcher-state.json"
_AI_QUEUE_MODE = "batch_wide_v2"
_REPOSITORY_AI_QUEUE_MODE = "repository_batch_v1"
_LEGACY_AI_QUEUE_MODE = "serial_v1"


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
        "schema_version": "1.1",
        "workflow_version": CURRENT_WORKFLOW_VERSION,
        "ai_policy_version": config.ai.policy_version,
        "batch_id": batch_id,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "READY",
        "static_phase_status": "PENDING",
        "config_sha256": _sha256(config.path),
        "inventory_csv_sha256": document.raw_csv_sha256,
        "inventory_csv_encoding": document.source_encoding,
        "current_task_id": None,
        "ai_queue_mode": _AI_QUEUE_MODE,
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


def _save(config: ReviewConfig, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _atomic_json(_state_path(config, str(state["batch_id"])), state)


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

    migrated = False
    workflow_version = value.get("workflow_version")
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

    stored_policy = value.get("ai_policy_version")
    if stored_policy is None:
        if not legacy_state_is_pristine(value):
            raise LauncherError(
                "旧批次已经开始执行但未冻结 AI 审查策略，不能用当前规则继续；"
                "请保留原批次证据并创建新批次"
            )
        value["ai_policy_version"] = config.ai.policy_version
        migrated = True
    elif stored_policy != config.ai.policy_version:
        raise LauncherError(
            "批次创建后 AI 审查策略发生变化；请保留原批次证据并创建新批次"
        )

    queue_mode = value.get("ai_queue_mode")
    if queue_mode is None:
        if not legacy_state_is_pristine(value):
            raise LauncherError(
                "旧批次已经开始执行但未冻结 AI Queue 协议，不能切换为 Batch-wide Queue；"
                "请保留原批次证据并创建新批次"
            )
        value["ai_queue_mode"] = _AI_QUEUE_MODE
        value["static_phase_status"] = "PENDING"
        queue_mode = _AI_QUEUE_MODE
        migrated = True
    elif queue_mode not in {_AI_QUEUE_MODE, _REPOSITORY_AI_QUEUE_MODE}:
        raise LauncherError(f"AI Queue 执行模式不兼容: {queue_mode!r}；请创建新批次")

    if queue_mode == _AI_QUEUE_MODE and "static_phase_status" not in value:
        value["static_phase_status"] = (
            "COMPLETED"
            if not any(
                isinstance(item, Mapping) and item.get("status") == "PENDING"
                for item in value["items"]
            )
            else "PENDING"
        )
        migrated = True

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


def _preflight(config: ReviewConfig) -> None:
    issues = review_preflight(config)
    if issues:
        raise LauncherError(
            "运行前检查未通过：\n" + "\n".join(f"- [{i.code}] {i.message}" for i in issues)
        )


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


def _waiting_items(
    state: Mapping[str, Any],
    *,
    repository: str | None = None,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in state.get("items", []):
        if not isinstance(item, dict) or item.get("status") != "WAITING_FOR_AI":
            continue
        if repository is not None and str(item.get("repo_name")) != repository:
            continue
        if branch is not None and str(item.get("branch")) != branch:
            continue
        items.append(item)
    return items


def _result_exists(item: Mapping[str, Any]) -> bool:
    value = item.get("ai_result_path")
    return bool(value) and Path(str(value)).is_file()


def _minimal_ai_queue_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": item["task_id"],
        "handoff": item["handoff_path"],
        "expected_result": item["ai_result_path"],
        "review_size_bytes": int(item.get("review_size_bytes") or 0),
    }


def _repository_ai_queue_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": item["task_id"],
        "skill_id": item["skill_id"],
        "skill_name": item["skill_name"],
        "repo_name": item["repo_name"],
        "branch": item["branch"],
        "handoff": item["handoff_path"],
        "expected_result": item["ai_result_path"],
        "review_size_bytes": item.get("review_size_bytes", 0),
        "skill_trigger": "/skill-security-review",
        "skill_triggers": {
            "claude_code": "/skill-security-review",
            "codex_cli": "$skill-security-review",
        },
        "review_agents": {
            "claude_code": "skill-security-reviewer",
            "codex_cli": "skill_security_reviewer",
        },
    }


def _queue_paths(config: ReviewConfig, state: Mapping[str, Any]) -> tuple[Path, Path]:
    root = config.workspace.manifest_root / str(state["batch_id"])
    return root / "ai-review-queue.json", root / "ai-review-current.json"


def _activate_batch_queue(
    config: ReviewConfig,
    state: dict[str, Any],
    waiting: Sequence[dict[str, Any]],
) -> None:
    if not waiting:
        raise LauncherError("没有可激活的 AI 审查任务")
    ready = [item for item in waiting if _result_exists(item)]
    missing = [item for item in waiting if not _result_exists(item)]
    missing.sort(
        key=lambda item: (-int(item.get("review_size_bytes") or 0), str(item.get("task_id") or ""))
    )
    current = ready[0] if ready else (missing[0] if missing else waiting[0])
    state["current_task_id"] = current["task_id"]
    state["status"] = "WAITING_FOR_AI"
    queue_path, current_path = _queue_paths(config, state)
    if missing:
        queue = {
            "schema_version": "2.0",
            "queue_mode": _AI_QUEUE_MODE,
            "batch_id": state["batch_id"],
            "scope": "BATCH",
            "max_parallel": config.concurrency.ai_reviews,
            "scheduling": "rolling_largest_first",
            "items": [_minimal_ai_queue_item(item) for item in missing],
            "skill_triggers": {
                "claude_code": "/skill-security-review",
                "codex_cli": "$skill-security-review",
            },
            "review_agents": {
                "claude_code": "skill-security-reviewer",
                "codex_cli": "skill_security_reviewer",
            },
        }
        _atomic_json(queue_path, queue)
        _atomic_json(
            current_path,
            {**queue, **_minimal_ai_queue_item(current), "items": queue["items"]},
        )
    else:
        queue_path.unlink(missing_ok=True)
        current_path.unlink(missing_ok=True)
    state["ai_queue_pending_count"] = len(missing)
    state["ai_result_ready_count"] = len(ready)
    _save(config, state)
    print(
        f"等待 Batch AI 审查: {len(missing)} 个待派发 / {len(ready)} 个结果待导入"
        f"（最多并行 {config.concurrency.ai_reviews} 个）"
    )


def _activate_repository_queue(
    config: ReviewConfig,
    state: dict[str, Any],
    waiting: Sequence[dict[str, Any]],
) -> None:
    if not waiting:
        raise LauncherError("没有可激活的 AI 审查任务")
    current = waiting[0]
    state["current_task_id"] = current["task_id"]
    state["status"] = "WAITING_FOR_AI"
    queue_path, current_path = _queue_paths(config, state)
    queue = {
        "schema_version": "1.0",
        "queue_mode": _REPOSITORY_AI_QUEUE_MODE,
        "batch_id": state["batch_id"],
        "repository": {"repo_name": current["repo_name"], "branch": current["branch"]},
        "max_parallel": config.concurrency.ai_reviews,
        "items": [_repository_ai_queue_item(item) for item in waiting],
    }
    _atomic_json(queue_path, queue)
    _atomic_json(
        current_path,
        {**queue, **_repository_ai_queue_item(current), "items": queue["items"]},
    )
    _save(config, state)


def _prepare_repository_items(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    document: InventoryDocument,
    row_lookup: Mapping[str, InventoryRow],
    group_items: Sequence[dict[str, Any]],
) -> None:
    for item in group_items:
        if item.get("status") != "PENDING":
            continue
        row = row_lookup[str(item["source_row_id"])]
        stored = item.get("download_snapshot")
        if not isinstance(stored, Mapping):
            raise LauncherError(f"Skill 缺少仓库提取快照: {item.get('skill_id')}")
        snapshot = _snapshot_from_item(item)
        item["review_size_bytes"] = int(
            snapshot.package_size_bytes or sum(entry.size for entry in snapshot.entries)
        )
        prepared = prepare_skill(
            config,
            batch_id=str(state["batch_id"]),
            row=row,
            downloaded=PartialDownload(
                Path(str(stored["task_root"])),
                Path(str(stored["task_root"])).parent,
                snapshot.source_revision,
                snapshot=snapshot,
                transport=str(item["download_transport"]),
            ),
        )
        item["index_path"] = str(prepared.index_path)
        item.pop("download_snapshot", None)
        item["workspace_cleaned"] = False
        if prepared.requires_ai:
            result_path = (
                config.workspace.manifest_root
                / str(state["batch_id"])
                / "ai-results"
                / f"{prepared.task_id}.json"
            )
            item["status"] = "WAITING_FOR_AI"
            item["handoff_path"] = str(prepared.handoff_path)
            item["ai_result_path"] = str(result_path)
        else:
            item["status"] = "COMPLETE"
        csv_path, json_path = write_skill_result_tables(
            config, document, batch_id=str(state["batch_id"])
        )
        state["result_csv"] = str(csv_path)
        state["result_json"] = str(json_path)
        _save(config, state)


def _start_repository(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    row_lookup: Mapping[str, InventoryRow],
    group_items: Sequence[dict[str, Any]],
) -> None:
    first = group_items[0]
    repository = str(first["repo_name"])
    branch = str(first["branch"])
    group_rows = [row_lookup[str(item["source_row_id"])] for item in group_items]
    print(f"开始仓库静态准备: {repository} ({branch})，Skill 数量: {len(group_rows)}")
    downloaded = download_repository_skills(
        config, batch_id=str(state["batch_id"]), rows=group_rows
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
        "skill_count": len(group_items),
    }
    state["static_phase_status"] = "RUNNING"
    _save(config, state)


def _complete_active_repository(config: ReviewConfig, state: dict[str, Any]) -> None:
    active = state.get("active_repository")
    if not isinstance(active, Mapping):
        return
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


def _write_interim_report(
    config: ReviewConfig,
    state: dict[str, Any],
    document: InventoryDocument,
) -> None:
    report = write_live_batch_report(config, document, batch_id=str(state["batch_id"]))
    state["interim_report_status"] = report.report_status
    state["interim_report_html"] = str(report.paths.html)
    state["interim_result_csv"] = str(report.current_csv)
    state["interim_result_json"] = str(report.current_json)
    state["static_phase_status"] = "COMPLETED"
    _save(config, state)


def _complete_batch(
    config: ReviewConfig,
    state: dict[str, Any],
    document: InventoryDocument,
) -> None:
    csv_path, json_path = write_skill_result_tables(
        config, document, batch_id=str(state["batch_id"])
    )
    report = write_live_batch_report(config, document, batch_id=str(state["batch_id"]))
    state["status"] = "COMPLETE"
    state["static_phase_status"] = "COMPLETED"
    state["current_task_id"] = None
    state["result_csv"] = str(csv_path)
    state["result_json"] = str(json_path)
    state["result_html"] = str(report.paths.html)
    state["report_status"] = report.report_status
    queue_path, current_path = _queue_paths(config, state)
    queue_path.unlink(missing_ok=True)
    current_path.unlink(missing_ok=True)
    _save(config, state)


def _prepare_all_static_batch(config: ReviewConfig, state: dict[str, Any]) -> None:
    document = _inventory(config)
    rows = _rows(config, document)
    row_lookup = {row.source_row_id: row for row in rows}
    while True:
        active = state.get("active_repository")
        if isinstance(active, Mapping):
            repository = str(active["repo_name"])
            branch = str(active["branch"])
            pending = [
                item
                for item in state["items"]
                if item.get("status") == "PENDING"
                and item.get("repo_name") == repository
                and item.get("branch") == branch
            ]
            if pending:
                _prepare_repository_items(
                    config,
                    state,
                    document=document,
                    row_lookup=row_lookup,
                    group_items=pending,
                )
                continue
            _complete_active_repository(config, state)
            print(f"仓库静态阶段完成并已清理: {repository} ({branch})")
            continue

        next_item = next(
            (item for item in state["items"] if item.get("status") == "PENDING"),
            None,
        )
        if next_item is not None:
            repository = str(next_item["repo_name"])
            branch = str(next_item["branch"])
            group_items = [
                item
                for item in state["items"]
                if item.get("status") == "PENDING"
                and item.get("repo_name") == repository
                and item.get("branch") == branch
            ]
            _start_repository(config, state, row_lookup=row_lookup, group_items=group_items)
            continue

        _write_interim_report(config, state, document)
        waiting = _waiting_items(state)
        if waiting:
            _activate_batch_queue(config, state, waiting)
            return
        _complete_batch(config, state, document)
        return


def _prepare_next_repository_compat(config: ReviewConfig, state: dict[str, Any]) -> None:
    document = _inventory(config)
    rows = _rows(config, document)
    row_lookup = {row.source_row_id: row for row in rows}
    while True:
        active = state.get("active_repository")
        if isinstance(active, Mapping):
            repository = str(active["repo_name"])
            branch = str(active["branch"])
            waiting = _waiting_items(state, repository=repository, branch=branch)
            if waiting:
                _activate_repository_queue(config, state, waiting)
                return
            pending = [
                item
                for item in state["items"]
                if item.get("status") == "PENDING"
                and item.get("repo_name") == repository
                and item.get("branch") == branch
            ]
            if pending:
                _prepare_repository_items(
                    config,
                    state,
                    document=document,
                    row_lookup=row_lookup,
                    group_items=pending,
                )
                continue
            _complete_active_repository(config, state)
            continue

        next_item = next(
            (item for item in state["items"] if item.get("status") == "PENDING"),
            None,
        )
        if next_item is None:
            _complete_batch(config, state, document)
            return
        repository = str(next_item["repo_name"])
        branch = str(next_item["branch"])
        group_items = [
            item
            for item in state["items"]
            if item.get("status") == "PENDING"
            and item.get("repo_name") == repository
            and item.get("branch") == branch
        ]
        _start_repository(config, state, row_lookup=row_lookup, group_items=group_items)


def _prepare_next(config: ReviewConfig, state: dict[str, Any]) -> None:
    mode = state.get("ai_queue_mode")
    if mode == _AI_QUEUE_MODE:
        _prepare_all_static_batch(config, state)
    elif mode == _REPOSITORY_AI_QUEUE_MODE:
        _prepare_next_repository_compat(config, state)
    else:
        raise LauncherError(
            f"当前批次 AI Queue 模式不支持继续执行: {mode or _LEGACY_AI_QUEUE_MODE}"
        )


def _import_ready_ai_results(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    waiting: Sequence[dict[str, Any]],
) -> int:
    document = _inventory(config)
    imported = 0
    for item in list(waiting):
        if not _result_exists(item):
            continue
        finalize_skill(
            config,
            index_path=Path(str(item["index_path"])),
            ai_result_path=Path(str(item["ai_result_path"])),
        )
        item["status"] = "COMPLETE"
        item["workspace_cleaned"] = True
        imported += 1
    if imported:
        csv_path, json_path = write_skill_result_tables(
            config, document, batch_id=str(state["batch_id"])
        )
        state["result_csv"] = str(csv_path)
        state["result_json"] = str(json_path)
        live = write_live_batch_report(config, document, batch_id=str(state["batch_id"]))
        state["result_html"] = str(live.paths.html)
        _save(config, state)
    return imported


def _finish_current_batch_wide(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    confirm_cleanup: bool,
) -> None:
    del confirm_cleanup
    waiting = _waiting_items(state)
    _import_ready_ai_results(config, state, waiting=waiting)
    remaining = _waiting_items(state)
    if remaining:
        _activate_batch_queue(config, state, remaining)
        return
    state["current_task_id"] = None
    state["status"] = "READY"
    _save(config, state)


def _finish_current_repository_compat(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    confirm_cleanup: bool,
) -> None:
    if not confirm_cleanup:
        raise LauncherError("旧仓库级批次进入下一仓库前必须提供 --confirm-cleanup")
    active = state.get("active_repository")
    if not isinstance(active, Mapping):
        state["current_task_id"] = None
        return
    repository = str(active["repo_name"])
    branch = str(active["branch"])
    waiting = _waiting_items(state, repository=repository, branch=branch)
    _import_ready_ai_results(config, state, waiting=waiting)
    remaining = _waiting_items(state, repository=repository, branch=branch)
    if remaining:
        _activate_repository_queue(config, state, remaining)
        return
    state["current_task_id"] = None
    state["status"] = "READY"
    _save(config, state)


def _finish_current(
    config: ReviewConfig,
    state: dict[str, Any],
    *,
    confirm_cleanup: bool,
) -> None:
    mode = state.get("ai_queue_mode")
    if mode == _AI_QUEUE_MODE:
        _finish_current_batch_wide(config, state, confirm_cleanup=confirm_cleanup)
    elif mode == _REPOSITORY_AI_QUEUE_MODE:
        _finish_current_repository_compat(config, state, confirm_cleanup=confirm_cleanup)
    else:
        raise LauncherError(
            f"当前批次 AI Queue 模式不支持推进: {mode or _LEGACY_AI_QUEUE_MODE}"
        )


def _cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    identifier = _batch_id(args.batch_id or _default_batch_id(config.batch.batch_id_prefix))
    if _state_path(config, identifier).exists():
        raise LauncherError(f"批次已存在: {identifier}")
    state = _new_state(config, identifier)
    _save(config, state)
    print(f"已生成执行计划: {_state_path(config, identifier)}")
    print(f"Skill 数量: {len(state['items'])}")
    print(f"AI Queue 模式: {state['ai_queue_mode']}")
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
        raise LauncherError(f"批次已有进行中的 AI 任务，请使用 advance: {identifier}")
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


def _cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = _load_state(config, _batch_id(args.batch_id))
    document = _inventory(config)
    if state.get("status") == "COMPLETE":
        csv_path, json_path = write_skill_result_tables(
            config, document, batch_id=str(state["batch_id"])
        )
        html_path = write_skill_html_report(config, document, batch_id=str(state["batch_id"]))
        state["result_csv"] = str(csv_path)
        state["result_json"] = str(json_path)
        state["result_html"] = str(html_path)
        _save(config, state)
        return 0
    live = write_live_batch_report(config, document, batch_id=str(state["batch_id"]))
    state["interim_report_status"] = live.report_status
    state["interim_report_html"] = str(live.paths.html)
    state["interim_result_csv"] = str(live.current_csv)
    state["interim_result_json"] = str(live.current_json)
    _save(config, state)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = _load_state(config, _batch_id(args.batch_id))
    counts: dict[str, int] = {}
    for item in state["items"]:
        status = str(item.get("status"))
        counts[status] = counts.get(status, 0) + 1
    queue_path, _ = _queue_paths(config, state)
    print(
        json.dumps(
            {
                "batch_id": state["batch_id"],
                "status": state["status"],
                "static_phase_status": state.get("static_phase_status"),
                "workflow_version": state.get("workflow_version"),
                "ai_queue_mode": state.get("ai_queue_mode"),
                "current_task_id": state.get("current_task_id"),
                "active_repository": state.get("active_repository"),
                "ai_queue_path": (
                    str(queue_path)
                    if state.get("status") == "WAITING_FOR_AI" and queue_path.is_file()
                    else None
                ),
                "skill_status_counts": counts,
                "result_paths": {
                    "csv": state.get("result_csv") or state.get("interim_result_csv"),
                    "json": state.get("result_json") or state.get("interim_result_json"),
                    "html": state.get("result_html") or state.get("interim_report_html"),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="逐 Skill 下载、归档和安全审查。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "start", "advance", "report", "status"):
        item = commands.add_parser(name)
        item.add_argument("--config", required=True, type=Path)
        item.add_argument("--batch-id", required=name in {"advance", "report", "status"})
        if name in {"start", "advance"}:
            item.add_argument("--execute", action="store_true")
        if name == "advance":
            item.add_argument("--confirm-cleanup", action="store_true")
    commands.choices["plan"].set_defaults(handler=_cmd_plan)
    commands.choices["start"].set_defaults(handler=_cmd_start)
    commands.choices["advance"].set_defaults(handler=_cmd_advance)
    commands.choices["report"].set_defaults(handler=_cmd_report)
    commands.choices["status"].set_defaults(handler=_cmd_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (LauncherError, PerSkillError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=os.sys.stderr)
        return 2


__all__ = [
    "LauncherError",
    "CURRENT_WORKFLOW_VERSION",
    "PartialDownload",
    "PackageEntry",
    "SnapshotResult",
    "cleanup_repository_download",
    "download_repository_skills",
    "finalize_skill",
    "prepare_skill",
    "skill_task_id",
    "write_live_batch_report",
    "write_skill_html_report",
    "write_skill_result_tables",
    "_AI_QUEUE_MODE",
    "_REPOSITORY_AI_QUEUE_MODE",
    "_LEGACY_AI_QUEUE_MODE",
    "_new_state",
    "_load_state",
    "_save",
    "_inventory",
    "_prepare_next",
    "_finish_current",
    "_cmd_report",
    "_cmd_plan",
    "_cmd_start",
    "_cmd_advance",
    "_cmd_status",
    "main",
]
