#!/usr/bin/env python3
import re
from pathlib import PurePosixPath

from inventory import normalize_path


def skill_root_from_skill_md(path):
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else normalize_path(parent)


def is_skill_md(path):
    return PurePosixPath(path or "").name == "SKILL.md"


def parse_skill_name(raw, fallback):
    warnings = []
    text = raw.decode("utf-8-sig", "replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        warnings.append("SKILL.md 无 YAML frontmatter，使用目录名作为 skill_name")
        return fallback, warnings
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        warnings.append("SKILL.md frontmatter 未闭合，使用目录名作为 skill_name")
        return fallback, warnings
    pattern = re.compile(r"^name\s*:\s*(.*?)\s*$")
    for line in lines[1:end]:
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if value:
            return value, warnings
    warnings.append("SKILL.md frontmatter 未找到可用 name，使用目录名作为 skill_name")
    return fallback, warnings


def _fallback_name(root, repository):
    return PurePosixPath(root).name if root else repository.split("/")[-1]


def _action_key(item):
    return (
        item.get("action"),
        item.get("source_key"),
        item.get("skill_path"),
        item.get("old_skill_path"),
    )


def analyze_change(client, inventory, change_id, detail, files, logger=None):
    repository = detail.get("project")
    revision = detail.get("current_revision")
    if not repository or not revision:
        raise ValueError("Gerrit change 缺少 project/current_revision")

    results = []
    seen = set()

    def add(item):
        key = _action_key(item)
        if key in seen:
            return
        seen.add(key)
        results.append(item)
        if logger:
            logger.info(
                "识别结果: %-14s repo=%s path=%s name=%s",
                item.get("action"),
                repository,
                item.get("skill_path") or item.get("old_skill_path"),
                item.get("skill_name", ""),
            )

    # 1) 直接处理 SKILL.md 的新增/修改/删除/重命名/复制。
    for path, info in files.items():
        status = info.get("status") or "M"
        old_path = info.get("old_path")
        new_is_skill = is_skill_md(path)
        old_is_skill = is_skill_md(old_path)
        if not new_is_skill and not old_is_skill:
            continue

        if status == "D" and new_is_skill:
            root = skill_root_from_skill_md(path)
            existing = inventory.exact_root(repository, root)
            if existing:
                for src in existing:
                    add(
                        {
                            "action": "DELETED_SKILL",
                            "repository": repository,
                            "skill_path": src.skill_path,
                            "skill_name": src.skill_name,
                            "source_key": src.source_key,
                            "trigger_file": path,
                            "file_status": status,
                            "reason": "SKILL.md deleted",
                        }
                    )
            else:
                add(
                    {
                        "action": "DELETED_SKILL",
                        "repository": repository,
                        "old_skill_path": root,
                        "skill_name": None,
                        "source_key": None,
                        "trigger_file": path,
                        "file_status": status,
                        "reason": "SKILL.md deleted but source not found in inventory",
                        "warnings": ["Inventory 中未找到被删除 Skill Source"],
                    }
                )
            continue

        if status in ("R", "C") and new_is_skill:
            new_root = skill_root_from_skill_md(path)
            old_root = skill_root_from_skill_md(old_path) if old_path else None
            raw = client.get_file_content(change_id, revision, path)
            skill_name, warnings = parse_skill_name(raw, _fallback_name(new_root, repository))
            old_sources = inventory.exact_root(repository, old_root) if old_root is not None else []
            action = "RENAMED_SKILL" if status == "R" else "COPIED_SKILL"
            item = {
                "action": action,
                "repository": repository,
                "skill_path": new_root,
                "old_skill_path": old_root,
                "skill_name": skill_name,
                "source_key": "{}|{}|{}".format(repository, new_root, skill_name),
                "trigger_file": path,
                "old_path": old_path,
                "file_status": status,
                "reason": "SKILL.md {}".format("renamed/moved" if status == "R" else "copied"),
                "warnings": warnings,
            }
            if old_sources:
                item["previous_sources"] = [s.source_key for s in old_sources]
            add(item)
            continue

        if new_is_skill:
            root = skill_root_from_skill_md(path)
            raw = client.get_file_content(change_id, revision, path)
            skill_name, warnings = parse_skill_name(raw, _fallback_name(root, repository))
            exact_root = inventory.exact_root(repository, root)
            same = [s for s in exact_root if s.skill_name == skill_name]
            if same:
                src = same[0]
                add(
                    {
                        "action": "UPDATED_SKILL",
                        "repository": repository,
                        "skill_path": root,
                        "skill_name": skill_name,
                        "source_key": src.source_key,
                        "trigger_file": path,
                        "file_status": status,
                        "reason": "existing SKILL.md changed",
                        "warnings": warnings,
                    }
                )
            else:
                reason = "new SKILL.md"
                previous = []
                if exact_root:
                    reason = "SKILL.md name changed; create a new Skill Source by current policy"
                    previous = [s.source_key for s in exact_root]
                item = {
                    "action": "NEW_SKILL",
                    "repository": repository,
                    "skill_path": root,
                    "skill_name": skill_name,
                    "source_key": "{}|{}|{}".format(repository, root, skill_name),
                    "trigger_file": path,
                    "file_status": status,
                    "reason": reason,
                    "warnings": warnings,
                }
                if previous:
                    item["previous_sources"] = previous
                add(item)

    # 2) 使用 Inventory 判断非 SKILL.md 文件是否落在已有 Skill Root 下。
    for path, info in files.items():
        status = info.get("status") or "M"
        old_path = info.get("old_path")
        candidates = [(path, "new")]
        if old_path:
            candidates.append((old_path, "old"))
        for candidate_path, side in candidates:
            for src in inventory.match_path(repository, candidate_path):
                # 同一个 Root 的 SKILL.md 已在上面精确处理；但父 Skill Root 仍需要保留影响。
                if is_skill_md(candidate_path) and normalize_path(src.skill_path) == skill_root_from_skill_md(candidate_path):
                    continue
                add(
                    {
                        "action": "UPDATED_SKILL",
                        "repository": repository,
                        "skill_path": src.skill_path,
                        "skill_name": src.skill_name,
                        "source_key": src.source_key,
                        "trigger_file": path,
                        "matched_path": candidate_path,
                        "matched_side": side,
                        "file_status": status,
                        "reason": "changed file is inside an existing Skill Root",
                    }
                )

    return results
