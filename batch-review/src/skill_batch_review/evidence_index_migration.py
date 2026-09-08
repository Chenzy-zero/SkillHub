"""One-time migration path for evidence created before persistent indexes existed."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from . import artifacts as _base
from .evidence_index import INDEX_NAME, register_reference, safe_evidence_bundle


def _resolve_task(reference: Any, evidence_root: Path | None) -> tuple[Path, Path] | None:
    if evidence_root is None or not reference:
        return None
    try:
        root = evidence_root.expanduser().resolve(strict=True)
        raw = Path(str(reference)).expanduser()
        target = (raw if raw.is_absolute() else root / raw).resolve(strict=True)
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if target.is_symlink() or not target.is_dir():
        return None
    return root, target


def bootstrap_legacy_index(task_root: Path, evidence_root: Path) -> None:
    """Hash a pre-index evidence tree exactly once and persist trusted metadata."""

    if (task_root / INDEX_NAME).exists() or (task_root / INDEX_NAME).is_symlink():
        return
    stack: list[tuple[Path, str]] = [(task_root, "")]
    references: list[_base.EvidenceReference] = []
    while stack:
        directory, prefix = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError:
            return
        for entry in children:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if relative == INDEX_NAME:
                continue
            try:
                _base._validate_relative_path(relative)
                metadata = entry.stat(follow_symlinks=False)
            except (OSError, _base.ArtifactError):
                continue
            if stat.S_ISLNK(metadata.st_mode):
                continue
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                stack.append((path, relative))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            try:
                digest, size = _base._sha256_file(path)
            except _base.ArtifactError:
                continue
            references.append(_base.EvidenceReference(path, relative, digest, size))
    # Registration is atomic per entry. A crash can leave a valid partial index;
    # no corrupt half-written JSON is ever exposed. Subsequent EvidenceStore
    # writes continue filling/updating the same index.
    for reference in sorted(references, key=lambda item: item.relative_path):
        register_reference(task_root, evidence_root, reference)


def safe_evidence_bundle_compat(reference: Any, evidence_root: Path | None) -> dict[str, Any]:
    resolved = _resolve_task(reference, evidence_root)
    if resolved is None:
        return {"document": None, "reference": "", "artifacts": []}
    root, task_root = resolved
    index = task_root / INDEX_NAME
    if not index.exists() and not index.is_symlink():
        bootstrap_legacy_index(task_root, root)
    return safe_evidence_bundle(task_root, root)


__all__ = ["bootstrap_legacy_index", "safe_evidence_bundle_compat"]
