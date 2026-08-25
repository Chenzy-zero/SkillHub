#!/usr/bin/env python3
import hashlib
import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(repo, *args, timeout=120):
    cmd = ["git", "-C", str(repo)] + list(args)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(
            "git command failed ({}): {}\n{}".format(
                proc.returncode,
                " ".join(cmd),
                proc.stderr.decode("utf-8", "replace"),
            )
        )
    return proc.stdout


def clone_or_fetch_project(config, project, revision_sha, revision_ref, logger=None):
    workspace = Path(config.get("workspace", "./workspace"))
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace.joinpath(*project.split("/"))
    timeout = int(config.get("git_fetch_timeout_seconds", 120))

    g = config.get("gerrit", {})
    ssh_user = g.get("ssh_username") or g.get("username") or ""
    template = g.get("ssh_url_template")
    if not template:
        raise GitError("config.gerrit.ssh_url_template 未配置")
    remote = template.format(username=ssh_user, project=project)

    if not (repo_dir / ".git").exists():
        if logger:
            logger.info("本地缓存不存在，clone: %s", remote)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--no-checkout", remote, str(repo_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise GitError("git clone failed:\n{}".format(proc.stderr.decode("utf-8", "replace")))
    else:
        if logger:
            logger.info("复用本地仓库缓存: %s", repo_dir)

    fetch_target = revision_ref or revision_sha
    if logger:
        logger.info("fetch 当前 Patchset: %s", fetch_target)
    try:
        run_git(repo_dir, "fetch", "--no-tags", "origin", fetch_target, timeout=timeout)
    except GitError:
        if fetch_target != revision_sha:
            if logger:
                logger.warning("按 revision ref fetch 失败，尝试直接 fetch commit SHA")
            run_git(repo_dir, "fetch", "--no-tags", "origin", revision_sha, timeout=timeout)
        else:
            raise

    resolved = run_git(repo_dir, "rev-parse", revision_sha, timeout=timeout).decode("ascii").strip()
    return repo_dir, resolved


def _parse_ls_tree(raw):
    entries = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path_b = rec.split(b"\t", 1)
        mode_b, type_b, obj_b = meta.split(b" ", 2)
        entries.append(
            {
                "mode": mode_b.decode("ascii"),
                "type": type_b.decode("ascii"),
                "object_id": obj_b.decode("ascii"),
                "path": path_b.decode("utf-8", "surrogateescape"),
            }
        )
    return entries


def calculate_skill_digest(repo, revision, skill_root):
    root = (skill_root or "").replace("\\", "/").strip("/")
    # 必须递归读取整个 Skill Root，而不是只读取该目录的 tree object。
    args = ["ls-tree", "-r", "-z", revision]
    if root:
        args += ["--", root]
    entries = _parse_ls_tree(run_git(repo, *args))

    blobs = []
    for entry in entries:
        path = entry["path"]
        if root and not (path == root or path.startswith(root + "/")):
            continue
        if entry["type"] not in ("blob", "commit"):
            continue
        rel_path = path[len(root) + 1 :] if root and path.startswith(root + "/") else path
        blobs.append((rel_path, entry))
    blobs.sort(key=lambda item: item[0].encode("utf-8", "surrogateescape"))

    manifest = []
    warnings = []
    package_size = 0
    manifest_bytes = bytearray()

    for rel_path, entry in blobs:
        if entry["type"] == "commit" or entry["mode"] == "160000":
            content = ("GITLINK\0" + entry["object_id"]).encode("ascii")
            warnings.append("submodule/gitlink: {}；未递归获取子仓库内容".format(rel_path))
        else:
            content = run_git(repo, "cat-file", "blob", entry["object_id"])

        sha = hashlib.sha256(content).hexdigest()
        package_size += len(content)

        if entry["mode"] == "120000":
            warnings.append("symlink: {} -> {}".format(rel_path, content.decode("utf-8", "replace")))
        if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            warnings.append("Git LFS pointer: {}；当前摘要的是 pointer，不是真实 LFS object".format(rel_path))
        if entry["type"] == "blob" and b"\0" in content[:8192]:
            warnings.append("binary-like file: {}；已计算 raw bytes SHA-256".format(rel_path))

        manifest.append(
            {
                "path": rel_path,
                "mode": entry["mode"],
                "type": entry["type"],
                "sha256": sha,
                "size": len(content),
                "git_object": entry["object_id"],
            }
        )
        manifest_bytes.extend(entry["mode"].encode("ascii"))
        manifest_bytes.extend(b"\0")
        manifest_bytes.extend(rel_path.encode("utf-8", "surrogateescape"))
        manifest_bytes.extend(b"\0")
        manifest_bytes.extend(sha.encode("ascii"))
        manifest_bytes.extend(b"\n")

    return {
        "skill_digest": hashlib.sha256(bytes(manifest_bytes)).hexdigest(),
        "digest_algorithm": "SHA-256",
        "file_count": len(manifest),
        "package_size": package_size,
        "manifest": manifest,
        "warnings": warnings,
    }
