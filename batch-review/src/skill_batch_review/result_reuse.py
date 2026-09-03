"""Fast, conservative reuse of previously approved Skill review results.

The canonical package digest is calculated while exporting a Git snapshot and
covers paths, file types, modes, symlink targets and file bytes.  Git does not
store filesystem timestamps, so comparing this digest is both faster than a
second directory walk and naturally ignores timestamps.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .config import ReviewConfig
from .scanners import ScannerAdapter


REUSE_STATUS = "RESULT_REUSED"
COMPARE_METHOD = "CANONICAL_SKILL_PACKAGE_SHA256"


def skill_root_name(skill_path: str, skill_name: str = "") -> str:
    """Return the exact Skill Root directory name used as the cheap prefilter."""

    normalized = str(skill_path).strip().rstrip("/")
    if normalized and normalized != ".":
        return PurePosixPath(normalized).name
    return str(skill_name).strip()


def _json_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_digest(root: Path) -> str:
    """Hash a trusted review-definition directory without using mtimes."""

    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        if path.is_symlink():
            entries.append({"path": relative, "type": "symlink", "mode": mode, "target": os.readlink(path)})
        elif path.is_dir():
            entries.append({"path": relative, "type": "directory", "mode": mode})
        elif path.is_file():
            entries.append({"path": relative, "type": "file", "mode": mode, "sha256": _file_digest(path)})
        else:
            raise ValueError(f"unsupported review definition entry: {path}")
    return _json_digest({"entries": entries})


def build_review_fingerprint(
    config: ReviewConfig, adapters: Mapping[str, ScannerAdapter]
) -> dict[str, Any]:
    """Bind reuse to every scanner, AI and policy input that affects a result."""

    payload = {
        "schema_version": "0.1",
        "scanners": {
            name: {
                "tool_version": adapter.tool_version,
                "config_digest": adapter.config_digest,
            }
            for name, adapter in sorted(adapters.items())
        },
        "ai": {
            "policy_version": config.ai.policy_version,
            "reviewer_model": config.ai.reviewer_model,
            "review_skill_digest": _directory_digest(config.ai.skill_path),
            "result_schema_sha256": _file_digest(config.ai.result_schema_path),
        },
        "quality_candidate_threshold": config.quality.candidate_threshold,
    }
    return {**payload, "fingerprint_digest": _json_digest(payload)}


def _component(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reuse_directory(
    manifest_root: Path, root_name: str, skill_digest: str, fingerprint_digest: str
) -> Path:
    return (
        manifest_root
        / "_approved-result-reuse"
        / _component(root_name)
        / skill_digest
        / fingerprint_digest
    )


def _safe_source_result(record: Mapping[str, Any], evidence_root: Path) -> Mapping[str, Any] | None:
    reference = record.get("source_evidence_ref")
    if not isinstance(reference, str) or not reference:
        return None
    root = evidence_root.expanduser().resolve()
    task_root = Path(reference).expanduser().resolve()
    try:
        task_root.relative_to(root)
    except ValueError:
        return None
    result = task_root / "final-result.json"
    if task_root.is_symlink() or not task_root.is_dir() or result.is_symlink() or not result.is_file():
        return None
    try:
        value = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    if value.get("status") != "COMPLETED" or value.get("security_decision") != "PASS":
        return None
    if value.get("quality_decision") != "PASS" or not value.get("candidate_eligible"):
        return None
    if value.get("review_fingerprint") != record.get("review_fingerprint"):
        return None
    subject = value.get("subject")
    if not isinstance(subject, Mapping) or subject.get("skill_digest_sha256") != record.get("skill_digest"):
        return None
    score = value.get("quality_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        return None
    return value


def find_reusable_result(
    config: ReviewConfig,
    *,
    root_name: str,
    skill_digest: str,
    review_fingerprint: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any]] | None:
    """Use an O(1)-addressed index, then validate the approved source evidence."""

    directory = _reuse_directory(
        config.workspace.manifest_root,
        root_name,
        skill_digest,
        str(review_fingerprint["fingerprint_digest"]),
    )
    if not directory.is_dir() or directory.is_symlink():
        return None
    for path in sorted(directory.glob("*.json"), reverse=True):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if (
            record.get("skill_root_name") != root_name
            or record.get("skill_digest") != skill_digest
            or record.get("review_fingerprint") != review_fingerprint
            or record.get("security_decision") != "PASS"
            or record.get("quality_decision") != "PASS"
        ):
            continue
        source_result = _safe_source_result(record, config.workspace.evidence_root)
        if source_result is not None:
            return record, source_result
    return None


def publish_reusable_result(
    config: ReviewConfig,
    *,
    batch_id: str,
    task_id: str,
    root_name: str,
    skill_digest: str,
    review_fingerprint: Mapping[str, Any],
    source_evidence_ref: str,
    source: Mapping[str, Any],
    final_result: Mapping[str, Any],
) -> Path | None:
    """Publish an immutable pointer only for a complete, candidate-eligible PASS."""

    if (
        final_result.get("status") != "COMPLETED"
        or final_result.get("security_decision") != "PASS"
        or final_result.get("quality_decision") != "PASS"
        or not final_result.get("candidate_eligible")
    ):
        return None
    directory = _reuse_directory(
        config.workspace.manifest_root,
        root_name,
        skill_digest,
        str(review_fingerprint["fingerprint_digest"]),
    )
    directory.mkdir(parents=True, exist_ok=True)
    filename = _component(f"{batch_id}\0{task_id}") + ".json"
    path = directory / filename
    record = {
        "schema_version": "0.1",
        "source_batch_id": batch_id,
        "source_task_id": task_id,
        "source_evidence_ref": source_evidence_ref,
        "source": dict(source),
        "skill_root_name": root_name,
        "skill_digest": skill_digest,
        "review_fingerprint": dict(review_fingerprint),
        "security_decision": "PASS",
        "quality_decision": "PASS",
        "quality_score": final_result.get("quality_score"),
    }
    data = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError(f"refusing to replace different reuse record: {path}")
        return path
    descriptor, temporary_name = tempfile.mkstemp(prefix=".reuse-", suffix=".tmp", dir=str(directory))
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


__all__ = [
    "COMPARE_METHOD",
    "REUSE_STATUS",
    "build_review_fingerprint",
    "find_reusable_result",
    "publish_reusable_result",
    "skill_root_name",
]
