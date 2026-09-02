"""Command-line entry points for planning and operator-controlled execution.

The commands keep side-effect boundaries visible: planning is local-only,
``prepare-repository`` is the explicit Gerrit/scanner boundary,
``finalize-repository`` imports a manually produced AI result, and cleanup
requires a separate command plus an explicit confirmation flag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .config import ConfigError, ReviewConfig, load_config
from .inventory import InventoryDocument, InventoryError, InventoryRow, load_inventory_csv
from .preflight import review_preflight
from .artifacts import ArtifactError
from .git_source import GitSourceError
from .orchestrator import (
    OrchestrationError,
    cleanup_repository_workspace,
    finalize_repository,
    plan_repositories,
    prepare_repository,
)
from .reporting import write_batch_reports
from .scanners import ScannerError
from .snapshot import SnapshotError


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _default_batch_id(prefix: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    value = f"{prefix}-{timestamp}"
    if not _BATCH_ID_RE.fullmatch(value):
        raise ConfigError("batch.batch_id_prefix produces an invalid batch id")
    return value


def _validate_batch_id(value: str) -> str:
    if not _BATCH_ID_RE.fullmatch(value):
        raise ConfigError(
            "batch id must be 1-64 characters using letters, numbers, '.', '_' or '-'"
        )
    return value


def _resolve_output(value: str, *, config: ReviewConfig, batch_id: str) -> Optional[Path]:
    if value == "-":
        return None
    if value:
        return Path(value).expanduser().resolve()
    return (config.workspace.manifest_root / batch_id / "batch-manifest.json").resolve()


def _validate_records(config: ReviewConfig, records: Sequence[InventoryRow]) -> None:
    """Validate non-network source references before creating a batch plan."""

    for record in records:
        try:
            # Rendering validates the configured repository allowlist and the
            # URL template.  It does not resolve the branch or contact Gerrit.
            config.gerrit.repository_url(record.repo_name, branch=record.branch)
        except ConfigError as exc:
            row_label = f" row {record.row_number}" if record.row_number else ""
            raise ConfigError(f"inventory record{row_label}: {exc}") from exc


def _build_manifest(
    config: ReviewConfig,
    inventory: InventoryDocument,
    *,
    batch_id: str,
    input_csv: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "execution_mode": "plan-only",
        "network_accessed": False,
        "scanners_executed": False,
        "config_file": str(config.path),
        "inventory_csv": str(input_csv),
        "inventory_csv_sha256": inventory.raw_csv_sha256,
        "source_row_count": inventory.raw_row_count,
        "execution_record_count": inventory.row_count,
        "exact_duplicate_count": inventory.duplicate_count,
        "input_conflict_count": sum(row.has_input_conflict for row in inventory.rows),
        "records": [record.to_dict() for record in inventory.rows],
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise ConfigError(f"cannot write manifest {path}: {exc}") from exc


def _cmd_validate_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.json:
        print(json.dumps({"status": "valid", **config.summary()}, ensure_ascii=False, indent=2))
    else:
        print(f"配置有效: {config.path}")
        print(f"扫描器: {', '.join(sorted(config.scanners))}")
        print(f"质量门槛: {config.quality.candidate_threshold}/{config.quality.max_score}")
    return 0


def _cmd_init_batch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    batch_id = _validate_batch_id(args.batch_id) if args.batch_id else _default_batch_id(
        config.batch.batch_id_prefix
    )
    input_csv = (
        Path(args.csv).expanduser().resolve()
        if args.csv
        else config.batch.inventory_csv
    )
    inventory = load_inventory_csv(input_csv, status_mapping=config.status_mapping.aliases)
    if not inventory.rows:
        raise ConfigError("inventory CSV contains no data rows")
    _validate_records(config, inventory.rows)
    manifest = _build_manifest(config, inventory, batch_id=batch_id, input_csv=input_csv)
    output = _resolve_output(args.output, config=config, batch_id=batch_id)
    if output is None:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _write_json(output, manifest)
        print(f"已生成批次清单（未联网、未扫描）: {output}")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    issues = review_preflight(config)
    payload = {
        "status": "ready" if not issues else "blocked",
        "network_accessed": False,
        "tools_executed": False,
        "issues": [issue.to_dict() for issue in issues],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif issues:
        print("运行前检查未通过：")
        for issue in issues:
            print(f"- [{issue.code}] {issue.message}")
    else:
        print("运行前本地检查通过（尚未验证 Gerrit 网络和权限）")
    return 0 if not issues else 2


def _load_execution_inventory(config: ReviewConfig, csv_value: str | None) -> tuple[Path, InventoryDocument]:
    path = Path(csv_value).expanduser().resolve() if csv_value else config.batch.inventory_csv
    document = load_inventory_csv(path, status_mapping=config.status_mapping.aliases)
    return path, document


def _cmd_plan_repositories(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    input_path, inventory = _load_execution_inventory(config, args.csv)
    plans = plan_repositories(inventory, included_statuses=config.batch.included_statuses)
    payload = {
        "schema_version": "0.1",
        "execution_mode": "plan-only",
        "network_accessed": False,
        "scanners_executed": False,
        "inventory_csv": str(input_path),
        "inventory_csv_sha256": inventory.raw_csv_sha256,
        "included_statuses": list(config.batch.included_statuses),
        "repository_count": len(plans),
        "repositories_to_prepare": [plan.repository for plan in plans if plan.included_rows],
        "plans": [plan.to_dict() for plan in plans],
    }
    if args.output == "-":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        output = (
            Path(args.output).expanduser().resolve()
            if args.output
            else config.workspace.manifest_root / "repository-plan.json"
        )
        _write_json(output, payload)
        print(f"已生成仓库执行计划（未联网、未扫描）: {output}")
    return 0


def _cmd_prepare_repository(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    issues = review_preflight(config)
    if issues:
        details = "; ".join(f"[{issue.code}] {issue.message}" for issue in issues)
        raise OrchestrationError(f"运行前检查未通过: {details}")
    _, inventory = _load_execution_inventory(config, args.csv)
    plans = {
        plan.repository: plan
        for plan in plan_repositories(inventory, included_statuses=config.batch.included_statuses)
    }
    plan = plans.get(args.repository)
    if plan is None:
        raise OrchestrationError(f"CSV 中不存在仓库: {args.repository}")
    if not plan.included_rows:
        raise OrchestrationError(f"仓库没有符合 included_statuses 的 Skill: {args.repository}")
    prepared = prepare_repository(
        config,
        batch_id=_validate_batch_id(args.batch_id),
        repository=args.repository,
        rows=plan.included_rows,
    )
    payload = {
        "repository_index": str(prepared.index_path),
        "task_count": len(prepared.tasks),
        "conflict_count": len(prepared.conflicts),
        "ai_review_queue": [
            {
                "task_id": task.task_id,
                "handoff": str(task.handoff_path),
                "expected_result_filename": f"{task.task_id}.json",
                "invoke_skill": "/skill-security-review",
            }
            for task in prepared.tasks
        ],
        "model_invoked": False,
        "candidate_exported": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_finalize_repository(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    results = finalize_repository(
        config,
        batch_id=_validate_batch_id(args.batch_id),
        repository_index=Path(args.repository_index).expanduser().resolve(),
        ai_results_dir=Path(args.ai_results_dir).expanduser().resolve(),
    )
    print(
        json.dumps(
            {
                "result_count": len(results),
                "candidate_count": sum(
                    result.get("candidate_status") == "EXPORTED_LOCAL" for result in results
                ),
                "repository_result_index": str(
                    Path(args.repository_index).expanduser().resolve().with_name(
                        Path(args.repository_index).stem + ".results.json"
                    )
                ),
                "commit_performed": False,
                "push_performed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_cleanup_repository(args: argparse.Namespace) -> int:
    if not args.confirm_cleanup:
        raise OrchestrationError("cleanup requires --confirm-cleanup")
    config = load_config(args.config)
    removed = cleanup_repository_workspace(
        config,
        batch_id=_validate_batch_id(args.batch_id),
        repository=args.repository,
        repository_index=Path(args.repository_index).expanduser().resolve(),
    )
    print("已清理仓库临时工作区" if removed else "仓库临时工作区已不存在")
    return 0


def _cmd_report_batch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    inventory = load_inventory_csv(
        config.batch.inventory_csv,
        status_mapping=config.status_mapping.aliases,
    )
    result_dir = Path(args.results_dir).expanduser().resolve()
    records: list[Mapping[str, Any]] = []
    for path in sorted(result_dir.glob("*.results.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or not isinstance(value.get("results"), list):
            raise OrchestrationError(f"结果索引格式无效: {path}")
        records.extend(item for item in value["results"] if isinstance(item, Mapping))
    if not records:
        raise OrchestrationError(f"结果目录中没有可汇总的 *.results.json: {result_dir}")
    paths = write_batch_reports(
        records,
        Path(args.output_dir).expanduser().resolve(),
        batch_id=_validate_batch_id(args.batch_id),
        input_csv_sha256=inventory.raw_csv_sha256,
        policy_version=config.ai.policy_version,
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        candidate_threshold=config.quality.candidate_threshold,
        evidence_root=config.workspace.evidence_root,
    )
    print(json.dumps(paths.as_dict(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill-batch-review",
        description="Skill 批量审查：本地计划、逐仓库静态准备、AI 结果导入与候选归档。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="校验 TOML 配置，不访问外部服务")
    validate.add_argument("config", type=Path, help="review.toml 路径")
    validate.add_argument("--json", action="store_true", help="以 JSON 输出校验摘要")
    validate.set_defaults(handler=_cmd_validate_config)

    init = subparsers.add_parser("init-batch", help="从 CSV 生成 plan-only 批次清单")
    init.add_argument("config", type=Path, help="review.toml 路径")
    init.add_argument("--csv", help="覆盖配置中的 batch.inventory_csv")
    init.add_argument("--batch-id", help="批次标识；默认使用 UTC 时间戳")
    init.add_argument(
        "--output",
        default="",
        help="manifest 输出文件；使用 - 只输出到标准输出",
    )
    init.set_defaults(handler=_cmd_init_batch)

    preflight = subparsers.add_parser(
        "preflight", help="检查正式运行所需的本地参数和工具，不访问 Gerrit"
    )
    preflight.add_argument("config", type=Path, help="review.toml 路径")
    preflight.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    preflight.set_defaults(handler=_cmd_preflight)

    plan = subparsers.add_parser(
        "plan-repositories", help="按 included_statuses 生成仓库执行顺序，不联网"
    )
    plan.add_argument("config", type=Path)
    plan.add_argument("--csv", help="覆盖配置中的 batch.inventory_csv")
    plan.add_argument("--output", default="", help="输出 JSON；使用 - 输出到标准输出")
    plan.set_defaults(handler=_cmd_plan_repositories)

    prepare = subparsers.add_parser(
        "prepare-repository",
        help="显式下载一个仓库、冻结快照并运行两套静态检查；不调用模型",
    )
    prepare.add_argument("config", type=Path)
    prepare.add_argument("--batch-id", required=True)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--csv", help="覆盖配置中的 batch.inventory_csv")
    prepare.set_defaults(handler=_cmd_prepare_repository)

    finalize = subparsers.add_parser(
        "finalize-repository", help="导入 Claude Code JSON、判定并导出本地私密候选"
    )
    finalize.add_argument("config", type=Path)
    finalize.add_argument("--batch-id", required=True)
    finalize.add_argument("--repository-index", required=True)
    finalize.add_argument("--ai-results-dir", required=True)
    finalize.set_defaults(handler=_cmd_finalize_repository)

    cleanup = subparsers.add_parser(
        "cleanup-repository", help="全部结果落盘后清理单仓库临时工作区"
    )
    cleanup.add_argument("config", type=Path)
    cleanup.add_argument("--batch-id", required=True)
    cleanup.add_argument("--repository", required=True)
    cleanup.add_argument("--repository-index", required=True)
    cleanup.add_argument("--confirm-cleanup", action="store_true")
    cleanup.set_defaults(handler=_cmd_cleanup_repository)

    report = subparsers.add_parser("report-batch", help="汇总仓库结果索引为批次报告")
    report.add_argument("config", type=Path)
    report.add_argument("--batch-id", required=True)
    report.add_argument("--results-dir", required=True)
    report.add_argument("--output-dir", required=True)
    report.set_defaults(handler=_cmd_report_batch)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ArtifactError,
        ConfigError,
        GitSourceError,
        InventoryError,
        OrchestrationError,
        ScannerError,
        SnapshotError,
        OSError,
        ValueError,
    ) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
