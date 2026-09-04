"""Per-Skill download, archive, review and durable result workflow."""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .ai_review import (
    AIReviewExpectation,
    AIReviewSourceMetadata,
    build_ai_review_handoff,
    load_and_validate_ai_review_result,
)
from .artifacts import EvidenceStore, safe_join
from .config import ReviewConfig
from .filesystem import remove_tree
from .git_source import GitCommandError, GitRunner
from .inventory import InventoryDocument, InventoryRow
from .orchestrator import _default_adapters, _run_static_scans, _scan_summary
from .result_reuse import (
    COMPARE_METHOD,
    REUSE_STATUS,
    build_review_fingerprint,
    find_reusable_result,
    publish_reusable_result,
    skill_root_name,
)
from .review_policy import evaluate_policy
from .scanners import ScannerAdapter
from .snapshot import SnapshotResult, export_skill_archive_snapshot, export_skill_snapshot


class PerSkillError(RuntimeError):
    """A per-Skill operation cannot be completed safely."""


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILTER_UNSUPPORTED = (
    "filtering not recognized by server",
    "server does not support filter",
    "filter capability is not supported",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _component(value: str, field: str) -> str:
    text = str(value).strip()
    if not _SAFE_COMPONENT.fullmatch(text) or text in {".", ".."}:
        raise PerSkillError(f"{field} is not a safe directory component: {value!r}")
    return text


def _skill_id(row: InventoryRow) -> str:
    value = row.trace_values.get("skill_id", "")
    if not value:
        raise PerSkillError(f"CSV row {row.row_number} has no skill_id")
    return _component(value, "skill_id")


def _task_id(row: InventoryRow) -> str:
    encoded = json.dumps(
        [_skill_id(row), row.repo_name, row.branch, row.skill_path, row.inventory_revision],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "skill-" + hashlib.sha256(encoded).hexdigest()[:24]


def skill_task_id(row: InventoryRow) -> str:
    """Public stable task identifier used by the launcher and cleanup guard."""

    return _task_id(row)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


@dataclass(frozen=True, slots=True)
class PartialDownload:
    task_root: Path
    repository: Path
    revision: str
    snapshot: SnapshotResult | None = None
    transport: str = "partial_clone"


@dataclass(frozen=True, slots=True)
class RepositoryDownload:
    """One frozen repository archive shared by every listed Skill in it."""

    workspace_root: Path
    repository: str
    branch: str
    revision: str
    skills: Mapping[str, PartialDownload]
    transport: str


def _repository_task_id(repository: str, branch: str) -> str:
    encoded = json.dumps(
        [repository, branch], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "repo-" + hashlib.sha256(encoded).hexdigest()[:24]


def download_repository_skills(
    config: ReviewConfig,
    *,
    batch_id: str,
    rows: Sequence[InventoryRow],
    runner: GitRunner | None = None,
) -> RepositoryDownload:
    """Download one frozen repository revision and extract all listed Skills.

    Gerrit in the target environment does not support path-limited archives or
    partial clone filtering. Consequently the transport downloads one
    history-free tar for a repository/branch, while the materializer writes
    only the CSV allowlisted Skill roots. The tar is deleted before return.
    """

    if not rows:
        raise PerSkillError("repository download requires at least one Skill")
    repository_name = rows[0].repo_name
    branch = rows[0].branch
    if any(row.repo_name != repository_name or row.branch != branch for row in rows):
        raise PerSkillError("repository download rows must share repo_name and branch")
    if len({row.source_row_id for row in rows}) != len(rows):
        raise PerSkillError("repository download contains duplicate Skill rows")
    for row in rows:
        root_name = _component(skill_root_name(row.skill_path, row.skill_name), "skill_name")
        if row.skill_name != root_name:
            raise PerSkillError(
                f"skill_name {row.skill_name!r} must equal Skill Root directory name {root_name!r}"
            )

    batch_root = (config.workspace.git_download_root / batch_id).resolve()
    workspace_root = (batch_root / _repository_task_id(repository_name, branch)).resolve()
    download_root = config.workspace.git_download_root.resolve()
    if workspace_root.parent.parent != download_root:
        raise PerSkillError("repository workspace escaped git_download_root")
    if batch_root.exists():
        leftovers = list(batch_root.iterdir())
        if (
            len(leftovers) == 1
            and leftovers[0] == workspace_root
            and workspace_root.is_dir()
            and not workspace_root.is_symlink()
            and not config.workspace.keep_failed_workspace
        ):
            remove_tree(workspace_root)
            leftovers = []
        if leftovers:
            raise PerSkillError(
                "git_download is not empty; finish the current repository first: "
                + ", ".join(path.name for path in leftovers[:5])
            )
    workspace_root.mkdir(parents=True)

    active_runner = runner
    if active_runner is None:
        environment = os.environ.copy()
        if config.gerrit.ssh_identity_file is not None:
            environment["GIT_SSH_COMMAND"] = " ".join(
                shlex.quote(value)
                for value in (
                    "ssh",
                    "-i",
                    str(config.gerrit.ssh_identity_file),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                )
            )
        active_runner = GitRunner(env=environment)

    url = config.gerrit.repository_url(repository_name, branch=branch)
    ref = f"refs/heads/{branch}"
    archive_path = workspace_root / ".repository-archive.tar"
    try:
        remote = active_runner.checked(("ls-remote", "--exit-code", url, ref), cwd=workspace_root)
        matches = [line.split() for line in remote.stdout.splitlines() if line.strip()]
        if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1] != ref:
            raise PerSkillError(f"cannot resolve exactly one remote branch: {ref}")
        revision = matches[0][0].lower()
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision):
            raise PerSkillError("Gerrit returned an invalid branch revision")

        try:
            active_runner.checked(
                (
                    "archive",
                    f"--remote={url}",
                    "--format=tar",
                    f"--output={archive_path}",
                    revision,
                ),
                cwd=workspace_root,
            )
        except GitCommandError as exc:
            raise PerSkillError(
                "REPOSITORY_ARCHIVE_UNAVAILABLE: Gerrit could not export the frozen "
                "repository revision"
            ) from exc
        if not archive_path.is_file():
            raise PerSkillError("Gerrit reported success without a repository archive")

        downloads: dict[str, PartialDownload] = {}
        for row in rows:
            task_root = workspace_root / _task_id(row)
            root_name = _component(skill_root_name(row.skill_path, row.skill_name), "skill_name")
            snapshot = export_skill_archive_snapshot(
                archive_path,
                repository_name,
                revision,
                row.skill_path,
                task_root / root_name,
            )
            downloads[row.source_row_id] = PartialDownload(
                task_root,
                workspace_root,
                revision,
                snapshot=snapshot,
                transport="whole_repository_archive",
            )
        archive_path.unlink()
        return RepositoryDownload(
            workspace_root,
            repository_name,
            branch,
            revision,
            downloads,
            "whole_repository_archive",
        )
    except Exception as operation_error:
        if workspace_root.exists() and not config.workspace.keep_failed_workspace:
            try:
                remove_tree(workspace_root)
            except OSError as cleanup_error:
                raise PerSkillError(
                    f"{operation_error}; repository workspace cleanup also failed: {cleanup_error}"
                ) from operation_error
        raise


def cleanup_repository_download(
    config: ReviewConfig, *, batch_id: str, repository: str, branch: str
) -> bool:
    target = (
        config.workspace.git_download_root
        / batch_id
        / _repository_task_id(repository, branch)
    ).resolve()
    root = config.workspace.git_download_root.resolve()
    if target.parent.parent != root or not target.name.startswith("repo-"):
        raise PerSkillError("repository cleanup target escaped git_download_root")
    if not target.exists():
        return False
    if target.is_symlink() or not target.is_dir():
        raise PerSkillError("repository cleanup target is not a real directory")
    remove_tree(target)
    return True


def partial_fetch_skill_repository(
    config: ReviewConfig,
    *,
    batch_id: str,
    row: InventoryRow,
    task_id: str | None = None,
    runner: GitRunner | None = None,
) -> PartialDownload:
    """Download exactly the Skill Root recorded in the inventory at its pinned CSV revision.

    The pinned CSV revision is reviewed, never the live branch tip, so a newer push
    cannot silently change what is audited.  Gerrit's ``git archive --remote`` cannot
    limit paths (it rejects ``-- <path>``), so the server-side transport requests a
    whole-repository tar at the pinned commit and the archive snapshot keeps only the
    requested Skill Root.  Servers without upload-archive fall back to a blobless
    fetch of the branch tip, which is only acceptable when the tip still equals the
    pinned CSV revision.
    """

    batch_root = (config.workspace.git_download_root / batch_id).resolve()
    task_root = (batch_root / (task_id or _task_id(row))).resolve()
    download_root = config.workspace.git_download_root.resolve()
    if task_root.parent.parent != download_root:
        raise PerSkillError("temporary download target escaped git_download_root")
    if batch_root.exists():
        leftovers = [path for path in batch_root.iterdir()]
        if (
            len(leftovers) == 1
            and leftovers[0] == task_root
            and task_root.is_dir()
            and not task_root.is_symlink()
            and not config.workspace.keep_failed_workspace
        ):
            # A prior attempt of this exact deterministic task may have been
            # interrupted while Windows still marked Git pack files read-only.
            # This is the only stale directory that can be removed implicitly.
            remove_tree(task_root)
            leftovers = []
        if leftovers:
            raise PerSkillError(
                "git_download is not empty; finish and clean the previous Skill before continuing: "
                + ", ".join(path.name for path in leftovers[:5])
            )
    if task_root.exists() or task_root.is_symlink():
        raise PerSkillError(f"temporary download directory is not empty: {task_root}")
    repository = task_root / ".transport.git"
    task_root.mkdir(parents=True)
    active_runner = runner
    if active_runner is None:
        environment = os.environ.copy()
        if config.gerrit.ssh_identity_file is not None:
            environment["GIT_SSH_COMMAND"] = " ".join(
                shlex.quote(value)
                for value in (
                    "ssh",
                    "-i",
                    str(config.gerrit.ssh_identity_file),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                )
            )
        active_runner = GitRunner(env=environment)
    url = config.gerrit.repository_url(row.repo_name, branch=row.branch)
    try:
        ref = f"refs/heads/{row.branch}"
        remote = active_runner.checked(("ls-remote", "--exit-code", url, ref), cwd=task_root)
        matches = [
            line.split()
            for line in remote.stdout.splitlines()
            if line.strip()
        ]
        if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1] != ref:
            raise PerSkillError(f"cannot resolve exactly one remote branch: {ref}")
        head = matches[0][0].lower()
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head):
            raise PerSkillError("Gerrit returned an invalid branch revision")
        pinned = str(row.inventory_revision or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", pinned):
            raise PerSkillError(
                f"CSV row has no reviewable pinned revision: {row.inventory_revision!r}"
            )
        if pinned != head:
            print(
                f"notice: branch {ref} head {head} differs from the pinned CSV revision "
                f"{pinned}; reviewing the pinned revision as recorded in the inventory"
            )

        # Gerrit upload-archive cannot restrict paths (it rejects "-- <path>"), so a
        # whole-repository tar at the pinned revision is requested and
        # export_skill_archive_snapshot materializes only the Skill Root subtree.
        archive_path = task_root / ".skill-archive.tar"
        archive_command = [
            "archive",
            f"--remote={url}",
            "--format=tar",
            f"--output={archive_path}",
            pinned,
        ]
        try:
            active_runner.checked(tuple(archive_command), cwd=task_root)
        except GitCommandError:
            if pinned != head:
                raise PerSkillError(
                    "PINNED_REVISION_UNAVAILABLE: the server cannot remote-archive the pinned "
                    f"CSV revision {pinned} (branch head is {head}) and partial-clone cannot "
                    "fetch an unadvertised commit; refresh the CSV revision or use a server "
                    "that can serve the pinned object"
                ) from None
            archive_path.unlink(missing_ok=True)
            active_runner.checked(("init", "--bare", str(repository)), cwd=task_root)
            active_runner.checked(("remote", "add", "origin", url), cwd=repository)
            result = active_runner.run(
                (
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "--filter=blob:none",
                    "origin",
                    f"{ref}:{ref}",
                ),
                cwd=repository,
                check=False,
            )
            diagnostic = (result.stdout + "\n" + result.stderr).lower()
            if any(marker in diagnostic for marker in _FILTER_UNSUPPORTED):
                raise PerSkillError(
                    "SKILL_ONLY_DOWNLOAD_UNSUPPORTED: Gerrit supports neither remote archive "
                    "nor partial-clone filtering; full-repository fallback is disabled"
                )
            if not result.ok:
                raise PerSkillError(f"partial fetch failed with exit code {result.returncode}: {result.stderr[:500]}")
            fetched_head = active_runner.checked(
                ("rev-parse", f"{ref}^{{commit}}"), cwd=repository
            ).stdout.strip().lower()
            if fetched_head != head:
                raise PerSkillError(
                    f"STALE_INVENTORY: fetched branch head {fetched_head} differs from remote {head}"
                )
            return PartialDownload(task_root, repository, head, transport="partial_clone")
        else:
            if not archive_path.is_file():
                raise PerSkillError("Gerrit remote archive reported success without an archive file")
            root_name = _component(
                skill_root_name(row.skill_path, row.skill_name), "skill_name"
            )
            snapshot = export_skill_archive_snapshot(
                archive_path,
                row.repo_name,
                pinned,
                row.skill_path,
                task_root / root_name,
            )
            archive_path.unlink()
            return PartialDownload(
                task_root,
                task_root,
                pinned,
                snapshot=snapshot,
                transport="remote_archive",
            )
    except Exception as operation_error:
        if task_root.exists() and not config.workspace.keep_failed_workspace:
            try:
                remove_tree(task_root)
            except OSError as cleanup_error:
                raise PerSkillError(
                    f"{operation_error}; temporary Git cleanup also failed: {cleanup_error}"
                ) from operation_error
        raise


def _archive_snapshot(config: ReviewConfig, row: InventoryRow, snapshot: SnapshotResult) -> SnapshotResult:
    identifier = _skill_id(row)
    name = _component(skill_root_name(row.skill_path, row.skill_name), "skill_name")
    if name != row.skill_name:
        raise PerSkillError(
            f"skill_name {row.skill_name!r} must equal Skill Root directory name {name!r}"
        )
    skill_id_root = (config.workspace.skills_root / identifier).resolve()
    skills_root = config.workspace.skills_root.resolve()
    if skill_id_root.parent != skills_root:
        raise PerSkillError("Skill output escaped skills_root")
    target = skill_id_root / name
    skill_id_root.mkdir(parents=True, exist_ok=True)
    metadata_path = skill_id_root / "source-metadata.json"
    metadata = {
        "schema_version": "1.0",
        "skill_id": identifier,
        "skill_name": name,
        "repo_name": row.repo_name,
        "branch": row.branch,
        "skill_path": row.skill_path,
        "inventory_revision": row.inventory_revision,
        "source_revision": snapshot.source_revision,
        "skill_digest": snapshot.skill_digest,
        "content_id": f"sha256:{snapshot.skill_digest}",
        "trace_values": row.trace_values,
        "source_row_id": row.source_row_id,
    }
    if target.exists():
        if target.is_symlink() or metadata_path.is_symlink() or not metadata_path.is_file():
            raise PerSkillError(f"OUTPUT_CONFLICT: unsafe existing Skill output {target}")
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if existing != metadata:
            raise PerSkillError(f"OUTPUT_CONFLICT: skill_id {identifier} already contains different content")
        remove_tree(snapshot.snapshot_path)
    else:
        try:
            os.replace(snapshot.snapshot_path, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            staging = skill_id_root / f".{name}.staging-{uuid.uuid4().hex}"
            try:
                shutil.copytree(snapshot.snapshot_path, staging, symlinks=True)
                os.replace(staging, target)
                remove_tree(snapshot.snapshot_path)
            finally:
                if staging.exists():
                    remove_tree(staging)
        try:
            _atomic_json(metadata_path, metadata)
        except Exception:
            if target.exists() and not target.is_symlink():
                remove_tree(target)
            raise
    return replace(snapshot, snapshot_path=target)


def _verify_archived_snapshot(snapshot: Any) -> None:
    expected: dict[str, Any] = {
        entry.relative_path: entry
        for entry in snapshot.entries
        if entry.file_type not in {"symlink", "submodule"}
    }
    actual: set[str] = set()
    for path in snapshot.snapshot_path.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(snapshot.snapshot_path).as_posix()
        if path.is_symlink() or not path.is_file():
            raise PerSkillError(f"archived Skill contains an unsafe entry: {relative}")
        actual.add(relative)
    if actual != set(expected):
        raise PerSkillError("archived Skill file set no longer matches the frozen snapshot")
    for relative, entry in expected.items():
        path = snapshot.snapshot_path.joinpath(*relative.split("/"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        mode = "100755" if stat.S_IMODE(path.stat().st_mode) & 0o111 else "100644"
        if digest != entry.sha256 or mode != entry.mode:
            raise PerSkillError(f"archived Skill changed after download: {relative}")


def _source_mapping(config: ReviewConfig, row: InventoryRow, snapshot: SnapshotResult) -> dict[str, Any]:
    result_path = config.workspace.skills_root / _skill_id(row) / "review-result.json"
    return {
        "source_row_id": row.source_row_id,
        "source_row_numbers": list(row.source_row_numbers),
        "skill_id": _skill_id(row),
        "skill_name": row.skill_name,
        "repo_name": row.repo_name,
        "branch": row.branch,
        "skill_path": row.skill_path,
        "inventory_revision": row.inventory_revision,
        "source_revision": snapshot.source_revision,
        "skill_digest": snapshot.skill_digest,
        "content_id": f"sha256:{snapshot.skill_digest}",
        "review_result_path": str(result_path),
        **{name: row.trace_values.get(name, "") for name in ("product_line", "user_name", "user_email")},
    }


def _finding_counts(findings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    values = {name: 0 for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}
    for finding in findings:
        severity = str(finding.get("severity") or "INFO").upper()
        if severity in values:
            values[severity] += 1
    return values


def _finding_summary(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = _finding_counts(findings)
    maximum = next(
        (name for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO") if counts[name]),
        "NONE",
    )
    return {
        "findings": list(findings),
        "finding_counts": counts,
        "finding_count": sum(counts.values()),
        "max_severity": maximum,
    }


def _durable_result(
    config: ReviewConfig,
    source: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Path:
    root = config.workspace.skills_root / str(source["skill_id"])
    return _atomic_json(root / "review-result.json", payload)


@dataclass(frozen=True, slots=True)
class SkillPreparation:
    task_id: str
    skill_id: str
    snapshot: SnapshotResult
    index_path: Path
    handoff_path: Path | None
    result: Mapping[str, Any] | None
    download_root: Path

    @property
    def requires_ai(self) -> bool:
        return self.handoff_path is not None and self.result is None


def prepare_skill(
    config: ReviewConfig,
    *,
    batch_id: str,
    row: InventoryRow,
    downloader: Callable[..., PartialDownload] = partial_fetch_skill_repository,
    downloaded: PartialDownload | None = None,
    adapters: Mapping[str, ScannerAdapter] | None = None,
) -> SkillPreparation:
    """Download, archive and statically review exactly one CSV Skill."""

    task_id = _task_id(row)
    root_name = _component(skill_root_name(row.skill_path, row.skill_name), "skill_name")
    if row.skill_name != root_name:
        raise PerSkillError(
            f"skill_name {row.skill_name!r} must equal Skill Root directory name {root_name!r}"
        )
    downloaded = downloaded or downloader(
        config, batch_id=batch_id, row=row, task_id=task_id
    )
    export_root = downloaded.task_root / root_name
    snapshot = downloaded.snapshot or export_skill_snapshot(
        downloaded.repository,
        downloaded.revision,
        row.skill_path,
        export_root,
    )
    snapshot = _archive_snapshot(config, row, snapshot)
    source = _source_mapping(config, row, snapshot)
    source["download_transport"] = downloaded.transport
    evidence = EvidenceStore(
        config.workspace.evidence_root,
        batch_id,
        task_id,
        candidate_root=config.workspace.candidate_root,
    )
    evidence.write_json("package-manifest.json", snapshot.manifest_dict())
    evidence.write_json("source-metadata.json", source)
    active_adapters = dict(adapters or _default_adapters(config))
    fingerprint = build_review_fingerprint(config, active_adapters)
    index_path = config.workspace.manifest_root / batch_id / "skills" / f"{task_id}.json"
    base_index = {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "task_id": task_id,
        "source": source,
        "snapshot": snapshot.manifest_dict(),
        "snapshot_path": str(snapshot.snapshot_path),
        "download_root": str(downloaded.task_root),
        "evidence_root": str(evidence.task_root),
        "review_fingerprint": fingerprint,
    }
    if not snapshot.coverage_complete:
        result = {
            **source,
            "schema_version": "1.0",
            "review_status": "INCOMPLETE",
            "security_decision": "INCOMPLETE",
            "quality_decision": "INCOMPLETE",
            "quality_score": None,
            **_finding_summary([]),
            "failure_reason": "; ".join(item.code for item in snapshot.blocking_issues),
            "evidence_ref": str(evidence.task_root),
        }
        evidence.write_json("final-result.json", result)
        _durable_result(config, source, result)
        _atomic_json(index_path, {**base_index, "status": "INCOMPLETE", "result": result})
        return SkillPreparation(task_id, _skill_id(row), snapshot, index_path, None, result, downloaded.task_root)
    reusable = find_reusable_result(
        config,
        root_name=root_name,
        skill_digest=snapshot.skill_digest,
        review_fingerprint=fingerprint,
    )
    if reusable is not None:
        record, approved = reusable
        findings = approved.get("findings") if isinstance(approved.get("findings"), list) else []
        approved_scans = approved.get("static_reports") if isinstance(approved.get("static_reports"), list) else []
        approved_ai = approved.get("ai_review") if isinstance(approved.get("ai_review"), Mapping) else {}
        approved_security = approved_ai.get("security_review") if isinstance(approved_ai.get("security_review"), Mapping) else {}
        result = {
            **source,
            "schema_version": "1.0",
            "review_status": "COMPLETED",
            "security_decision": "PASS",
            "quality_decision": "PASS",
            "quality_score": approved.get("quality_score"),
            "static_reports": [_scan_summary(item) for item in approved_scans if isinstance(item, Mapping)],
            "ai_review_summary": {
                "status": "RESULT_REUSED",
                "security_verdict": approved_security.get("verdict"),
                "max_severity": approved_security.get("max_severity"),
            },
            **_finding_summary(findings),
            "reuse_status": REUSE_STATUS,
            "reused_from_skill_id": (record.get("source") or {}).get("skill_id") if isinstance(record.get("source"), Mapping) else None,
            "reused_from_task_id": record.get("source_task_id"),
            "reused_from_batch_id": record.get("source_batch_id"),
            "reused_from_evidence_ref": record.get("source_evidence_ref"),
            "comparison_method": COMPARE_METHOD,
            "timestamp_ignored": True,
            "reuse_reason": "Skill Root 名称相同，且规范化包内容摘要完全一致；时间戳不参与比较。",
            "review_policy_version": config.ai.policy_version,
            "reviewed_at": _utc_now(),
            "reused_from_reviewed_at": approved.get("reviewed_at") or approved_ai.get("reviewed_at"),
            "evidence_ref": str(evidence.task_root),
            "failure_reason": "",
        }
        evidence.write_json("result-reuse.json", result)
        evidence.write_json("final-result.json", result)
        _durable_result(config, source, result)
        _atomic_json(index_path, {**base_index, "status": REUSE_STATUS, "result": result})
        return SkillPreparation(task_id, _skill_id(row), snapshot, index_path, None, result, downloaded.task_root)
    scans = _run_static_scans(
        active_adapters,
        snapshot=snapshot,
        work_root=downloaded.task_root / "scanner-work",
        evidence=evidence,
    )
    if any(not scan.completed or not scan.tool_ok for scan in scans):
        policy = evaluate_policy(scans, None, skill_digest=snapshot.skill_digest)
        result = {
            **source,
            "schema_version": "1.0",
            "review_status": "INCOMPLETE",
            "security_decision": policy.security_decision,
            "quality_decision": "INCOMPLETE",
            "quality_score": None,
            "static_reports": [_scan_summary(scan.to_dict()) for scan in scans],
            **_finding_summary(policy.findings),
            "failure_reason": "; ".join(policy.reasons),
            "evidence_ref": str(evidence.task_root),
        }
        evidence.write_json("final-result.json", result)
        _durable_result(config, source, result)
        _atomic_json(index_path, {**base_index, "status": "INCOMPLETE", "result": result})
        return SkillPreparation(task_id, _skill_id(row), snapshot, index_path, None, result, downloaded.task_root)
    assigned = _utc_now()
    handoff = build_ai_review_handoff(
        snapshot=snapshot,
        scans=scans,
        source=AIReviewSourceMetadata(
            row.skill_name,
            row.repo_name,
            row.branch,
            row.skill_path,
            row.inventory_revision,
        ),
        review_id=task_id,
        policy_version=config.ai.policy_version,
        assigned_reviewed_at=assigned,
        reviewer_model=config.ai.reviewer_model,
        result_schema_path=config.ai.result_schema_path,
    )
    handoff_ref = evidence.write_json("ai/handoff.json", handoff)
    scan_paths = {
        name: str(evidence.task_root / "scanners" / name / "normalized-result.json")
        for name in ("cisco", "skillspector")
    }
    _atomic_json(
        index_path,
        {
            **base_index,
            "status": "WAITING_FOR_AI",
            "handoff_path": str(handoff_ref.path),
            "static_result_paths": scan_paths,
        },
    )
    return SkillPreparation(task_id, _skill_id(row), snapshot, index_path, handoff_ref.path, None, downloaded.task_root)


def finalize_skill(
    config: ReviewConfig,
    *,
    index_path: Path,
    ai_result_path: Path,
) -> Mapping[str, Any]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("status") != "WAITING_FOR_AI":
        raise PerSkillError("Skill is not waiting for an AI result")
    source = index.get("source")
    snapshot_data = index.get("snapshot")
    if not isinstance(source, Mapping) or not isinstance(snapshot_data, Mapping):
        raise PerSkillError("Skill index is malformed")
    from .orchestrator import _snapshot_from_task

    snapshot = _snapshot_from_task(index)
    _verify_archived_snapshot(snapshot)
    review = load_and_validate_ai_review_result(
        ai_result_path,
        schema_path=config.ai.result_schema_path,
        expectation=AIReviewExpectation(
            snapshot.skill_digest,
            snapshot.source_revision,
            review_id=str(index["task_id"]),
            policy_version=config.ai.policy_version,
        ),
    )
    evidence = EvidenceStore(
        config.workspace.evidence_root,
        str(index["batch_id"]),
        str(index["task_id"]),
        candidate_root=config.workspace.candidate_root,
    )
    scans: list[Mapping[str, Any]] = []
    for name in ("cisco", "skillspector"):
        expected = safe_join(evidence.task_root, f"scanners/{name}/normalized-result.json")
        scans.append(json.loads(expected.read_text(encoding="utf-8")))
    policy = evaluate_policy(
        scans,
        review,
        skill_digest=snapshot.skill_digest,
        source_selection_status="SELECTED",
        quality_threshold=config.quality.candidate_threshold,
    )
    findings = list(policy.findings)
    result = {
        **dict(source),
        "schema_version": "1.0",
        "review_status": "COMPLETED",
        "security_decision": policy.security_decision,
        "quality_decision": policy.quality_decision,
        "quality_score": policy.quality_score,
        "static_reports": [_scan_summary(scan) for scan in scans],
        "ai_review_summary": {
            "status": "COMPLETED",
            "security_verdict": (review.get("security_review") or {}).get("verdict"),
            "max_severity": (review.get("security_review") or {}).get("max_severity"),
            "quality_verdict": (review.get("quality_review") or {}).get("verdict"),
        },
        **_finding_summary(findings),
        "reuse_status": "",
        "review_policy_version": config.ai.policy_version,
        "reviewed_at": review.get("reviewed_at"),
        "evidence_ref": str(evidence.task_root),
        "failure_reason": "; ".join(policy.blocking_reasons + policy.incomplete_reasons),
    }
    evidence.write_json("ai/imported-result.json", review)
    evidence.write_json(
        "final-result.json",
        {
            **result,
            "status": "COMPLETED",
            "candidate_eligible": policy.candidate_eligible,
            "review_fingerprint": index["review_fingerprint"],
            "subject": review.get("subject"),
            "ai_review": review,
        },
    )
    result_path = _durable_result(config, source, result)
    final_evidence = json.loads((evidence.task_root / "final-result.json").read_text(encoding="utf-8"))
    publish_reusable_result(
        config,
        batch_id=str(index["batch_id"]),
        task_id=str(index["task_id"]),
        root_name=str(source["skill_name"]),
        skill_digest=snapshot.skill_digest,
        review_fingerprint=index["review_fingerprint"],
        source_evidence_ref=str(evidence.task_root),
        source=source,
        final_result=final_evidence,
    )
    _atomic_json(index_path, {**index, "status": "COMPLETED", "result_path": str(result_path)})
    return result


RESULT_COLUMNS = (
    "review_status",
    "security_decision",
    "quality_decision",
    "quality_score",
    "max_severity",
    "finding_count",
    "critical_count",
    "high_count",
    "medium_count",
    "low_count",
    "info_count",
    "content_id",
    "reuse_status",
    "reused_from_skill_id",
    "review_result_path",
    "evidence_ref",
    "review_policy_version",
    "reviewed_at",
    "failure_reason",
)


def write_skill_result_tables(
    config: ReviewConfig,
    inventory: InventoryDocument,
    *,
    batch_id: str,
) -> tuple[Path, Path]:
    """Atomically rebuild CSV and JSON from durable per-skill results."""

    results: dict[str, Mapping[str, Any]] = {}
    for row in inventory.rows:
        identifier = row.trace_values.get("skill_id", "")
        path = config.workspace.skills_root / identifier / "review-result.json"
        if identifier and path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, Mapping) and value.get("source_row_id") == row.source_row_id:
                results[row.source_row_id] = value
    output_root = config.workspace.results_root / batch_id
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = _atomic_json(
        output_root / "skill-review-results.json",
        {
            "schema_version": "1.0",
            "batch_id": batch_id,
            "inventory_csv_sha256": inventory.raw_csv_sha256,
            "inventory_csv_encoding": inventory.source_encoding,
            "result_count": len(results),
            "skills": [dict(results[row.source_row_id]) for row in inventory.rows if row.source_row_id in results],
        },
    )
    fields = tuple(inventory.headers) + RESULT_COLUMNS
    descriptor, name = tempfile.mkstemp(prefix=".results-", suffix=".csv.tmp", dir=str(output_root))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in inventory.rows:
                original = dict(row.raw)
                result = results.get(row.source_row_id, {})
                if result.get("review_status") == "COMPLETED":
                    original["security_reviewed"] = "是"
                counts = result.get("finding_counts") if isinstance(result.get("finding_counts"), Mapping) else {}
                severities = [name for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO") if counts.get(name, 0)]
                default_status = (
                    "PENDING"
                    if row.status in config.batch.included_statuses
                    else "SKIPPED_STATUS"
                )
                output = {
                    **original,
                    "review_status": result.get("review_status", default_status),
                    "security_decision": result.get("security_decision", ""),
                    "quality_decision": result.get("quality_decision", ""),
                    "quality_score": result.get("quality_score", ""),
                    "max_severity": severities[0] if severities else "NONE",
                    "finding_count": sum(int(value) for value in counts.values()),
                    "critical_count": counts.get("CRITICAL", 0),
                    "high_count": counts.get("HIGH", 0),
                    "medium_count": counts.get("MEDIUM", 0),
                    "low_count": counts.get("LOW", 0),
                    "info_count": counts.get("INFO", 0),
                    "content_id": result.get("content_id", ""),
                    "reuse_status": result.get("reuse_status", ""),
                    "reused_from_skill_id": result.get("reused_from_skill_id", ""),
                    "review_result_path": str(config.workspace.skills_root / row.trace_values.get("skill_id", "") / "review-result.json") if result else "",
                    "evidence_ref": result.get("evidence_ref", ""),
                    "review_policy_version": result.get("review_policy_version", ""),
                    "reviewed_at": result.get("reviewed_at", ""),
                    "failure_reason": result.get("failure_reason", ""),
                }
                writer.writerow(output)
            handle.flush()
            os.fsync(handle.fileno())
        csv_path = output_root / "skill-review-results.csv"
        os.replace(temporary, csv_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return csv_path, json_path


def cleanup_skill_download(config: ReviewConfig, *, batch_id: str, task_id: str) -> bool:
    target = (config.workspace.git_download_root / batch_id / task_id).resolve()
    root = config.workspace.git_download_root.resolve()
    if target.parent.parent != root or not target.name.startswith("skill-"):
        raise PerSkillError("cleanup target escaped git_download_root")
    if not target.exists():
        return False
    if target.is_symlink() or not target.is_dir():
        raise PerSkillError("cleanup target is not a real task directory")
    remove_tree(target)
    return True


__all__ = [
    "PartialDownload",
    "PerSkillError",
    "RepositoryDownload",
    "SkillPreparation",
    "cleanup_repository_download",
    "cleanup_skill_download",
    "download_repository_skills",
    "finalize_skill",
    "partial_fetch_skill_repository",
    "prepare_skill",
    "skill_task_id",
    "write_skill_result_tables",
]
