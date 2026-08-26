#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sys
from pathlib import Path

from change_analyzer import analyze_change
from database import Database
from gerrit_client import GerritClient
from inventory import Inventory
from report_generator import generate_report
from skill_digest import GitError, calculate_skill_digest, clone_or_fetch_project


def load_config(path):
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8-sig") as fh:
        config = json.load(fh)
    base = config_path.parent
    for key in ("workspace", "output_dir", "inventory_file", "database_path", "report_dir"):
        value = config.get(key)
        if value and not Path(value).is_absolute():
            config[key] = str((base / value).resolve())
    if not config.get("database_path"):
        config["database_path"] = str((base / "data" / "skillhub-poc.db").resolve())
    if not config.get("report_dir"):
        config["report_dir"] = str((base / "output" / "dashboard").resolve())
    return config


def setup_logger(output_dir, verbose=False):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gerrit-change-discovery")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers[:] = []
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)
    file_handler = logging.FileHandler(output / "gerrit-change-discovery.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def main():
    parser = argparse.ArgumentParser(description="Analyze one Gerrit Code Review patchset for affected Skills")
    parser.add_argument("--config", default="config.json", help="Config JSON path")
    parser.add_argument("--change", required=True, help="Gerrit change number or change-id")
    parser.add_argument("--expected-revision", help="Optional exact revision SHA expected by caller/hook")
    parser.add_argument("--expected-patchset", type=int, help="Optional exact patchset number expected by caller/hook")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-digest", action="store_true", help="Only analyze changed files; do not clone/fetch Git")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as exc:
        print("配置读取失败: {}".format(exc), file=sys.stderr)
        return 2

    logger = setup_logger(config.get("output_dir", "./output"), args.verbose)
    logger.info("================ Gerrit Change Skill Discovery POC ================")
    logger.info("Change: %s", args.change)

    g = config.get("gerrit", {})
    env_name = g.get("http_password_env", "GERRIT_HTTP_PASSWORD")
    has_direct_password = bool(g.get("http_password"))
    has_env_password = bool(os.environ.get(env_name))
    if g.get("username") and not (has_direct_password or has_env_password):
        logger.warning("未配置 Gerrit HTTP Password；REST 请求将按匿名方式尝试")
    elif has_direct_password:
        logger.info("REST 认证: 使用 config.json 中配置的 Gerrit HTTP Password")
    else:
        logger.info("REST 认证: 使用环境变量 %s", env_name)

    try:
        database = Database.from_config(config, logger=logger)
        logger.info("数据库: %s", database.description())
        database.init_schema()

        client = GerritClient.from_config(config, logger)
        logger.info("[1/8] 获取 Gerrit Change 信息...")
        detail = client.get_change_detail(args.change)
        revision_sha, revision_info = client.current_revision_info(detail)
        revision_ref = revision_info.get("ref")
        patchset = revision_info.get("_number")
        project = detail.get("project")
        branch = detail.get("branch")
        logger.info("项目: %s", project)
        logger.info("Branch: %s", branch)
        logger.info("Patchset: %s", patchset)
        logger.info("Revision: %s", revision_sha)
        logger.info("Revision Ref: %s", revision_ref)

        if args.expected_revision and revision_sha != args.expected_revision:
            raise ValueError(
                "当前 Gerrit revision 与 Submit Hook 传入 commit 不一致: current={} expected={}".format(
                    revision_sha, args.expected_revision
                )
            )
        if args.expected_patchset is not None and int(patchset) != int(args.expected_patchset):
            raise ValueError(
                "当前 Gerrit patchset 与 Submit Hook 传入 patchset 不一致: current={} expected={}".format(
                    patchset, args.expected_patchset
                )
            )

        logger.info("[2/8] 获取本 Patchset 文件清单...")
        files = client.get_revision_files(args.change, revision_sha)
        logger.info("Changed Files: %s", len(files))
        for path, info in files.items():
            logger.debug("  %s %s old=%s", info.get("status") or "M", path, info.get("old_path"))

        logger.info("[3/8] 加载 Baseline + Database Skill Inventory...")
        baseline_inventory = Inventory.load(config.get("inventory_file"), logger)
        db_inventory = Inventory.from_rows(database.inventory_rows(), logger, "Database Inventory")
        inventory = baseline_inventory.merge(db_inventory, logger)

        logger.info("[4/8] 基于文件清单识别受影响 Skill...")
        affected = analyze_change(client, inventory, args.change, detail, files, logger)
        logger.info("受影响 Skill 记录: %s", len(affected))

        digest_enabled = bool(config.get("calculate_digest", True)) and not args.no_digest
        if digest_enabled and affected:
            logger.info("[5/8] 仅获取当前 Patchset，并计算受影响 Skill Root Digest...")
            try:
                repo_dir, resolved = clone_or_fetch_project(config, project, revision_sha, revision_ref, logger)
                digest_cache = {}
                for item in affected:
                    if item.get("action") == "DELETED_SKILL":
                        item["digest_status"] = "NOT_APPLICABLE"
                        continue
                    root = item.get("skill_path")
                    if root not in digest_cache:
                        logger.info("计算 Skill Root Digest: %s", root or "<repository-root>")
                        digest_cache[root] = calculate_skill_digest(repo_dir, resolved, root)
                    digest_info = digest_cache[root]
                    item.update(digest_info)
                    item["digest_status"] = "SUCCESS"
                    logger.info("Digest: %s -> %s", root or "<root>", digest_info["skill_digest"][:16] + "...")
            except GitError as exc:
                logger.error("Digest 阶段失败，但保留文件清单分析结果: %s", exc)
                for item in affected:
                    if item.get("action") != "DELETED_SKILL":
                        item["digest_status"] = "ERROR"
                        item["digest_error"] = str(exc)
        else:
            logger.info("[5/8] Digest 阶段跳过")
            for item in affected:
                item["digest_status"] = "SKIPPED" if item.get("action") != "DELETED_SKILL" else "NOT_APPLICABLE"

        logger.info("[6/8] 写入原始 JSON 结果...")
        payload = {
            "change": {
                "id": detail.get("id"),
                "number": detail.get("_number"),
                "project": project,
                "branch": branch,
                "subject": detail.get("subject"),
                "status": detail.get("status"),
                "patchset": patchset,
                "revision": revision_sha,
                "revision_ref": revision_ref,
            },
            "changed_file_count": len(files),
            "changed_files": [
                {
                    "path": path,
                    "status": info.get("status") or "M",
                    "old_path": info.get("old_path"),
                    "binary": bool(info.get("binary", False)),
                }
                for path, info in files.items()
            ],
            "affected_skill_count": len(affected),
            "affected_skills": affected,
        }
        output_dir = Path(config.get("output_dir", "./output"))
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_change = str(detail.get("_number") or args.change).replace("/", "_")
        output_file = output_dir / "change-{}-patchset-{}.json".format(safe_change, patchset or "current")
        with output_file.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
        logger.info("JSON: %s", output_file)

        logger.info("[7/8] 数据库存档...")
        database.persist_analysis(payload)

        logger.info("[8/8] 刷新 HTML Dashboard...")
        if bool(config.get("auto_generate_report", True)):
            dashboard = generate_report(config, config["report_dir"], logger)
            logger.info("Dashboard: %s", dashboard)
        else:
            logger.info("auto_generate_report=false，跳过 Dashboard 生成")

        if not affected:
            logger.info("本单据未发现新增 SKILL.md，也未命中 Inventory 中已有 Skill Root")
        logger.info("完成。")
        return 0

    except Exception as exc:
        logger.error("执行失败: %s", exc)
        if args.verbose:
            logger.exception("详细异常")
        return 2


if __name__ == "__main__":
    sys.exit(main())
