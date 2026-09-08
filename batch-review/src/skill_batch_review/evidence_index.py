"""Persistent evidence index maintained at the trusted artifact boundary.

Evidence bytes are hashed once when EvidenceStore writes/copies them.  This module
persists those already-computed integrity facts so incremental report refreshes do
not re-hash historical scanner/AI evidence.  Report readers still validate lexical
paths, symlink boundaries, file existence, size and mtime before exposing an index
entry; they never trust an index path as a filesystem locator on its own.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import artifacts as _base


INDEX_NAME = "evidence-index.json"
_INDEX_SCHEMA = "1.0"
_HEX_SHA256 = __import__("re").compile(r"^[0-9a-f]{64}$")

_METADATA: dict[str, tuple[str, str]] = {
    "final-result.json": ("综合结论", "DERIVED"),
    "source-metadata.json": ("来源元数据", "SOURCE"),
    "package-manifest.json": ("包清单", "SOURCE"),
    "scanners/cisco/normalized-result.json": ("Cisco 规范化结果", "NORMALIZED"),
    "scanners/cisco/raw-report.json": ("Cisco 原始输出", "RAW"),
    "scanners/skillspector/normalized-result.json": ("SkillSpector 规范化结果", "NORMALIZED"),
    "scanners/skillspector/raw-report.json": ("SkillSpector 原始输出", "RAW"),
    "ai/handoff.json": ("AI 任务交接", "SOURCE"),
    "ai/imported-result.json": ("AI 审查结果", "RAW"),
    "result-reuse.json": ("结果复用记录", "DERIVED"),
}


class EvidenceIndexError(_base.ArtifactError):
    pass


def _relative(value: str | os.PathLike[str]) -> str:
    return "/".join(_base._validate_relative_path(value))


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
        _base._fsync_directory(path.parent)
        return path
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_regular(task_root: Path, relative: str) -> tuple[Path, os.stat_result] | None:
    try:
        target = _base.safe_join(task_root, relative)
        lexical = Path(os.path.abspath(target))
        task = task_root.resolve(strict=True)
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(task)
        if lexical.is_symlink() or resolved.is_symlink() or not resolved.is_file():
            return None
        return resolved, os.stat(resolved, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError, _base.ArtifactError):
        return None


def _load_index(task_root: Path) -> dict[str, Any]:
    path = task_root / INDEX_NAME
    if not path.is_file() or path.is_symlink():
        return {
            "schema_version": _INDEX_SCHEMA,
            "batch_id": task_root.parent.name,
            "task_id": task_root.name,
            "artifacts": [],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceIndexError(f"evidence index is unreadable: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
        raise EvidenceIndexError("evidence index has an invalid structure")
    return value


def _artifact_record(reference: _base.EvidenceReference, *, task_root: Path, root: Path) -> dict[str, Any]:
    relative = _relative(reference.relative_path)
    if relative == INDEX_NAME:
        raise EvidenceIndexError("evidence index cannot index itself")
    safe = _safe_regular(task_root, relative)
    if safe is None:
        raise EvidenceIndexError(f"cannot index unsafe evidence file: {relative}")
    path, stat_result = safe
    try:
        root_relative = path.relative_to(root.resolve(strict=True)).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceIndexError(f"evidence file escapes configured root: {relative}") from exc
    label, evidence_type = _METADATA.get(relative, (Path(relative).name, "DERIVED"))
    digest = str(reference.sha256).lower()
    if not _HEX_SHA256.fullmatch(digest):
        raise EvidenceIndexError(f"invalid SHA-256 for evidence file: {relative}")
    if stat_result.st_size != reference.size_bytes:
        raise EvidenceIndexError(f"evidence size changed before indexing: {relative}")
    return {
        "label": label,
        "type": evidence_type,
        "path": root_relative,
        "task_path": relative,
        "size_bytes": int(reference.size_bytes),
        "sha256": digest,
        "mtime_ns": int(stat_result.st_mtime_ns),
        "navigation": "OPEN" if evidence_type in {"DERIVED", "NORMALIZED"} else "PATH_ONLY",
    }


def register_reference(
    task_root: Path,
    root: Path,
    reference: _base.EvidenceReference,
) -> Path:
    """Atomically insert/update one evidence record using its already-known digest."""

    task_root = task_root.resolve(strict=True)
    root = root.resolve(strict=True)
    task_root.relative_to(root)
    record = _artifact_record(reference, task_root=task_root, root=root)
    current = _load_index(task_root)
    artifacts = [
        dict(item)
        for item in current.get("artifacts", [])
        if isinstance(item, Mapping) and item.get("task_path") != record["task_path"]
    ]
    artifacts.append(record)
    artifacts.sort(key=lambda item: str(item.get("task_path") or ""))
    payload = {
        "schema_version": _INDEX_SCHEMA,
        "batch_id": task_root.parent.name,
        "task_id": task_root.name,
        "artifacts": artifacts,
    }
    return _atomic_json(task_root / INDEX_NAME, payload)


class IndexedEvidenceStore(_base.EvidenceStore):
    """EvidenceStore variant that synchronizes ``evidence-index.json`` on every write."""

    def _register(self, reference: _base.EvidenceReference) -> _base.EvidenceReference:
        register_reference(self.task_root, self.root, reference)
        return reference

    def _write(self, relative_path: str | os.PathLike[str], data: bytes) -> _base.EvidenceReference:
        return self._register(super()._write(relative_path, data))

    def copy_raw_report(
        self,
        source: str | os.PathLike[str],
        relative_path: str | os.PathLike[str] | None = None,
        *,
        scanner: str | None = None,
    ) -> _base.EvidenceReference:
        return self._register(
            super().copy_raw_report(source, relative_path, scanner=scanner)
        )

    copy_report = copy_raw_report


def _index_artifacts(task_root: Path, root: Path) -> list[dict[str, Any]]:
    try:
        index = _load_index(task_root)
    except EvidenceIndexError:
        return []
    values: list[dict[str, Any]] = []
    for item in index.get("artifacts", []):
        if not isinstance(item, Mapping):
            continue
        try:
            relative = _relative(str(item.get("task_path") or ""))
        except _base.ArtifactError:
            continue
        safe = _safe_regular(task_root, relative)
        if safe is None:
            continue
        path, stat_result = safe
        digest = str(item.get("sha256") or "").lower()
        try:
            size = int(item.get("size_bytes"))
            mtime_ns = int(item.get("mtime_ns"))
            path_from_root = path.relative_to(root).as_posix()
        except (TypeError, ValueError):
            continue
        if (
            not _HEX_SHA256.fullmatch(digest)
            or size != stat_result.st_size
            or mtime_ns != stat_result.st_mtime_ns
            or str(item.get("path") or "") != path_from_root
        ):
            continue
        evidence_type = str(item.get("type") or "DERIVED").upper()
        if evidence_type not in {"DERIVED", "NORMALIZED", "RAW", "SOURCE"}:
            continue
        values.append(
            {
                "label": str(item.get("label") or Path(relative).name),
                "type": evidence_type,
                "path": path_from_root,
                "task_path": relative,
                "size_bytes": size,
                "sha256": digest,
                "navigation": "OPEN" if evidence_type in {"DERIVED", "NORMALIZED"} else "PATH_ONLY",
            }
        )
    return values


def safe_evidence_bundle(reference: Any, evidence_root: Path | None) -> dict[str, Any]:
    """Read indexed evidence without hashing historical files during report refresh."""

    if evidence_root is None or not reference:
        return {"document": None, "reference": "", "artifacts": []}
    raw_root = evidence_root.expanduser().absolute()
    try:
        root = raw_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"document": None, "reference": "", "artifacts": []}
    raw = Path(str(reference)).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(raw_root)
    except ValueError:
        relative = None
    current = raw_root
    if relative is None:
        try:
            target = lexical.resolve(strict=True)
            target.relative_to(root)
            relative = target.relative_to(root)
            current = root
        except (OSError, RuntimeError, ValueError):
            return {"document": None, "reference": "", "artifacts": []}
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return {"document": None, "reference": "", "artifacts": []}
        except OSError:
            return {"document": None, "reference": "", "artifacts": []}
    try:
        target = lexical.resolve(strict=True)
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return {"document": None, "reference": "", "artifacts": []}
    if not target.is_dir() or target.is_symlink():
        return {"document": None, "reference": "", "artifacts": []}

    artifacts = _index_artifacts(target, root)
    document: Mapping[str, Any] | None = None
    final = next((item for item in artifacts if item["task_path"] == "final-result.json"), None)
    if final is not None and int(final["size_bytes"]) <= 20 * 1024 * 1024:
        safe = _safe_regular(target, "final-result.json")
        if safe is not None:
            try:
                value = json.loads(safe[0].read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, Mapping):
                from .reporting import redact

                redacted = redact(value)
                document = redacted if isinstance(redacted, Mapping) else None
    return {
        "document": document,
        "reference": target.relative_to(root).as_posix(),
        "artifacts": artifacts,
    }


__all__ = [
    "EvidenceIndexError",
    "INDEX_NAME",
    "IndexedEvidenceStore",
    "register_reference",
    "safe_evidence_bundle",
]
