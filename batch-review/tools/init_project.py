#!/usr/bin/env python3
"""Initialize a local operator configuration without overwriting existing values."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_REVIEW_DIR = SCRIPT_DIR.parent
REPOSITORY_ROOT = BATCH_REVIEW_DIR.parent
CONFIG_DIR = BATCH_REVIEW_DIR / "config"
DEFAULT_CONFIG = CONFIG_DIR / "review.local.toml"
OPERATOR_STATE = BATCH_REVIEW_DIR / ".batch-review" / "operator-state.json"
SUPPORTED_PYTHON = {(3, 11), (3, 12), (3, 13), (3, 14)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _select_profile() -> str:
    print("请选择初始化环境：")
    print("1. 公司 Gerrit 正式/联调环境（推荐）")
    print("2. GitHub 示例验证环境")
    answer = input("请输入 1 或 2 [1]：").strip() or "1"
    if answer not in {"1", "2"}:
        raise ValueError("只能选择 1 或 2")
    return "company" if answer == "1" else "github"


def _scanner_executable(name: str, executable: str) -> str:
    if os.name == "nt":
        return str((BATCH_REVIEW_DIR / ".scanner-tools" / name / "Scripts" / f"{executable}.exe").resolve())
    return str((BATCH_REVIEW_DIR / ".scanner-tools" / name / "bin" / executable).resolve())


def _find_ssh_key() -> Path | None:
    candidates = (
        Path.home() / ".ssh" / "id_ed25519_github_chenzy_zero",
        Path.home() / ".ssh" / "id_ed25519",
        Path.home() / ".ssh" / "id_rsa",
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _localize_template(text: str, *, profile: str) -> str:
    base = (BATCH_REVIEW_DIR / ".batch-review" / profile).resolve()
    replacements = {
        "../../test/github_skill_summary.csv": str(REPOSITORY_ROOT / "test" / "github_skill_summary.csv"),
        "../../test/skill_summary.csv": str(REPOSITORY_ROOT / "test" / "skill_summary.csv"),
        "../skills/skill-security-review/references/review-result.schema.json": str(
            BATCH_REVIEW_DIR
            / "skills"
            / "skill-security-review"
            / "references"
            / "review-result.schema.json"
        ),
        "../skills/skill-security-review": str(
            BATCH_REVIEW_DIR / "skills" / "skill-security-review"
        ),
        "../.batch-review/github-validation/work": str(base / "work"),
        "../.batch-review/github-validation/restricted-evidence": str(base / "restricted-evidence"),
        "../.batch-review/github-validation/private-candidates": str(base / "private-candidates"),
        "../.batch-review/github-validation/manifests": str(base / "manifests"),
        "../.batch-review/github-validation/git_download": str(base / "git_download"),
        "../.batch-review/github-validation/skills": str(base / "skills"),
        "../.batch-review/github-validation/results": str(base / "results"),
        "/data/skill-review/work": str(base / "work"),
        "/data/skill-review/restricted-evidence": str(base / "restricted-evidence"),
        "/data/skill-review/private-candidates": str(base / "private-candidates"),
        "/data/skill-review/manifests": str(base / "manifests"),
        "/data/skill-review/git_download": str(base / "git_download"),
        "/data/skill-review/skills": str(base / "skills"),
        "/data/skill-review/results": str(base / "results"),
        "/opt/skill-review/scanners/cisco/bin/skill-scanner": _scanner_executable(
            "cisco", "skill-scanner"
        ),
        "/opt/skill-review/scanners/skillspector/bin/skillspector": _scanner_executable(
            "skillspector", "skillspector"
        ),
        "FILL_CISCO_EXECUTABLE": _scanner_executable("cisco", "skill-scanner"),
        "FILL_SKILLSPECTOR_EXECUTABLE": _scanner_executable("skillspector", "skillspector"),
    }
    if profile == "github":
        key = _find_ssh_key()
        if key:
            replacements["FILL_GITHUB_READONLY_SSH_KEY_PATH"] = str(key)
    for old, new in replacements.items():
        text = text.replace(old, new.replace("\\", "/"))
    return text


def initialize(
    *,
    profile: str,
    config_path: Path,
    operator_state_path: Path = OPERATOR_STATE,
    force: bool = False,
) -> tuple[Path, bool]:
    template_name = "review.company.example.toml" if profile == "company" else "review.github.example.toml"
    template = CONFIG_DIR / template_name
    config_path = config_path.expanduser().resolve()
    created = False
    if config_path.exists() and not force:
        print(f"保留已有配置，不覆盖：{config_path}")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = _localize_template(template.read_text(encoding="utf-8"), profile=profile)
        config_path.write_text(content, encoding="utf-8", newline="\n")
        created = True
        print(f"已生成本机配置：{config_path}")

    previous: dict[str, Any] = {}
    if operator_state_path.is_file():
        try:
            value = json.loads(operator_state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                previous = value
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
    previous.update(
        {
            "schema_version": "1.0",
            "initialized_at": _utc_now(),
            "profile": profile,
            "config_path": str(config_path),
            "batch_id": None if created or force else previous.get("batch_id"),
        }
    )
    _atomic_json(operator_state_path, previous)
    return config_path, created


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化 Skill 批量审查本机环境。")
    parser.add_argument("--profile", choices=("company", "github"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--operator-state", type=Path, default=OPERATOR_STATE, help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true", help="覆盖本机配置；默认绝不覆盖")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    version = sys.version_info[:2]
    if version not in SUPPORTED_PYTHON:
        print("初始化需要 Python 3.11～3.14。", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        profile = args.profile or _select_profile()
        config_path, created = initialize(
            profile=profile,
            config_path=args.config,
            operator_state_path=args.operator_state.expanduser().resolve(),
            force=args.force,
        )
    except (OSError, ValueError) as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2

    print("\n初始化完成。")
    if created:
        print(f"下一步先打开并检查配置文件：{config_path}")
        if profile == "company":
            print("填写 Gerrit 只读地址/账号、CSV 路径和仓库白名单。")
        else:
            print("检查 GitHub SSH 私钥路径；其余验证参数已经按本机目录生成。")
    print("完成配置后，可双击 batch-review/review.cmd；Linux/CentOS 执行 batch-review/review.sh。")
    print("也可直接启动 AI 自动入口：Codex CLI 输入 $auto-skill-review；Claude Code 输入 /auto-skill-review。")
    print("只查看状态：Codex CLI 输入 $ask-cc；Claude Code 输入 /ask-cc。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
