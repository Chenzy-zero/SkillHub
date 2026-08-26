#!/usr/bin/env python3
"""POC: discover SKILL.md-based skills from any Git revision and calculate SHA-256 package digests.

Works with both normal and bare repositories. Uses only the Python standard library and git CLI.
Compatible with Python 3.8+.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import PurePosixPath
from typing import Dict, List


class GitError(RuntimeError):
    pass


LOGGER = logging.getLogger("skillhub-poc")


def git(repo, *args, **kwargs):
    input_bytes = kwargs.get("input_bytes")
    cmd = ["git", "-C", repo] + list(args)
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise GitError(
            "git command failed ({}): {}\n{}".format(
                proc.returncode,
                " ".join(cmd),
                proc.stderr.decode("utf-8", "replace"),
            )
        )
    return proc.stdout


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    obj_type: str
    object_id: str
    path: str


@dataclass
class SkillRecord:
    repository: str
    revision: str
    skill_name: str
    skill_path: str
    source_key: str
    skill_digest: str
    digest_algorithm: str
    file_count: int
    package_size: int
    manifest: List[Dict]
    warnings: List[str]


def parse_ls_tree(raw):
    entries = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path_b = rec.split(b"\t", 1)
        mode_b, type_b, obj_b = meta.split(b" ", 2)
        entries.append(
            TreeEntry(
                mode=mode_b.decode("ascii"),
                obj_type=type_b.decode("ascii"),
                object_id=obj_b.decode("ascii"),
                path=path_b.decode("utf-8", "surrogateescape"),
            )
        )
    return entries


def list_tree(repo, revision):
    # -r is required; without it git ls-tree only returns the top-level tree.
    return parse_ls_tree(git(repo, "ls-tree", "-r", "-z", revision))


class GitObjectReader:
    """常驻 `git cat-file --batch` 进程，按 object id 批量读取 Git 对象内容。

    旧实现每个文件起一次 `git show` 子进程；在 Windows + 安全软件环境下
    单次进程创建可达 1 秒，上万个文件的仓库会卡住数小时。常驻进程把
    进程创建次数从 O(文件数) 降为 O(1)，代价只是管道读写。
    """

    _CHUNK = 64

    def __init__(self, repo):
        self._proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout

    def read_blob(self, object_id):
        """读取单个对象内容；object_id 必须是 ls-tree 给出的完整哈希。"""
        self._stdin.write(object_id.encode("ascii") + b"\n")
        self._stdin.flush()
        return self._read_one()

    def read_blobs(self, object_ids):
        """批量读取对象，按输入顺序产出 (object_id, bytes)。

        以 _CHUNK 个请求为单位预写入 stdin 再顺序读取 stdout 响应。
        响应被完整消费前不会写下一批请求，因此不会写满管道造成死锁。
        调用方必须完整消费本生成器，否则进程流将失步。
        """
        ids = list(object_ids)
        for start in range(0, len(ids), self._CHUNK):
            chunk = ids[start : start + self._CHUNK]
            self._stdin.write(b"".join(oid.encode("ascii") + b"\n" for oid in chunk))
            self._stdin.flush()
            for oid in chunk:
                yield oid, self._read_one()

    def _read_one(self):
        """从 cat-file 输出流解析一条响应：头行 + 指定字节数内容 + 结尾换行。"""
        header = self._stdout.readline()
        if not header.endswith(b"\n"):
            raise GitError("git cat-file --batch 输出意外终止: " + self._stderr_tail())
        parts = header.rstrip(b"\n").split(b" ")
        if len(parts) == 2 and parts[1] == b"missing":
            raise GitError("对象不存在: " + parts[0].decode("ascii", "replace"))
        if len(parts) != 3:
            raise GitError("无法解析 cat-file 响应头: {!r}".format(header))
        object_id = parts[0].decode("ascii", "replace")
        size = int(parts[2])
        content = self._stdout.read(size)
        if len(content) != size:
            raise GitError("对象内容被截断: " + object_id)
        if self._stdout.read(1) != b"\n":
            raise GitError("对象内容后缺少换行分隔符: " + object_id)
        return content

    def _stderr_tail(self, limit=500):
        """进程异常退出时提取 stderr 尾部内容，用于错误诊断。"""
        if self._proc.poll() is None:
            return "<git cat-file 仍在运行>"
        try:
            data = self._proc.stderr.read() or b""
        except (OSError, ValueError):
            return "<无法读取 stderr>"
        return data.decode("utf-8", "replace").strip()[-limit:]

    def close(self):
        """关闭常驻进程并释放管道；幂等，可安全重复调用。"""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _is_gitlink(entry):
    """判断 tree entry 是否为 submodule/gitlink（commit 类型或 160000 模式）。"""
    return entry.obj_type == "commit" or entry.mode == "160000"


def parse_skill_name(skill_md, fallback):
    warnings = []
    text = skill_md.decode("utf-8-sig", "replace")
    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        warnings.append("SKILL.md has no YAML frontmatter; directory name used as skill_name")
        return fallback, warnings

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        warnings.append("SKILL.md frontmatter is not closed; directory name used as skill_name")
        return fallback, warnings

    name_re = re.compile(r"^name\s*:\s*(.*?)\s*$")
    for line in lines[1:end]:
        match = name_re.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            break
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if value:
            return value, warnings

    warnings.append("SKILL.md frontmatter has no usable name; directory name used as skill_name")
    return fallback, warnings


def is_under(path, root):
    if not root:
        return True
    return path == root or path.startswith(root.rstrip("/") + "/")


def rel_to_root(path, root):
    if not root:
        return path
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else path


def package_digest(reader, root, entries):
    """计算一个 Skill 包的文件清单、整体 SHA-256 摘要与告警。

    通过常驻 cat-file 进程按相对路径排序顺序流式读取每个文件内容，
    内存占用只与单个文件体积相关。reader 必须是已打开的 GitObjectReader。
    """
    warnings = []
    manifest = []
    package_size = 0

    package_entries = [
        entry
        for entry in entries
        if entry.obj_type in {"blob", "commit"} and is_under(entry.path, root)
    ]
    package_entries.sort(
        key=lambda entry: rel_to_root(entry.path, root).encode("utf-8", "surrogateescape")
    )

    blob_entries = [entry for entry in package_entries if not _is_gitlink(entry)]
    content_iter = reader.read_blobs(entry.object_id for entry in blob_entries)
    total_blobs = len(blob_entries)
    processed = 0

    manifest_bytes = bytearray()
    for entry in package_entries:
        rel_path = rel_to_root(entry.path, root)

        if _is_gitlink(entry):
            content = ("GITLINK\0" + entry.object_id).encode("ascii")
            warnings.append(
                "submodule/gitlink detected: {}; actual child repository content is not present "
                "in this Git tree".format(rel_path)
            )
        else:
            content = next(content_iter)[1]
            processed += 1
            if total_blobs >= 2000 and processed % 2000 == 0:
                LOGGER.info("  进度 [%s]: 已读取 %d/%d 个文件", root or ".", processed, total_blobs)

        file_sha256 = hashlib.sha256(content).hexdigest()
        package_size += len(content)

        if entry.mode == "120000":
            target = content.decode("utf-8", "replace")
            warnings.append(
                "symlink detected: {} -> {}; POC hashes the link target text and does not "
                "follow it".format(rel_path, target)
            )
        if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            warnings.append(
                "Git LFS pointer detected: {}; POC hashes the pointer, not the external LFS "
                "object".format(rel_path)
            )
        if entry.obj_type == "blob" and b"\0" in content[:8192]:
            warnings.append(
                "binary-like blob detected: {}; POC hashes raw bytes but performs no semantic "
                "binary analysis".format(rel_path)
            )

        manifest.append(
            {
                "path": rel_path,
                "mode": entry.mode,
                "type": entry.obj_type,
                "sha256": file_sha256,
                "size": len(content),
                "git_object": entry.object_id,
            }
        )
        manifest_bytes.extend(entry.mode.encode("ascii"))
        manifest_bytes.extend(b"\0")
        manifest_bytes.extend(rel_path.encode("utf-8", "surrogateescape"))
        manifest_bytes.extend(b"\0")
        manifest_bytes.extend(file_sha256.encode("ascii"))
        manifest_bytes.extend(b"\n")

    digest = hashlib.sha256(bytes(manifest_bytes)).hexdigest()
    return digest, len(package_entries), package_size, manifest, warnings


def scan_revision(repo, revision, repository_name=None):
    """扫描一个 revision 的完整文件树，返回全部 SKILL.md 对应的 SkillRecord 列表。"""
    revision = git(repo, "rev-parse", revision).decode("ascii").strip()
    if not repository_name:
        repository_name = os.path.basename(os.path.abspath(repo))
        if repository_name.endswith(".git"):
            repository_name = repository_name[:-4]

    entries = list_tree(repo, revision)
    skill_entries = sorted(
        (
            entry
            for entry in entries
            if entry.obj_type == "blob" and PurePosixPath(entry.path).name == "SKILL.md"
        ),
        key=lambda entry: entry.path,
    )

    records = []
    roots = [str(PurePosixPath(entry.path).parent) for entry in skill_entries]
    roots = ["" if root == "." else root for root in roots]

    with GitObjectReader(repo) as reader:
        for skill_entry, root in zip(skill_entries, roots):
            fallback_name = PurePosixPath(root).name if root else repository_name
            skill_md = reader.read_blob(skill_entry.object_id)
            skill_name, warnings = parse_skill_name(skill_md, fallback_name)

            nested = [
                candidate
                for candidate in roots
                if candidate != root
                and (not root or candidate.startswith(root.rstrip("/") + "/"))
            ]
            if nested:
                warnings.append(
                    "nested Skill Root(s) detected; parent digest currently includes nested content: "
                    + ", ".join(nested)
                )

            digest, file_count, package_size, manifest, digest_warnings = package_digest(
                reader, root, entries
            )
            warnings.extend(digest_warnings)

            source_key = "{}|{}|{}".format(repository_name, root, skill_name)
            records.append(
                SkillRecord(
                    repository=repository_name,
                    revision=revision,
                    skill_name=skill_name,
                    skill_path=root,
                    source_key=source_key,
                    skill_digest=digest,
                    digest_algorithm="SHA-256",
                    file_count=file_count,
                    package_size=package_size,
                    manifest=manifest,
                    warnings=warnings,
                )
            )
    return records


def main():
    parser = argparse.ArgumentParser(
        description="Discover SKILL.md roots and calculate package SHA-256 digests for a Git revision"
    )
    parser.add_argument("--repo", required=True, help="Path to a normal or bare Git repository")
    parser.add_argument("--revision", default="HEAD", help="Commit/revision to scan")
    parser.add_argument("--repository-name", help="Logical Gerrit project/repository name")
    parser.add_argument("--output", choices=["json", "jsonl"], default="json")
    parser.add_argument("--no-manifest", action="store_true", help="Omit per-file manifest from output")
    args = parser.parse_args()

    try:
        records = scan_revision(args.repo, args.revision, args.repository_name)
    except (GitError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = []
    for record in records:
        item = asdict(record)
        if args.no_manifest:
            item.pop("manifest", None)
        payload.append(item)

    if args.output == "jsonl":
        for item in payload:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
