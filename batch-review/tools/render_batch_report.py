#!/usr/bin/env python3
"""Render the current audit snapshot without advancing the batch."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SRC_DIR = PROJECT_DIR / "src"
OPERATOR_STATE = PROJECT_DIR / ".batch-review" / "operator-state.json"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skill_batch_review.config import load_config  # noqa: E402
from skill_batch_review.inventory import load_inventory_csv  # noqa: E402
from skill_batch_review.live_report import write_live_batch_report  # noqa: E402


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成当前批次的 INTERIM/FINAL 审计报告，不推进扫描或 AI 状态。"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--batch-id")
    return parser


def _operator_defaults(config_path: Path | None, batch_id: str | None) -> tuple[Path, str]:
    if config_path is not None and batch_id:
        return config_path, str(batch_id)
    if not OPERATOR_STATE.is_file() or OPERATOR_STATE.is_symlink():
        raise ValueError("缺少当前操作状态；请提供 --config 和 --batch-id，或先运行 init/review 入口")
    value = json.loads(OPERATOR_STATE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("operator-state.json 格式无效")
    resolved_config = config_path or Path(str(value.get("config_path") or ""))
    resolved_batch = str(batch_id or value.get("batch_id") or "").strip()
    if not str(resolved_config):
        raise ValueError("当前操作状态没有 config_path")
    if not resolved_batch:
        raise ValueError("当前操作状态还没有 batch_id；请先创建/启动批次")
    return resolved_config, resolved_batch


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config_path, batch_id = _operator_defaults(args.config, args.batch_id)
        batch_id = batch_id.strip()
        if not _BATCH_ID_RE.fullmatch(batch_id):
            raise ValueError("batch-id 格式无效")
        config = load_config(config_path)
        inventory = load_inventory_csv(
            config.batch.inventory_csv,
            status_mapping=config.status_mapping.aliases,
        )
        result = write_live_batch_report(config, inventory, batch_id=batch_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    print(f"报告状态: {result.report_status}")
    print(
        "进度: "
        f"Static {result.progress['static_completed'] + result.progress['static_incomplete']}/{result.progress['selected']}, "
        f"AI {result.progress['ai_completed'] + result.progress['ai_not_required']}/{result.progress['selected']}, "
        f"Final {result.progress['final_completed'] + result.progress['final_incomplete']}/{result.progress['selected']}"
    )
    print(f"HTML: {result.paths.html}")
    print(f"当前结果 CSV: {result.current_csv}")
    print(f"当前结果 JSON: {result.current_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
