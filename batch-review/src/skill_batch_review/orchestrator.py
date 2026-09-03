"""Repository-at-a-time orchestration for the batch review pipeline.

The orchestration deliberately has two operator-visible phases:

``prepare_repository``
    synchronizes one Gerrit mirror, freezes sources, exports immutable Skill
    snapshots, runs both approved static scanners against each selected
    snapshot, and writes a read-only Claude Code handoff.  It never invokes a
    model and never exports a candidate.

``finalize_repository``
    imports JSON files produced by the project AI review Skill, validates
    them against the frozen revision/digest and schema, applies the policy,
    and exports only explicitly eligible local private candidates.  It never
    commits, pushes, or publishes anything.

Keeping one repository open across these two phases makes the cleanup rule
honest: the repository workspace is removed only after all of its selected
tasks have a durable final result (or an explicit retained failure).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

from .ai_review import (
    AIReviewExpectation,
    AIReviewSourceMetadata,
    build_ai_review_handoff,
    load_and_validate_ai_review_result,
)
from .artifacts import EvidenceStore, export_private_candidate, safe_join
from .config import ReviewConfig
from .git_source import (
    BRANCH_CONTENT_CONFLICT,
    SELECTED,
    SKIPPED_SUPERSEDED_BRANCH,
    GitMirror,
    GitRunner,
    GitSourceResolver,
    ResolvedSource,
)
from .inventory import InventoryDocument, InventoryRow
from .review_policy import PolicyResult, evaluate_policy
from .result_reuse import (
    COMPARE_METHOD,
    REUSE_STATUS,
    build_review_fingerprint,
    find_reusable_result,
    publish_reusable_result,
    skill_root_name,
)
from .scanners import CiscoSkillScannerAdapter, ScannerAdapter, SkillSpectorAdapter
from .snapshot import PackageEntry, SnapshotResult, export_skill_snapshot


class OrchestrationError(RuntimeError):
    """A repository cannot safely advance to the requested stage."""


@dataclass(frozen=True, slots=True)
class RepositoryPlan:
    repository: str
    included_rows: tuple[InventoryRow, ...]
    excluded_rows: tuple[InventoryRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "included_row_count": len(self.included_rows),
            "excluded_row_count": len(self.excluded_rows),
            "included_rows": [row.to_dict() for row in self.included_rows],
            "excluded_rows": [row.to_dict() for row in self.excluded_rows],
        }


@dataclass(frozen=True, slots=True)
class PreparedTask:
    task_id: str
    source: ResolvedSource
    snapshot: SnapshotResult
    scans: tuple[Any, ...]
    handoff_path: Path
    evidence_root: Path

    def to_dict(self) -> dict[str, Any]:
        scan_paths = {
            name: str(self.evidence_root / "scanners" / name / "normalized-result.json")
            for name in ("cisco", "skillspector")
        }
        return {
            "task_id": self.task_id,
            "source": self.source.to_dict(),
            "snapshot": self.snapshot.manifest_dict(),
            "snapshot_path": str(self.snapshot.snapshot_path),
            # Full normalized findings remain in the restricted evidence
            # area.  The general manifest stores references only.
            "static_result_paths": scan_paths,
            "handoff_path": str(self.handoff_path),
            "evidence_root": str(self.evidence_root),
            "status": "WAITING_FOR_AI_REVIEW",
        }


@dataclass(frozen=True, slots=True)
class ReusedTask:
    task_id: str
    source: ResolvedSource
    snapshot: SnapshotResult
    reuse_record: Mapping[str, Any]
    evidence_root: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source.to_dict(),
            "snapshot": self.snapshot.manifest_dict(),
            "snapshot_path": str(self.snapshot.snapshot_path),
            "reuse_record": dict(self.reuse_record),
            "evidence_root": str(self.evidence_root),
            "status": REUSE_STATUS,
        }


@dataclass(frozen=True, slots=True)
class RepositoryPreparation:
    repository: str
    mirror_path: Path
    source_records: tuple[ResolvedSource, ...]
    tasks: tuple[PreparedTask, ...]
    reused_tasks: tuple[ReusedTask, ...]
    conflicts: tuple[Mapping[str, Any], ...]
    pre_ai_results: tuple[Mapping[str, Any], ...]
    review_fingerprint: Mapping[str, Any]
    index_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "repository": self.repository,
            "mirror_path": str(self.mirror_path),
            "source_records": [record.to_dict() for record in self.source_records],
            "tasks": [task.to_dict() for task in self.tasks],
            "reused_tasks": [task.to_dict() for task in self.reused_tasks],
            "conflicts": [dict(conflict) for conflict in self.conflicts],
            "pre_ai_results": [dict(result) for result in self.pre_ai_results],
            "review_fingerprint": dict(self.review_fingerprint),
            "status": "WAITING_FOR_AI_REVIEW" if self.tasks else "NO_AI_TASKS",
        }


def _identifier(*values: str, prefix: str = "task") -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _repo_workspace_name(repository: str) -> str:
    readable = repository.replace("/", "-")
    readable = "".join(char if char.isalnum() or char in "._-" else "-" for char in readable)
    return f"{readable[:80]}-{hashlib.sha256(repository.encode('utf-8')).hexdigest()[:12]}"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> Path:
    """Write a deterministic JSON document; identical reruns are idempotent."""

    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise OrchestrationError(f"refusing to replace different orchestration record: {path}")
        return path
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
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


def plan_repositories(
    inventory: InventoryDocument,
    *,
    included_statuses: Sequence[str],
) -> tuple[RepositoryPlan, ...]:
    """Group only explicitly included lifecycle states by repository."""

    allowed = {str(value).strip().upper() for value in included_statuses if str(value).strip()}
    if not allowed:
        raise OrchestrationError("included_statuses must not be empty")
    repositories = sorted({row.repo_name for row in inventory.rows})
    plans: list[RepositoryPlan] = []
    for repository in repositories:
        rows = tuple(row for row in inventory.rows if row.repo_name == repository)
        included = tuple(row for row in rows if row.status.upper() in allowed)
        excluded = tuple(row for row in rows if row.status.upper() not in allowed)
        plans.append(RepositoryPlan(repository, included, excluded))
    return tuple(plans)


def _default_adapters(config: ReviewConfig) -> dict[str, ScannerAdapter]:
    cisco = config.scanner("cisco")
    spector = config.scanner("skillspector")
    if not cisco.enabled or not spector.enabled:
        raise OrchestrationError("both static scanners must be enabled for the baseline review")
    return {
        "cisco": CiscoSkillScannerAdapter(
            executable=cisco.executable,
            timeout_seconds=cisco.timeout_seconds,
            tool_version=cisco.version,
        ),
        "skillspector": SkillSpectorAdapter(
            executable=spector.executable,
            timeout_seconds=spector.timeout_seconds,
            tool_version=spector.version,
        ),
    }


def _run_static_scans(
    adapters: Mapping[str, ScannerAdapter],
    *,
    snapshot: SnapshotResult,
    work_root: Path,
    evidence: EvidenceStore,
) -> tuple[Any, ...]:
    if set(adapters) != {"cisco", "skillspector"}:
        raise OrchestrationError("exactly Cisco and SkillSpector adapters are required")

    def run_one(name: str) -> Any:
        attempt_root = work_root / "scanner-output" / name
        attempt_root.mkdir(parents=True, exist_ok=True)
        report_path = attempt_root / "attempt-1.json"
        result = adapters[name].scan(
            snapshot.snapshot_path,
            output_file=report_path,
            skill_digest=snapshot.skill_digest,
        )
        if result.raw_report_path:
            report_ref = evidence.copy_raw_report(
                result.raw_report_path,
                f"scanners/{name}/raw-report.json",
            )
            # The preserved evidence path, not the disposable scanner output,
            # is what the model handoff and audit record must reference.
            result = replace(result, raw_report_path=str(report_ref.path))
        evidence.write_json(f"scanners/{name}/normalized-result.json", result.to_dict())
        return result

    # Both tools read the same immutable snapshot and write separate outputs.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="skill-static") as executor:
        futures = {name: executor.submit(run_one, name) for name in sorted(adapters)}
        results = {name: future.result() for name, future in futures.items()}
    return (results["cisco"], results["skillspector"])


def _resolve_equal_time_conflicts(
    records: Sequence[ResolvedSource],
    snapshots: Mapping[str, SnapshotResult],
) -> tuple[tuple[ResolvedSource, ...], tuple[Mapping[str, Any], ...]]:
    """Auto-collapse equal-time branches only when their package digest matches."""

    resolved = list(records)
    conflicts: list[Mapping[str, Any]] = []
    indexes_by_target: dict[tuple[str, str], list[int]] = {}
    for index, record in enumerate(resolved):
        if record.source_selection_status == BRANCH_CONTENT_CONFLICT:
            key = (record.row.repo_name, record.row.skill_path)
            indexes_by_target.setdefault(key, []).append(index)
    for key, indexes in indexes_by_target.items():
        digest_by_index = {
            index: snapshots[resolved[index].row.source_row_id].skill_digest for index in indexes
        }
        unique = set(digest_by_index.values())
        if len(unique) == 1:
            winner = min(indexes, key=lambda index: (resolved[index].row.branch, resolved[index].row.source_row_id))
            for index in indexes:
                current = resolved[index]
                if index == winner:
                    resolved[index] = replace(
                        current,
                        source_selection_status=SELECTED,
                        branch_content_conflict=False,
                        awaiting_snapshot=False,
                        reasons=current.reasons + ("equal-time branch packages have the same digest",),
                    )
                else:
                    resolved[index] = replace(
                        current,
                        source_selection_status=SKIPPED_SUPERSEDED_BRANCH,
                        branch_content_conflict=False,
                        awaiting_snapshot=False,
                        reasons=current.reasons + ("equal-time duplicate content was collapsed by digest",),
                    )
        else:
            conflicts.append(
                {
                    "repository": key[0],
                    "skill_path": key[1],
                    "status": BRANCH_CONTENT_CONFLICT,
                    "candidates": [
                        {
                            "source_row_id": resolved[index].row.source_row_id,
                            "branch": resolved[index].row.branch,
                            "source_revision": resolved[index].source_revision,
                            "skill_digest": digest_by_index[index],
                        }
                        for index in indexes
                    ],
                }
            )
    return tuple(resolved), tuple(conflicts)


def _pre_ai_result(
    record: ResolvedSource,
    *,
    security_decision: str,
    reason: str,
    task_id: str | None = None,
    snapshot: SnapshotResult | None = None,
    scans: Sequence[Any] = (),
) -> dict[str, Any]:
    status = "WAITING_FOR_MANUAL_REVIEW" if security_decision == "REVIEW_REQUIRED" else "INCOMPLETE"
    return {
        "schema_version": "0.1",
        "task_id": task_id,
        "source_row_id": record.row.source_row_id,
        "source_row_numbers": list(record.row.source_row_numbers),
        "repo_name": record.row.repo_name,
        "skill_name": record.row.skill_name,
        "branch": record.row.branch,
        "skill_path": record.row.skill_path,
        "inventory_revision": record.row.inventory_revision,
        "source_revision": record.source_revision,
        "skill_last_change_revision": record.skill_last_change_revision,
        "skill_digest": snapshot.skill_digest if snapshot else None,
        "source_selection_status": record.source_selection_status,
        "static_reports": [
            {
                "scanner": scan.scanner,
                "status": scan.status,
                "decision": scan.decision,
                "tool_ok": scan.tool_ok,
                "report_complete": scan.report_complete,
                "skill_digest": scan.skill_digest,
            }
            for scan in scans
        ],
        "security_decision": security_decision,
        "quality_decision": "INCOMPLETE",
        "quality_score": None,
        "candidate_status": "NOT_ELIGIBLE",
        "manual_reason": reason if security_decision == "REVIEW_REQUIRED" else "",
        "failure_reason": reason if security_decision == "INCOMPLETE" else "",
        "status": status,
    }


MirrorSync = Callable[[str, Path], Path]


def prepare_repository(
    config: ReviewConfig,
    *,
    batch_id: str,
    repository: str,
    rows: Sequence[InventoryRow],
    mirror_sync: MirrorSync | None = None,
    resolver_factory: Callable[[Path], GitSourceResolver] = GitSourceResolver,
    snapshot_exporter: Callable[..., SnapshotResult] = export_skill_snapshot,
    adapters: Mapping[str, ScannerAdapter] | None = None,
) -> RepositoryPreparation:
    """Prepare all selected Skills from one repository and stop before AI."""

    if not rows or any(row.repo_name != repository for row in rows):
        raise OrchestrationError("prepare_repository rows must belong to exactly one repository")
    if any(row.status.upper() not in set(config.batch.included_statuses) for row in rows):
        raise OrchestrationError("prepare_repository received a row outside included_statuses")

    repo_root = config.workspace.root / batch_id / "repositories" / _repo_workspace_name(repository)
    mirror_path = repo_root / "mirror.git"
    if mirror_sync is None:
        url = config.gerrit.repository_url(repository)
        runner = None
        if config.gerrit.ssh_identity_file is not None:
            environment = os.environ.copy()
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
            runner = GitRunner(env=environment)
        GitMirror(url, mirror_path, runner=runner or GitRunner()).clone_or_fetch()
    else:
        returned = Path(mirror_sync(repository, mirror_path)).resolve()
        if returned != mirror_path.resolve():
            raise OrchestrationError("mirror_sync returned an unexpected mirror path")
    resolver = resolver_factory(mirror_path)
    source_result = resolver.resolve_sources(rows)

    snapshots: dict[str, SnapshotResult] = {}
    for record in source_result.records:
        if not record.selected and not record.branch_content_conflict:
            continue
        if not record.source_revision:
            raise OrchestrationError("selected source has no frozen revision")
        destination = repo_root / "snapshots" / record.row.source_row_id
        snapshots[record.row.source_row_id] = snapshot_exporter(
            mirror_path,
            record.source_revision,
            record.row.skill_path,
            destination,
        )

    resolved_records, conflicts = _resolve_equal_time_conflicts(source_result.records, snapshots)
    active_adapters = dict(adapters or _default_adapters(config))
    review_fingerprint = build_review_fingerprint(config, active_adapters)
    pre_ai_results: list[Mapping[str, Any]] = []
    conflict_rows = {
        candidate["source_row_id"]
        for conflict in conflicts
        for candidate in conflict.get("candidates", [])
        if isinstance(candidate, Mapping) and isinstance(candidate.get("source_row_id"), str)
    }
    for record in resolved_records:
        if record.row.source_row_id in conflict_rows:
            pre_ai_results.append(
                _pre_ai_result(
                    record,
                    security_decision="REVIEW_REQUIRED",
                    reason="equal-time branch variants have different content digests",
                    snapshot=snapshots.get(record.row.source_row_id),
                )
            )
        elif record.source_selection_status in {
            "STALE_INVENTORY",
            "INPUT_INVALID",
            "INPUT_CONFLICT",
            "SOURCE_UNAVAILABLE",
        }:
            pre_ai_results.append(
                _pre_ai_result(
                    record,
                    security_decision="INCOMPLETE",
                    reason="; ".join(record.reasons) or record.error or "source resolution failed",
                )
            )
    tasks: list[PreparedTask] = []
    reused_tasks: list[ReusedTask] = []
    for record in resolved_records:
        if not record.selected:
            continue
        snapshot = snapshots[record.row.source_row_id]
        task_id = _identifier(
            repository,
            record.row.skill_path,
            snapshot.source_revision,
            snapshot.skill_digest,
        )
        evidence = EvidenceStore(
            config.workspace.evidence_root,
            batch_id,
            task_id,
            candidate_root=config.workspace.candidate_root,
        )
        manifest_ref = evidence.write_json("package-manifest.json", snapshot.manifest_dict())
        evidence.write_json("source-resolution.json", record.to_dict())
        if not snapshot.coverage_complete:
            pre_result = _pre_ai_result(
                record,
                security_decision="INCOMPLETE",
                reason="snapshot coverage is incomplete: "
                + "; ".join(issue.code for issue in snapshot.blocking_issues),
                task_id=task_id,
                snapshot=snapshot,
            )
            evidence.write_json("final-result.json", pre_result)
            pre_ai_results.append(pre_result)
            continue
        root_name = skill_root_name(record.row.skill_path, record.row.skill_name)
        reusable = find_reusable_result(
            config,
            root_name=root_name,
            skill_digest=snapshot.skill_digest,
            review_fingerprint=review_fingerprint,
        )
        if reusable is not None:
            reuse_record, _ = reusable
            reused_tasks.append(
                ReusedTask(task_id, record, snapshot, reuse_record, evidence.task_root)
            )
            continue
        scans = _run_static_scans(
            active_adapters,
            snapshot=snapshot,
            work_root=repo_root / "tasks" / task_id,
            evidence=evidence,
        )
        if any(not scan.completed or not scan.tool_ok for scan in scans):
            policy = evaluate_policy(
                scans,
                None,
                skill_digest=snapshot.skill_digest,
                source_selection_status=record.source_selection_status,
                quality_threshold=config.quality.candidate_threshold,
            )
            pre_result = _pre_ai_result(
                record,
                security_decision=policy.security_decision,
                reason="; ".join(policy.reasons) or "static review is incomplete",
                task_id=task_id,
                snapshot=snapshot,
                scans=scans,
            )
            evidence.write_json("final-result.json", pre_result)
            pre_ai_results.append(pre_result)
            continue
        handoff = build_ai_review_handoff(
            snapshot=snapshot,
            scans=scans,
            source=AIReviewSourceMetadata(
                record.row.skill_name,
                repository,
                record.row.branch,
                record.row.skill_path,
                record.row.inventory_revision,
            ),
            review_id=task_id,
            policy_version=config.ai.policy_version,
            assigned_reviewed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            reviewer_model=config.ai.reviewer_model,
            manifest_path=manifest_ref.path,
            result_schema_path=config.ai.result_schema_path,
            result_output_path=safe_join(evidence.task_root, "ai/imported-result.json"),
        )
        handoff_ref = evidence.write_json("ai/handoff.json", handoff)
        tasks.append(
            PreparedTask(
                task_id,
                record,
                snapshot,
                scans,
                handoff_ref.path,
                evidence.task_root,
            )
        )

    index_path = config.workspace.manifest_root / batch_id / "repositories" / (
        _repo_workspace_name(repository) + ".json"
    )
    preparation = RepositoryPreparation(
        repository,
        mirror_path,
        resolved_records,
        tuple(tasks),
        tuple(reused_tasks),
        conflicts,
        tuple(pre_ai_results),
        review_fingerprint,
        index_path,
    )
    _atomic_json(index_path, preparation.to_dict())
    return preparation


def _snapshot_from_task(task: Mapping[str, Any]) -> SimpleNamespace:
    snapshot = task.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise OrchestrationError("repository index task has no snapshot manifest")
    entries = tuple(
        PackageEntry(
            relative_path=item["relative_path"],
            file_type=item["type"],
            mode=item["mode"],
            size=item["size"],
            sha256=item.get("sha256"),
            symlink_target=item.get("symlink_target"),
            git_object_id=item.get("git_object_id"),
        )
        for item in snapshot.get("entries", [])
    )
    return SimpleNamespace(
        repository=snapshot["repository"],
        source_revision=snapshot["source_revision"],
        skill_path=snapshot["skill_path"],
        skill_digest=snapshot["skill_digest"],
        snapshot_path=Path(task["snapshot_path"]),
        entries=entries,
        coverage_complete=bool(snapshot.get("coverage_complete")),
    )


def _scan_summary(scan: Mapping[str, Any]) -> dict[str, Any]:
    order = {"UNKNOWN": 0, "INFO": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4, "CRITICAL": 5}
    severities = [
        str(finding.get("severity") or "UNKNOWN").upper()
        for finding in scan.get("findings", [])
        if isinstance(finding, Mapping)
    ]
    maximum = max(severities, key=lambda value: order.get(value, 0), default="NONE")
    return {
        "scanner": scan.get("scanner"),
        "status": scan.get("status"),
        "decision": scan.get("decision"),
        "tool_ok": scan.get("tool_ok"),
        "report_complete": scan.get("report_complete"),
        "skill_digest": scan.get("skill_digest"),
        "max_severity": maximum,
    }


def _result_summary(
    *,
    task_id: str,
    source: Mapping[str, Any],
    review: Mapping[str, Any],
    scans: Sequence[Mapping[str, Any]],
    policy: PolicyResult,
    candidate: Mapping[str, Any] | None,
    evidence_ref: str,
) -> dict[str, Any]:
    subject = review.get("subject") if isinstance(review.get("subject"), Mapping) else {}
    security = (
        review.get("security_review")
        if isinstance(review.get("security_review"), Mapping)
        else {}
    )
    quality = review.get("quality_review") if isinstance(review.get("quality_review"), Mapping) else {}
    return {
        "schema_version": "0.1",
        "task_id": task_id,
        "source_row_id": source.get("source_row_id"),
        "source_row_numbers": source.get("source_row_numbers", []),
        "subject": dict(subject),
        "source_selection_status": source.get("source_selection_status"),
        "skill_last_change_revision": source.get("skill_last_change_revision"),
        "static_reports": [_scan_summary(scan) for scan in scans],
        "ai_review": {
            "status": "COMPLETED",
            "security_review": {
                "verdict": security.get("verdict"),
                "max_severity": security.get("max_severity"),
            },
        },
        "security_decision": policy.security_decision,
        "quality_decision": policy.quality_decision,
        "quality_score": policy.quality_score,
        "quality_review": {
            "score": quality.get("score"),
            "verdict": quality.get("verdict"),
            "dimensions": quality.get("dimensions", []),
        },
        "candidate_status": "EXPORTED_LOCAL" if candidate else "NOT_ELIGIBLE",
        "candidate": dict(candidate) if candidate else None,
        "manual_reason": "; ".join(policy.review_reasons),
        "failure_reason": "; ".join(policy.blocking_reasons + policy.incomplete_reasons),
        "review_policy_version": review.get("policy_version"),
        "reviewed_at": review.get("reviewed_at"),
        "evidence_ref": evidence_ref,
        "status": "COMPLETED",
    }


def _current_subject(source: Mapping[str, Any], snapshot: Any) -> dict[str, Any]:
    source_key = source.get("source_key")
    if not isinstance(source_key, Mapping):
        raise OrchestrationError("reused task has no source identity")
    return {
        "skill_name": source_key.get("skill_name"),
        "repo_name": source_key.get("repository"),
        "branch": source_key.get("branch"),
        "skill_path": source_key.get("skill_path"),
        "inventory_revision": source.get("inventory_revision"),
        "source_revision": snapshot.source_revision,
        "skill_digest_sha256": snapshot.skill_digest,
    }


def _finalize_reused_task(
    config: ReviewConfig,
    *,
    batch_id: str,
    task: Mapping[str, Any],
    review_fingerprint: Mapping[str, Any],
) -> Mapping[str, Any]:
    task_id = task.get("task_id")
    source = task.get("source")
    reuse_record = task.get("reuse_record")
    if not isinstance(task_id, str) or not isinstance(source, Mapping) or not isinstance(reuse_record, Mapping):
        raise OrchestrationError("repository index contains a malformed reused task")
    snapshot = _snapshot_from_task(task)
    source_key = source.get("source_key")
    if not isinstance(source_key, Mapping):
        raise OrchestrationError(f"reused task {task_id} has no source identity")
    root_name = skill_root_name(str(source_key.get("skill_path") or ""), str(source_key.get("skill_name") or ""))
    reusable = find_reusable_result(
        config,
        root_name=root_name,
        skill_digest=snapshot.skill_digest,
        review_fingerprint=review_fingerprint,
    )
    if reusable is None or reusable[0].get("source_task_id") != reuse_record.get("source_task_id"):
        raise OrchestrationError(f"approved reuse source is no longer valid for task {task_id}")
    approved_record, approved = reusable
    evidence = EvidenceStore(
        config.workspace.evidence_root,
        batch_id,
        task_id,
        candidate_root=config.workspace.candidate_root,
    )
    notice = {
        "reuse_status": REUSE_STATUS,
        "comparison_method": COMPARE_METHOD,
        "timestamp_ignored": True,
        "skill_root_name": root_name,
        "skill_digest": snapshot.skill_digest,
        "review_fingerprint": dict(review_fingerprint),
        "reused_from_batch_id": approved_record.get("source_batch_id"),
        "reused_from_task_id": approved_record.get("source_task_id"),
        "reused_from_source": approved_record.get("source"),
        "reused_from_evidence_ref": approved_record.get("source_evidence_ref"),
        "reason": "Skill Root 名称相同，且规范化包内容摘要完全一致；文件时间戳不参与比较。",
    }
    notice_ref = evidence.write_json("result-reuse.json", notice)
    quality_score = approved.get("quality_score")
    approved_ai = approved.get("ai_review") if isinstance(approved.get("ai_review"), Mapping) else {}
    approved_quality = (
        approved_ai.get("quality_review")
        if isinstance(approved_ai.get("quality_review"), Mapping)
        else {}
    )
    candidate = export_private_candidate(
        snapshot,
        candidate_root=config.workspace.candidate_root,
        repository=str(source_key["repository"]),
        skill_path=str(source_key["skill_path"]),
        source_revision=snapshot.source_revision,
        skill_digest=snapshot.skill_digest,
        eligible=True,
        branch=str(source_key["branch"]),
        skill_name=str(source_key["skill_name"]),
        security_decision="PASS",
        quality_score=int(quality_score),
        evidence_ref=str(notice_ref.path),
        evidence_root=config.workspace.evidence_root,
    ).to_dict()
    subject = _current_subject(source, snapshot)
    final_evidence = {
        "schema_version": "0.1",
        "task_id": task_id,
        "source": dict(source),
        "subject": subject,
        "static_reports": approved.get("static_reports", []),
        "ai_review": approved_ai,
        "security_decision": "PASS",
        "quality_decision": "PASS",
        "quality_score": quality_score,
        "quality_review": approved_quality,
        "candidate_eligible": True,
        "candidate_status": "EXPORTED_LOCAL",
        "candidate": candidate,
        "findings": approved.get("findings", []),
        "review_policy_version": approved.get("review_policy_version") or approved_ai.get("policy_version"),
        "reviewed_at": approved.get("reviewed_at") or approved_ai.get("reviewed_at"),
        "review_fingerprint": dict(review_fingerprint),
        **notice,
        "evidence_ref": str(evidence.task_root),
        "status": "COMPLETED",
    }
    evidence.write_json("final-result.json", final_evidence)
    scans = approved.get("static_reports") if isinstance(approved.get("static_reports"), list) else []
    security = approved_ai.get("security_review") if isinstance(approved_ai.get("security_review"), Mapping) else {}
    return {
        "schema_version": "0.1",
        "task_id": task_id,
        "source_row_id": source.get("source_row_id"),
        "subject": subject,
        "source_selection_status": source.get("source_selection_status"),
        "skill_last_change_revision": source.get("skill_last_change_revision"),
        "static_reports": [_scan_summary(item) for item in scans if isinstance(item, Mapping)],
        "ai_review": {"status": "COMPLETED", "security_review": {"verdict": security.get("verdict"), "max_severity": security.get("max_severity")}},
        "security_decision": "PASS",
        "quality_decision": "PASS",
        "quality_score": quality_score,
        "quality_review": {"score": approved_quality.get("score"), "verdict": approved_quality.get("verdict"), "dimensions": approved_quality.get("dimensions", [])},
        "candidate_status": "EXPORTED_LOCAL",
        "candidate": candidate,
        "review_policy_version": final_evidence.get("review_policy_version"),
        "reviewed_at": final_evidence.get("reviewed_at"),
        **notice,
        "evidence_ref": str(evidence.task_root),
        "status": "COMPLETED",
    }


def finalize_repository(
    config: ReviewConfig,
    *,
    batch_id: str,
    repository_index: Path,
    ai_results_dir: Path,
) -> tuple[Mapping[str, Any], ...]:
    """Validate imported AI JSON, apply policy, and export eligible candidates."""

    try:
        index = json.loads(Path(repository_index).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"cannot read repository index: {exc}") from exc
    if index.get("repository") is None or not isinstance(index.get("tasks"), list):
        raise OrchestrationError("repository index is malformed")
    reused_tasks = index.get("reused_tasks", [])
    review_fingerprint = index.get("review_fingerprint")
    if not isinstance(reused_tasks, list) or not isinstance(review_fingerprint, Mapping):
        raise OrchestrationError("repository index reuse metadata is malformed")
    pre_ai_results = index.get("pre_ai_results", [])
    if not isinstance(pre_ai_results, list) or any(
        not isinstance(item, Mapping) for item in pre_ai_results
    ):
        raise OrchestrationError("repository index pre_ai_results are malformed")
    results: list[Mapping[str, Any]] = [dict(item) for item in pre_ai_results]
    for task in reused_tasks:
        if not isinstance(task, Mapping):
            raise OrchestrationError("repository index contains a malformed reused task")
        results.append(
            _finalize_reused_task(
                config,
                batch_id=batch_id,
                task=task,
                review_fingerprint=review_fingerprint,
            )
        )
    for task in index["tasks"]:
        task_id = task.get("task_id")
        if not isinstance(task_id, str):
            raise OrchestrationError("repository index contains a task without task_id")
        result_path = Path(ai_results_dir) / f"{task_id}.json"
        snapshot = _snapshot_from_task(task)
        review = load_and_validate_ai_review_result(
            result_path,
            schema_path=config.ai.result_schema_path,
            expectation=AIReviewExpectation(
                snapshot.skill_digest,
                snapshot.source_revision,
                review_id=task_id,
                policy_version=config.ai.policy_version,
            ),
        )
        evidence = EvidenceStore(
            config.workspace.evidence_root,
            batch_id,
            task_id,
            candidate_root=config.workspace.candidate_root,
        )
        result_paths = task.get("static_result_paths")
        if not isinstance(result_paths, Mapping) or set(result_paths) != {"cisco", "skillspector"}:
            raise OrchestrationError(f"task {task_id} has no static scan result references")
        scans: list[Mapping[str, Any]] = []
        for scanner in ("cisco", "skillspector"):
            expected = safe_join(evidence.task_root, f"scanners/{scanner}/normalized-result.json")
            if Path(str(result_paths[scanner])).resolve() != expected.resolve():
                raise OrchestrationError(f"task {task_id} has an unexpected {scanner} result path")
            try:
                value = json.loads(expected.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise OrchestrationError(
                    f"cannot read restricted {scanner} result for {task_id}: {exc}"
                ) from exc
            if not isinstance(value, Mapping):
                raise OrchestrationError(f"restricted {scanner} result is not an object")
            scans.append(value)
        source = task.get("source")
        if not isinstance(source, Mapping):
            raise OrchestrationError(f"task {task_id} has no source record")
        policy: PolicyResult = evaluate_policy(
            scans,
            review,
            skill_digest=snapshot.skill_digest,
            source_selection_status=str(source.get("source_selection_status") or ""),
            stale_inventory=False,
            branch_content_conflict=bool(source.get("branch_content_conflict")),
            coverage_requires_review=bool(task["snapshot"].get("coverage_issues")),
            quality_threshold=config.quality.candidate_threshold,
        )
        imported = evidence.write_json("ai/imported-result.json", review)
        candidate = None
        source_key = source.get("source_key")
        if not isinstance(source_key, Mapping):
            raise OrchestrationError(f"task {task_id} has no source identity")
        if policy.candidate_eligible:
            candidate = export_private_candidate(
                snapshot,
                candidate_root=config.workspace.candidate_root,
                repository=str(source_key["repository"]),
                skill_path=str(source_key["skill_path"]),
                source_revision=snapshot.source_revision,
                skill_digest=snapshot.skill_digest,
                eligible=True,
                branch=str(source_key["branch"]),
                skill_name=str(source_key["skill_name"]),
                security_decision=policy.security_decision,
                quality_score=policy.quality_score,
                evidence_ref=str(imported.path),
                evidence_root=config.workspace.evidence_root,
            ).to_dict()
        final_evidence = {
            "schema_version": "0.1",
            "task_id": task_id,
            "source": dict(source),
            "subject": review.get("subject"),
            "static_reports": scans,
            "ai_review": review,
            **policy.to_dict(),
            "candidate_status": "EXPORTED_LOCAL" if candidate else "NOT_ELIGIBLE",
            "candidate": candidate,
            "review_fingerprint": dict(review_fingerprint),
            "evidence_ref": str(evidence.task_root),
            "status": "COMPLETED",
        }
        evidence.write_json("final-result.json", final_evidence)
        publish_reusable_result(
            config,
            batch_id=batch_id,
            task_id=task_id,
            root_name=skill_root_name(str(source_key["skill_path"]), str(source_key["skill_name"])),
            skill_digest=snapshot.skill_digest,
            review_fingerprint=review_fingerprint,
            source_evidence_ref=str(evidence.task_root),
            source=source,
            final_result=final_evidence,
        )
        results.append(
            _result_summary(
                task_id=task_id,
                source=source,
                review=review,
                scans=scans,
                policy=policy,
                candidate=candidate,
                evidence_ref=str(evidence.task_root),
            )
        )
    result_index = Path(repository_index).with_name(Path(repository_index).stem + ".results.json")
    _atomic_json(
        result_index,
        {
            "schema_version": "0.1",
            "batch_id": batch_id,
            "repository": index["repository"],
            "results": results,
        },
    )
    return tuple(results)


def cleanup_repository_workspace(
    config: ReviewConfig,
    *,
    batch_id: str,
    repository: str,
    repository_index: Path,
) -> bool:
    """Remove only a verified repository workspace after finalization.

    Evidence, manifests and candidates are outside ``workspace.root`` by
    configuration and are never targets of this function.
    """

    result_index = Path(repository_index).with_name(Path(repository_index).stem + ".results.json")
    if not result_index.is_file():
        raise OrchestrationError("repository results are not durable; cleanup is blocked")
    document = json.loads(result_index.read_text(encoding="utf-8"))
    if document.get("repository") != repository or not isinstance(document.get("results"), list):
        raise OrchestrationError("repository result index does not match cleanup target")
    original = json.loads(Path(repository_index).read_text(encoding="utf-8"))
    expected = {
        task["task_id"]
        for group in (original.get("tasks", []), original.get("reused_tasks", []))
        for task in group
    }
    completed = {
        result.get("task_id")
        for result in document["results"]
        if result.get("status") == "COMPLETED" and isinstance(result.get("task_id"), str)
    }
    if not expected.issubset(completed):
        raise OrchestrationError("not every prepared task has a completed final result")
    target = (
        config.workspace.root / batch_id / "repositories" / _repo_workspace_name(repository)
    ).resolve()
    root = config.workspace.root.resolve()
    if target.parent.parent.parent != root or not target.name.startswith(
        _repo_workspace_name(repository)
    ):
        raise OrchestrationError("cleanup target is outside the configured repository workspace")
    if not target.exists():
        return False
    if target.is_symlink() or not target.is_dir():
        raise OrchestrationError("cleanup target is not a real directory")
    shutil.rmtree(target)
    return True


__all__ = [
    "OrchestrationError",
    "PreparedTask",
    "ReusedTask",
    "RepositoryPlan",
    "RepositoryPreparation",
    "cleanup_repository_workspace",
    "finalize_repository",
    "plan_repositories",
    "prepare_repository",
]
