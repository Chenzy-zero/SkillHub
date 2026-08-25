#!/usr/bin/env python3
"""Batch clone/fetch Gerrit repositories and scan SKILL.md assets.

Python 3.8+; standard library only. This is a baseline POC runner, not the
Gerrit real-time hook path.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from skill_scan import GitError, scan_revision


LOGGER = logging.getLogger("skillhub-poc")


def load_config(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        config = json.load(fh)
    if not isinstance(config, dict):
        raise ValueError("config root must be a JSON object")
    repos = config.get("repositories")
    if not isinstance(repos, list):
        raise ValueError("config.repositories must be a JSON array")
    return config


def safe_url(url):
    """Avoid printing an HTTP password/token if a URL contains one."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme not in ("http", "https") or "@" not in parts.netloc:
        return url
    userinfo, host = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    netloc = "{}:***@{}".format(username, host)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def derive_repo_name(url):
    value = url.rstrip("/\\")
    if ":" in value and "/" not in value.split(":", 1)[0]:
        # SCP-like form: user@host:path/to/repo.git
        value = value.split(":", 1)[1]
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    if value.endswith(".git"):
        value = value[:-4]
    return value or "repository"


def safe_dir_name(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value or "repository"


def run_console_command(cmd, cwd=None):
    LOGGER.debug("执行命令: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=cwd)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到命令 '{}': {}".format(cmd[0], exc))
    if proc.returncode != 0:
        raise RuntimeError("命令执行失败 (exit={}): {}".format(proc.returncode, " ".join(cmd)))


def git_capture(repo, *args):
    cmd = ["git", "-C", str(repo)] + list(args)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "git command failed ({}): {}\n{}".format(
                proc.returncode,
                " ".join(cmd),
                proc.stderr.decode("utf-8", "replace"),
            )
        )
    return proc.stdout.decode("utf-8", "replace").strip()


