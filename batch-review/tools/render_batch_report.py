#!/usr/bin/env python3
"""Render the current audit snapshot without advancing the batch."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SRC_DIR = PROJECT_DIR / "src"
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
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--batch-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    batch_id = str(args.batch_id).strip()
    if not _BATCH_ID_RE.fullmatch(batch_id):
        print("错误: batch-id 格式无效", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        inventory = load_inventory_csv(
            config.batch.inventory_csv,
            status_mapping=config.status_mapping.aliases,
        )
        result = write_live_batch_report(config, inventory, batch_id=batch_id)
    except (OSError, ValueError) as exc:
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
