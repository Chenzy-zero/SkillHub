#!/usr/bin/env python3
"""Prepare/import incremental zh-CN report localization jobs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_REVIEW_DIR = SCRIPT_DIR.parent
SRC_DIR = BATCH_REVIEW_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skill_batch_review import batch_launcher  # noqa: E402
from skill_batch_review.config import load_config  # noqa: E402
from skill_batch_review.live_report import write_live_batch_report  # noqa: E402
from skill_batch_review.localization_job import (  # noqa: E402
    DEFAULT_MAX_UNITS,
    LocalizationJobError,
    import_localization_result,
    prepare_localization_job,
)


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
RESULT_SCHEMA = (
    BATCH_REVIEW_DIR
    / ".agents"
    / "skills"
    / "report-zh-localizer"
    / "references"
    / "localization-result.schema.json"
)


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--batch-id")
    parser.add_argument(
        "--current",
        action="store_true",
        help="从 .batch-review/operator-state.json 读取当前 config/batch",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="增量中文化 batch-review 审计报告。")
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare", help="冻结当前 pending translation units 为一个可信 job")
    _add_context_arguments(prepare)
    prepare.add_argument("--max-units", type=int, default=DEFAULT_MAX_UNITS)

    imported = sub.add_parser("import", help="校验 localizer result、合并 Translation Memory 并刷新报告")
    _add_context_arguments(imported)
    imported.add_argument("--job-id", required=True)
    imported.add_argument("--max-units", type=int, default=DEFAULT_MAX_UNITS)
    return parser


def _batch_id(value: str) -> str:
    text = str(value or "").strip()
    if not _BATCH_ID_RE.fullmatch(text):
        raise LocalizationJobError("batch-id must be 1-64 safe filename characters")
    return text


def _current_context() -> tuple[Path, str]:
    if not OPERATOR_STATE.is_file() or OPERATOR_STATE.is_symlink():
        raise LocalizationJobError("current operator state is unavailable")
    value = json.loads(OPERATOR_STATE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LocalizationJobError("current operator state is invalid")
    config_path = Path(str(value.get("config_path") or "")).expanduser()
    batch_id = _batch_id(str(value.get("batch_id") or ""))
    if not config_path.is_file() or config_path.is_symlink():
        raise LocalizationJobError("current config path is unavailable")
    return config_path, batch_id


def _context(args: argparse.Namespace) -> tuple[Path, str]:
    if args.current:
        if args.config is not None or args.batch_id is not None:
            raise LocalizationJobError("--current cannot be combined with --config/--batch-id")
        return _current_context()
    if args.config is None or args.batch_id is None:
        raise LocalizationJobError("provide --current or both --config and --batch-id")
    return args.config, _batch_id(args.batch_id)


def _prepare(config_path: Path, batch_id: str, max_units: int) -> dict:
    config = load_config(config_path)
    job = prepare_localization_job(
        config.workspace.results_root,
        batch_id,
        result_schema_path=RESULT_SCHEMA,
        max_units=max_units,
    )
    if job is None:
        return {
            "status": "COMPLETE",
            "batch_id": batch_id,
            "pending_count": 0,
            "localization_dispatch": None,
        }
    return {
        "status": "READY",
        "batch_id": batch_id,
        "pending_count": job.unit_count + job.remaining_count,
        "localization_dispatch": job.to_dict(),
    }


def _import(config_path: Path, batch_id: str, job_id: str, max_units: int) -> dict:
    config = load_config(config_path)
    imported = import_localization_result(
        config.workspace.results_root,
        batch_id,
        job_id=job_id,
        result_schema_path=RESULT_SCHEMA,
    )
    inventory = batch_launcher._inventory(config)
    live = write_live_batch_report(config, inventory, batch_id=batch_id)
    next_job = prepare_localization_job(
        config.workspace.results_root,
        batch_id,
        result_schema_path=RESULT_SCHEMA,
        max_units=max_units,
    )
    return {
        "status": "COMPLETE" if next_job is None else "READY",
        "batch_id": batch_id,
        "import": imported.to_dict(),
        "report_html": str(live.paths.html),
        "report_status": live.report_status,
        "localization_dispatch": next_job.to_dict() if next_job is not None else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.max_units < 1:
            raise LocalizationJobError("max-units must be >= 1")
        config_path, batch_id = _context(args)
        if args.action == "prepare":
            response = _prepare(config_path, batch_id, args.max_units)
        else:
            response = _import(config_path, batch_id, args.job_id, args.max_units)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, LocalizationJobError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
