#!/usr/bin/env python3
"""POC: discover SKILL.md-based skills from any Git revision and calculate SHA-256 package digests.

Works with both normal and bare repositories. Uses only the Python standard library and git CLI.
Compatible with Python 3.8+.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import PurePosixPath
from typing import Dict, List


class GitError(RuntimeError):
    pass


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


def read_blob(repo, revision, path):
    return git(repo, "show", "{}:{}".format(revision, path))


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


def package_digest(repo, revision, root, entries):
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

    manifest_bytes = bytearray()
    for entry in package_entries:
        rel_path = rel_to_root(entry.path, root)

        if entry.obj_type == "commit" or entry.mode == "160000":
            content = ("GITLINK\0" + entry.object_id).encode("ascii")
            warnings.append(
                "submodule/gitlink detected: {}; actual child repository content is not present "
                "in this Git tree".format(rel_path)
            )
        else:
            content = read_blob(repo, revision, entry.path)

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
    revision = git(repo, "rev-parse", revision).decode("ascii").strip()
    if not repository_name:
        repository_name = os.path.basename(os.path.abspath(repo))
        if repository_name.endswith(".git"):
            repository_name = repository_name[:-4]

    entries = list_tree(repo, revision)
    skill_md_paths = sorted(
        [
            entry.path
            for entry in entries
            if entry.obj_type == "blob" and PurePosixPath(entry.path).name == "SKILL.md"
        ]
    )

    records = []
    roots = [str(PurePosixPath(path).parent) for path in skill_md_paths]
    roots = ["" if root == "." else root for root in roots]

    for skill_md_path, root in zip(skill_md_paths, roots):
        fallback_name = PurePosixPath(root).name if root else repository_name
        skill_md = read_blob(repo, revision, skill_md_path)
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
            repo,
            revision,
            root,
            entries,
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
