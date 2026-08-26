#!/usr/bin/env python3
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SkillSource:
    repository: str
    skill_path: str
    skill_name: str
    source_key: str


def normalize_path(path):
    value = (path or "").replace("\\", "/").strip("/")
    while "//" in value:
        value = value.replace("//", "/")
    return value


def is_under(path, root):
    path = normalize_path(path)
    root = normalize_path(root)
    if not root:
        return True
    return path == root or path.startswith(root + "/")


class Inventory:
    def __init__(self, sources=None):
        self.sources = list(sources or [])

    @classmethod
    def from_rows(cls, rows, logger=None, label="Inventory"):
        sources = []
        seen = set()
        for row in rows or []:
            repository = row.get("repository") or row.get("project")
            skill_path = normalize_path(row.get("skill_path"))
            skill_name = row.get("skill_name") or row.get("name")
            if not repository or not skill_name:
                continue
            source_key = row.get("source_key") or "{}|{}|{}".format(repository, skill_path, skill_name)
            if source_key in seen:
                continue
            seen.add(source_key)
            sources.append(SkillSource(repository, skill_path, skill_name, source_key))
        if logger:
            logger.info("已加载 %s: %s 个 Skill Source", label, len(sources))
        return cls(sources)

    @classmethod
    def load(cls, path, logger=None):
        if not path or not os.path.exists(path):
            if logger:
                logger.warning("Baseline Inventory 不存在: %s；将主要依赖 SQLite Inventory", path)
            return cls([])
        with open(path, "r", encoding="utf-8-sig") as fh:
            payload = json.load(fh)
        rows = payload.get("skills", []) if isinstance(payload, dict) else payload
        return cls.from_rows(rows, logger, "Baseline Inventory")

    def merge(self, other, logger=None):
        merged = {}
        for source in list(self.sources) + list(other.sources):
            merged[source.source_key] = source
        self.sources = list(merged.values())
        if logger:
            logger.info("合并后的有效 Inventory: %s 个 Skill Source", len(self.sources))
        return self

    def for_repository(self, repository):
        return [s for s in self.sources if s.repository == repository]

    def exact_root(self, repository, root):
        root = normalize_path(root)
        return [s for s in self.sources if s.repository == repository and normalize_path(s.skill_path) == root]

    def match_path(self, repository, path):
        matches = [s for s in self.sources if s.repository == repository and is_under(path, s.skill_path)]
        matches.sort(key=lambda s: len(normalize_path(s.skill_path)), reverse=True)
        return matches