def ensure_repository(url, local_path, refresh_existing=True):
    if (local_path / ".git").exists():
        LOGGER.info("本地仓库已存在: %s", local_path)
        if refresh_existing:
            LOGGER.info("拉取远端最新引用: %s", safe_url(url))
            run_console_command(["git", "-C", str(local_path), "fetch", "--all", "--prune"])
            # Keep origin/HEAD reasonably fresh when the remote exposes a default branch.
            subprocess.run(
                ["git", "-C", str(local_path), "remote", "set-head", "origin", "-a"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return

    if local_path.exists() and any(local_path.iterdir()):
        raise RuntimeError("目标目录已存在但不是 Git 仓库: {}".format(local_path))

    local_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("开始 clone: %s", safe_url(url))
    LOGGER.info("本地目录: %s", local_path)
    run_console_command(["git", "clone", url, str(local_path)])


def revision_exists(repo, revision):
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "{}^{{commit}}".format(revision)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def resolve_revision(repo, configured_revision):
    revision = (configured_revision or "HEAD").strip()

    if revision == "HEAD":
        # For a refreshed clone, origin/HEAD is a better baseline than a stale local branch.
        if revision_exists(repo, "origin/HEAD"):
            return "origin/HEAD"
        return "HEAD"

    if revision_exists(repo, revision):
        return revision

    remote_revision = "origin/{}".format(revision)
    if revision_exists(repo, remote_revision):
        return remote_revision

    raise RuntimeError("无法解析 revision: {} (也未找到 {})".format(revision, remote_revision))


def setup_logging(output_dir, verbose=False):
    output_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    LOGGER.setLevel(level)
    LOGGER.handlers[:] = []

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)

    file_handler = logging.FileHandler(str(output_dir / "batch_scan.log"), encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)


def write_outputs(output_dir, results, include_manifest):
    generated_at = datetime.now().astimezone().isoformat()
    all_skills = []
    for result in results:
        for skill in result.get("skills", []):
            item = dict(skill)
            if not include_manifest:
                item.pop("manifest", None)
            item["configured_url"] = result.get("url")
            item["configured_revision"] = result.get("configured_revision")
            item["resolved_revision"] = result.get("resolved_revision")
            item["local_path"] = result.get("local_path")
            all_skills.append(item)

    report = {
        "generated_at": generated_at,
        "repository_count": len(results),
        "success_count": sum(1 for item in results if item.get("status") == "success"),
        "failed_count": sum(1 for item in results if item.get("status") == "failed"),
        "skill_count": len(all_skills),
        "repositories": results,
        "skills": all_skills,
    }

    json_path = output_dir / "skill_inventory.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)

    jsonl_path = output_dir / "skill_inventory.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for item in all_skills:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    return json_path, jsonl_path


def main():
    parser = argparse.ArgumentParser(description="Batch clone/fetch repositories and scan SKILL.md assets")
    parser.add_argument("--config", default="scan_config.json", help="Path to JSON config")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(
            "配置文件不存在: {}\n请复制 scan_config.example.json 为 scan_config.json 后填写仓库 SSH 地址。".format(
                config_path
            ),
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(str(config_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("读取配置失败: {}".format(exc), file=sys.stderr)
        return 2

    base_dir = config_path.parent
    workspace = Path(config.get("workspace", "./workspace"))
    output_dir = Path(config.get("output_dir", "./output"))
    if not workspace.is_absolute():
        workspace = (base_dir / workspace).resolve()
    if not output_dir.is_absolute():
        output_dir = (base_dir / output_dir).resolve()

    include_manifest = bool(config.get("include_manifest", False))
    refresh_existing = bool(config.get("refresh_existing", True))
    continue_on_error = bool(config.get("continue_on_error", True))

    setup_logging(output_dir, args.verbose)

    enabled_repos = [item for item in config.get("repositories", []) if item.get("enabled", True)]
    LOGGER.info("================ SkillHub Gerrit Baseline POC ================")
    LOGGER.info("配置文件: %s", config_path)
    LOGGER.info("工作目录: %s", workspace)
    LOGGER.info("输出目录: %s", output_dir)
    LOGGER.info("待处理仓库: %d", len(enabled_repos))

    results = []
    total = len(enabled_repos)

    for index, item in enumerate(enabled_repos, 1):
        url = (item.get("url") or "").strip()
        name = (item.get("name") or derive_repo_name(url)).strip()
        configured_revision = (item.get("revision") or "HEAD").strip()
        local_dir = (item.get("local_dir") or safe_dir_name(name)).strip()
        local_path = workspace / local_dir

        LOGGER.info("------------------------------------------------------------")
        LOGGER.info("[%d/%d] 仓库: %s", index, total, name)
        LOGGER.info("远端: %s", safe_url(url) if url else "<未配置>")
        LOGGER.info("目标 Revision: %s", configured_revision)

        result = {
            "name": name,
            "url": safe_url(url),
            "configured_revision": configured_revision,
            "resolved_revision": None,
            "local_path": str(local_path),
            "status": "failed",
            "error": None,
            "skill_count": 0,
            "skills": [],
        }

        try:
            if not url:
                raise ValueError("repository.url 不能为空")

            ensure_repository(url, local_path, refresh_existing=refresh_existing)
            revision = resolve_revision(local_path, configured_revision)
            resolved_sha = git_capture(local_path, "rev-parse", revision)
            result["resolved_revision"] = resolved_sha

            LOGGER.info("开始扫描: %s @ %s", name, resolved_sha[:12])
            records = scan_revision(str(local_path), revision, name)
            result["skills"] = [asdict(record) for record in records]
            result["skill_count"] = len(records)
            result["status"] = "success"

            if not records:
                LOGGER.info("扫描完成: 未发现 SKILL.md")
            else:
                LOGGER.info("扫描完成: 共发现 %d 个 Skill", len(records))
                for record in records:
                    LOGGER.info(
                        "  Skill: %s | path=%s | digest=%s | files=%d",
                        record.skill_name,
                        record.skill_path or ".",
                        record.skill_digest[:16],
                        record.file_count,
                    )
                    for warning in record.warnings:
                        LOGGER.warning("    %s", warning)

        except (ValueError, RuntimeError, GitError, OSError) as exc:
            result["error"] = str(exc)
            LOGGER.error("处理失败: %s", exc)
            if not continue_on_error:
                results.append(result)
                break

        results.append(result)

    json_path, jsonl_path = write_outputs(output_dir, results, include_manifest)
    success_count = sum(1 for item in results if item.get("status") == "success")
    failed_count = sum(1 for item in results if item.get("status") == "failed")
    skill_count = sum(item.get("skill_count", 0) for item in results)

    LOGGER.info("============================================================")
    LOGGER.info("批量扫描结束")
    LOGGER.info("成功仓库: %d", success_count)
    LOGGER.info("失败仓库: %d", failed_count)
    LOGGER.info("发现 Skill: %d", skill_count)
    LOGGER.info("JSON 报告: %s", json_path)
    LOGGER.info("JSONL 清单: %s", jsonl_path)
    LOGGER.info("日志文件: %s", output_dir / "batch_scan.log")

    return 1 if failed_count else 0


if __name__ == "__main__":
    sys.exit(main())
