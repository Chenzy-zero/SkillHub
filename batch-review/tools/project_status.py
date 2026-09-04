#!/usr/bin/env python3
"""Read-only project status and next-action advisor for operators and ask-cc."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_REVIEW_DIR = SCRIPT_DIR.parent
REPOSITORY_ROOT = BATCH_REVIEW_DIR.parent
SRC_DIR = BATCH_REVIEW_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skill_batch_review.config import ConfigError, ReviewConfig, load_config  # noqa: E402
from skill_batch_review.inventory import load_inventory_csv  # noqa: E402
from skill_batch_review.preflight import PreflightIssue, review_preflight  # noqa: E402


OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
LAUNCHER_STATE_NAME = "per-skill-launcher-state.json"


@dataclass(frozen=True)
class ProjectStatus:
    state: str
    summary: str
    next_action: str
    next_instruction: str
    config_path: str | None = None
    batch_id: str | None = None
    batch_status: str | None = None
    current_skill: Mapping[str, Any] | None = None
    inventory: Mapping[str, Any] | None = None
    issues: tuple[Mapping[str, Any], ...] = ()
    result_paths: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "summary": self.summary,
            "next_action": self.next_action,
            "next_instruction": self.next_instruction,
            "config_path": self.config_path,
            "batch_id": self.batch_id,
            "batch_status": self.batch_status,
            "current_skill": dict(self.current_skill) if self.current_skill else None,
            "inventory": dict(self.inventory) if self.inventory else None,
            "issues": [dict(issue) for issue in self.issues],
            "result_paths": dict(self.result_paths) if self.result_paths else None,
        }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return value


def _operator_state(path: Path) -> dict[str, Any] | None:
    try:
        return _read_json(path)
    except FileNotFoundError:
        return None


def _blocking_config_issues(issues: Sequence[PreflightIssue]) -> tuple[PreflightIssue, ...]:
    scanner_codes = {"SCANNER_NOT_FOUND"}
    return tuple(issue for issue in issues if issue.code not in scanner_codes)


def _current_item(batch_state: Mapping[str, Any]) -> dict[str, Any] | None:
    task_id = batch_state.get("current_task_id")
    items = batch_state.get("items")
    if not task_id or not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return item
    return None


def _inspect_inventory(config: ReviewConfig) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    try:
        document = load_inventory_csv(
            config.batch.inventory_csv,
            status_mapping=config.status_mapping.aliases,
        )
    except (OSError, ValueError) as exc:
        return {}, ({"code": "INVENTORY_INVALID", "message": str(exc), "blocking": True},)
    included = tuple(row for row in document.rows if row.status in config.batch.included_statuses)
    ids: dict[str, list[int]] = {}
    problems: list[dict[str, Any]] = []
    for row in included:
        identifier = row.trace_values.get("skill_id", "").strip()
        if not identifier:
            problems.append(
                {
                    "code": "SKILL_ID_MISSING",
                    "message": f"CSV 第 {row.row_number} 行缺少 skill_id",
                    "blocking": True,
                }
            )
            continue
        ids.setdefault(identifier, []).append(row.row_number)
    for identifier, line_numbers in ids.items():
        if len(line_numbers) > 1:
            problems.append(
                {
                    "code": "SKILL_ID_DUPLICATE",
                    "message": f"skill_id {identifier} 在 CSV 行 {line_numbers} 重复",
                    "blocking": True,
                }
            )
    summary = {
        "csv_path": str(config.batch.inventory_csv),
        "csv_sha256": document.raw_csv_sha256,
        "total_rows": len(document.rows),
        "included_skills": len(included),
        "excluded_rows": len(document.rows) - len(included),
        "repositories": len({row.repo_name for row in included}),
    }
    return summary, tuple(problems)


def inspect_project(*, operator_state_path: Path = OPERATOR_STATE) -> ProjectStatus:
    operator = _operator_state(operator_state_path)
    if operator is None:
        return ProjectStatus(
            state="NOT_INITIALIZED",
            summary="项目尚未执行首次初始化。",
            next_action="INITIALIZE",
            next_instruction="双击 batch-review/init.cmd，Linux/CentOS 执行 batch-review/init.sh。",
        )

    raw_config = operator.get("config_path")
    config_path = Path(str(raw_config)).expanduser() if raw_config else Path()
    if not raw_config or not config_path.is_file():
        return ProjectStatus(
            state="CONFIG_MISSING",
            summary="初始化记录存在，但实际配置文件不存在。",
            next_action="INITIALIZE",
            next_instruction="重新运行初始化入口，生成本机配置文件。",
            config_path=str(config_path) if raw_config else None,
        )

    try:
        config = load_config(config_path)
    except (ConfigError, OSError, ValueError) as exc:
        return ProjectStatus(
            state="CONFIG_INVALID",
            summary="本机配置文件无法通过格式校验。",
            next_action="EDIT_CONFIG",
            next_instruction=f"打开并修正配置文件：{config_path}",
            config_path=str(config_path),
            issues=({"code": "CONFIG_INVALID", "message": str(exc), "blocking": True},),
        )

    issues = review_preflight(config)
    blocking = _blocking_config_issues(issues)
    if blocking:
        return ProjectStatus(
            state="CONFIGURATION_REQUIRED",
            summary="项目已初始化，但 Git、CSV、AI 规则或基础配置仍需补充。",
            next_action="EDIT_CONFIG",
            next_instruction=f"打开 {config_path}，按问题列表填写后再次双击 review.cmd。",
            config_path=str(config_path),
            issues=tuple(issue.to_dict() for issue in issues),
        )

    inventory, inventory_issues = _inspect_inventory(config)
    if inventory_issues:
        return ProjectStatus(
            state="INVENTORY_INVALID",
            summary="本机配置已读取，但 Skill 清单不满足逐项执行要求。",
            next_action="EDIT_CONFIG",
            next_instruction="修正状态中显示的 CSV；保存后再次双击 review.cmd。",
            config_path=str(config_path),
            inventory=inventory,
            issues=inventory_issues,
        )

    missing_scanners = tuple(issue for issue in issues if issue.code == "SCANNER_NOT_FOUND")
    if missing_scanners:
        return ProjectStatus(
            state="SCANNERS_REQUIRED",
            summary="基础配置已就绪，但静态扫描器尚未安装到约定位置。",
            next_action="INSTALL_SCANNERS",
            next_instruction="双击 review.cmd，并确认执行扫描器安装。",
            config_path=str(config_path),
            inventory=inventory,
            issues=tuple(issue.to_dict() for issue in missing_scanners),
        )

    batch_id = str(operator.get("batch_id") or "").strip() or None
    if not batch_id:
        return ProjectStatus(
            state="READY_TO_PLAN",
            summary="配置、CSV 和扫描器均已就绪，尚未创建审查批次。",
            next_action="PLAN",
            next_instruction="双击 review.cmd，确认后自动生成批次计划。",
            config_path=str(config_path),
            inventory=inventory,
        )

    state_path = config.workspace.manifest_root / batch_id / LAUNCHER_STATE_NAME
    try:
        batch_state = _read_json(state_path)
    except FileNotFoundError:
        return ProjectStatus(
            state="BATCH_STATE_MISSING",
            summary="初始化记录中的批次不存在或已被移动。",
            next_action="PLAN",
            next_instruction="双击 review.cmd，确认后创建一个新批次。",
            config_path=str(config_path),
            batch_id=batch_id,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ProjectStatus(
            state="BATCH_STATE_INVALID",
            summary="批次状态文件无法读取，不能安全推进。",
            next_action="MANUAL_CHECK",
            next_instruction=f"检查批次状态文件：{state_path}",
            config_path=str(config_path),
            batch_id=batch_id,
            issues=({"code": "BATCH_STATE_INVALID", "message": str(exc), "blocking": True},),
        )

    batch_status = str(batch_state.get("status") or "UNKNOWN")
    current = _current_item(batch_state)
    common = {
        "config_path": str(config_path),
        "batch_id": batch_id,
        "batch_status": batch_status,
        "current_skill": current,
        "inventory": inventory,
    }
    if batch_status == "COMPLETE":
        return ProjectStatus(
            state="COMPLETE",
            summary="当前批次已完成，结果表已经生成。",
            next_action="VIEW_RESULTS",
            next_instruction="查看批次 CSV/JSON；需要新一轮审查时重新运行 init.cmd 选择新配置或创建新批次。",
            result_paths={
                "csv": str(batch_state.get("result_csv") or ""),
                "json": str(batch_state.get("result_json") or ""),
            },
            **common,
        )
    if batch_status == "WAITING_FOR_AI":
        ai_path = Path(str(current.get("ai_result_path") or "")) if current else Path()
        if current and ai_path.is_file():
            return ProjectStatus(
                state="AI_RESULT_READY",
                summary="当前 Skill 的 AI 结果已保存，可以合并结果并进入下一个 Skill。",
                next_action="ADVANCE",
                next_instruction="运行 review.cmd；程序会校验并保存结果，然后自动继续。",
                **common,
            )
        return ProjectStatus(
            state="WAITING_FOR_AI",
            summary="当前 Skill 的静态扫描已完成，正在等待 AI 安全与质量审查。",
            next_action="AI_REVIEW",
            next_instruction="在 Claude Code 输入 /auto-skill-review；它会完成当前及后续 AI 审查并自动推进批次。",
            **common,
        )
    if batch_status == "READY_TO_ADVANCE":
        return ProjectStatus(
        state="READY_TO_ADVANCE",
        summary="当前 Skill 已复用合格结果，无需重复 AI 审查。",
        next_action="ADVANCE",
        next_instruction="运行 review.cmd；程序会写入复用记录并自动进入下一个 Skill。",
            **common,
        )
    if batch_status == "READY":
        return ProjectStatus(
        state="READY_TO_START",
        summary="批次计划已建立，尚未按仓库下载和扫描。",
        next_action="START",
        next_instruction="双击 review.cmd，一次确认后按仓库自动下载并逐一扫描。",
            **common,
        )
    return ProjectStatus(
        state="MANUAL_CHECK_REQUIRED",
        summary=f"遇到未识别的批次状态：{batch_status}",
        next_action="MANUAL_CHECK",
        next_instruction=f"检查状态文件：{state_path}",
        **common,
    )


def _human(status: ProjectStatus) -> str:
    lines = [
        f"当前状态：{status.summary}",
        f"下一步：{status.next_instruction}",
    ]
    if status.config_path:
        lines.append(f"配置文件：{status.config_path}")
    if status.batch_id:
        lines.append(f"批次号：{status.batch_id}")
    if status.current_skill:
        lines.append(
            "当前 Skill："
            f"{status.current_skill.get('skill_id', '-')} / {status.current_skill.get('skill_name', '-')}"
        )
    if status.inventory:
        lines.append(
            "清单："
            f"{status.inventory.get('included_skills', 0)} 个 Skill / "
            f"{status.inventory.get('repositories', 0)} 个仓库"
        )
    if status.issues:
        lines.append("需要处理的问题：")
        lines.extend(f"- [{issue.get('code')}] {issue.get('message')}" for issue in status.issues)
    if status.result_paths:
        lines.append(f"结果 CSV：{status.result_paths.get('csv')}")
        lines.append(f"结果 JSON：{status.result_paths.get('json')}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读分析 Skill 审查项目状态并给出下一步。")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--operator-state", type=Path, default=OPERATOR_STATE, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        status = inspect_project(operator_state_path=args.operator_state.expanduser().resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"项目状态检查失败：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_human(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
