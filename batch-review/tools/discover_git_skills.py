#!/usr/bin/env python3
"""Generate the formal Skill inventory CSV from one fixed Git revision.

The command is read-only with respect to Git. It scans the tree recorded by a
commit instead of the current checkout, so local uncommitted files cannot leak
into a validation batch.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Sequence


CSV_FIELDS = (
    "skill_id",
    "skill_name",
    "repo_name",
    "branch",
    "skill_path",
    "latest_commitid",
    "security_reviewed",
    "status",
    "update_time",
    "history_id",
)
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_FRONTMATTER_NAME_RE = re.compile(r"(?m)^name\s*:\s*(.+?)\s*$")


class DiscoveryError(RuntimeError):
    pass


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            shell=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise DiscoveryError("git executable is unavailable") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise DiscoveryError(f"git command failed: {message[:500]}")
    return result.stdout


def _frontmatter_name(content: str, fallback: str) -> str:
    if not content.startswith("---"):
        return fallback
    parts = content.split("---", 2)
    if len(parts) < 3:
        return fallback
    match = _FRONTMATTER_NAME_RE.search(parts[1])
    if not match:
        return fallback
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value or fallback


def _stable_skill_id(repo_name: str, branch: str, skill_path: str, skill_name: str) -> str:
    value = "\0".join((repo_name, branch, skill_path, skill_name)).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


def discover(
    repository: Path,
    *,
    repo_name: str,
    branch: str,
    revision: str,
) -> tuple[str, list[dict[str, str]]]:
    repository = repository.expanduser().resolve()
    if not (repository / ".git").exists():
        raise DiscoveryError(f"not a Git working repository: {repository}")
    if not _REPOSITORY_RE.fullmatch(repo_name) or ".." in repo_name.split("/"):
        raise DiscoveryError("repo_name must be an owner/project style path")
    if not branch or any(value in branch for value in ("..", "\\", "\x00")):
        raise DiscoveryError("branch is invalid")
    commit = _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    source_revision = commit.decode("ascii", errors="strict").strip().lower()
    tree = _git(repository, "ls-tree", "-r", "-z", "--name-only", source_revision)
    paths = sorted(
        item.decode("utf-8", errors="strict")
        for item in tree.split(b"\0")
        if item and PurePosixPath(item.decode("utf-8", errors="strict")).name == "SKILL.md"
    )
    rows: list[dict[str, str]] = []
    for index, anchor_path in enumerate(paths, 1):
        root = str(PurePosixPath(anchor_path).parent)
        if root == ".":
            root = "."
        fallback_name = PurePosixPath(root).name if root != "." else repo_name.rsplit("/", 1)[-1]
        content = _git(repository, "show", f"{source_revision}:{anchor_path}").decode(
            "utf-8", errors="replace"
        )
        skill_name = _frontmatter_name(content, fallback_name)
        change = _git(
            repository,
            "log",
            "-1",
            "--format=%H%x00%cI",
            source_revision,
            "--",
            root,
        ).decode("utf-8", errors="strict").rstrip("\n")
        values = change.split("\0", 1)
        if len(values) != 2:
            raise DiscoveryError(f"cannot resolve last change for {root}")
        rows.append(
            {
                "skill_id": _stable_skill_id(repo_name, branch, root, skill_name),
                "skill_name": skill_name,
                "repo_name": repo_name,
                "branch": branch,
                "skill_path": root,
                "latest_commitid": values[0].lower(),
                "security_reviewed": "否",
                "status": "新增",
                "update_time": values[1],
                "history_id": str(index),
            }
        )
    return source_revision, rows


def write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从固定 Git Revision 生成 Skill 台账 CSV。")
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        revision, rows = discover(
            args.repository,
            repo_name=args.repo_name,
            branch=args.branch,
            revision=args.revision,
        )
        write_csv(args.output, rows)
    except (DiscoveryError, OSError, UnicodeError, ValueError) as exc:
        print(f"错误: {exc}", file=os.sys.stderr)
        return 2
    print(f"固定版本: {revision}")
    print(f"Skill 数量: {len(rows)}")
    print(f"台账文件: {args.output.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
