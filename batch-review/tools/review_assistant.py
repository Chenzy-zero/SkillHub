#!/usr/bin/env python3
"""State-aware no-argument operator entry point for the per-Skill workflow."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_REVIEW_DIR = SCRIPT_DIR.parent
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
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def main() -> int:
    try:
        status = _status()
        _print_status(status)
        action = status.get("next_action")
        if action == "INITIALIZE":
            if _confirm("是否现在初始化项目"):
                return _run((sys.executable, str(INIT_SCRIPT)))
            return 0
        if action == "EDIT_CONFIG":
            print("请只修改上面显示的本机配置文件；保存后再次运行本入口。")
            return 0
        if action == "INSTALL_SCANNERS":
            if not _confirm("是否现在从已配置的公司 pip 源安装两套扫描器"):
                return 0
            code = _run((sys.executable, str(INSTALLER), "--root", str(BATCH_REVIEW_DIR / ".scanner-tools")))
            if code == 0:
                print("扫描器已安装。再次双击 review.cmd，程序会继续判断下一步。")
            return code
        if action == "PLAN":
            if not _confirm("是否现在生成新的逐 Skill 审查计划（不会联网和扫描）"):
                return 0
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
            if code == 0:
                operator["batch_id"] = batch_id
                _save_operator(operator)
                print("计划已生成。再次双击 review.cmd 即可开始第一个 Skill。")
            return code
        if action == "START":
            if not _confirm("是否现在连接 Git 并下载、扫描第一个 Skill"):
                return 0
            operator = _operator()
            return _run(
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
        if action == "AI_REVIEW":
            print("请在当前仓库的 Claude Code 会话输入：/ask-cc")
            print("ask-cc 会读取当前交接文件并明确告诉你下一步，不需要记路径或参数。")
            return 0
        if action == "ADVANCE":
            if not _confirm("是否保存当前结果、清理本次临时下载并进入下一个 Skill"):
                return 0
            operator = _operator()
            return _run(
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
        if action == "VIEW_RESULTS":
            results = status.get("result_paths") or {}
            print(f"结果 CSV：{results.get('csv', '')}")
            print(f"结果 JSON：{results.get('json', '')}")
            return 0
        print("当前状态需要人工检查，程序没有执行修改操作。")
        return 2
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"操作入口失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
