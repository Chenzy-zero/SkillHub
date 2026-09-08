"""Serialize evidence-index read/merge/write updates per task.

Static scanners intentionally run in parallel and may write different evidence
files for the same task at the same time.  The evidence files themselves can be
written concurrently, but the shared ``evidence-index.json`` is a read/modify/
replace document and therefore needs one in-process writer per task.  Without
this boundary Windows can reject competing ``os.replace`` calls and every
platform can lose an update when two threads read the same previous index.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any


_LOCKS_GUARD = threading.Lock()
_TASK_LOCKS: dict[str, threading.RLock] = {}


def _task_lock(task_root: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(task_root).resolve(strict=True)))
    with _LOCKS_GUARD:
        lock = _TASK_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TASK_LOCKS[key] = lock
        return lock


def install_evidence_index_concurrency(evidence_index_module: Any) -> None:
    """Install the lock once while preserving the module's public API."""

    if getattr(evidence_index_module, "_concurrency_boundary_installed", False):
        return
    original_register = evidence_index_module.register_reference

    def register_reference(task_root: Path, root: Path, reference: Any) -> Path:
        with _task_lock(Path(task_root)):
            return original_register(task_root, root, reference)

    evidence_index_module.register_reference = register_reference
    evidence_index_module._concurrency_boundary_installed = True


__all__ = ["install_evidence_index_concurrency"]
