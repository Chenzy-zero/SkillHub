#!/usr/bin/env python3
"""Compatibility CLI facade for the trusted batch-review launcher state machine.

The implementation lives in :mod:`skill_batch_review.batch_launcher`.  Keeping
this thin facade preserves the historical script path and the test/operator
surface while allowing the state machine itself to be imported and tested as a
normal package module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skill_batch_review import batch_launcher as _impl  # noqa: E402


LauncherError = _impl.LauncherError
CURRENT_WORKFLOW_VERSION = _impl.CURRENT_WORKFLOW_VERSION
PartialDownload = _impl.PartialDownload
PackageEntry = _impl.PackageEntry
SnapshotResult = _impl.SnapshotResult

# Re-export dependency hooks used by existing tests and approved local wrappers.
# Before each state-machine call these values are copied back into the package
# module so monkey-patching this historical script surface keeps working.
cleanup_repository_download = _impl.cleanup_repository_download
download_repository_skills = _impl.download_repository_skills
finalize_skill = _impl.finalize_skill
prepare_skill = _impl.prepare_skill
skill_task_id = _impl.skill_task_id
write_live_batch_report = _impl.write_live_batch_report
write_skill_html_report = _impl.write_skill_html_report
write_skill_result_tables = _impl.write_skill_result_tables
load_config = _impl.load_config

_AI_QUEUE_MODE = _impl._AI_QUEUE_MODE
_REPOSITORY_AI_QUEUE_MODE = _impl._REPOSITORY_AI_QUEUE_MODE
_LEGACY_AI_QUEUE_MODE = _impl._LEGACY_AI_QUEUE_MODE

_SYNC_NAMES = (
    "cleanup_repository_download",
    "download_repository_skills",
    "finalize_skill",
    "prepare_skill",
    "skill_task_id",
    "write_live_batch_report",
    "write_skill_html_report",
    "write_skill_result_tables",
    "load_config",
)


def _sync_dependencies() -> None:
    for name in _SYNC_NAMES:
        setattr(_impl, name, globals()[name])


def _new_state(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._new_state(*args, **kwargs)


def _load_state(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._load_state(*args, **kwargs)


def _save(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._save(*args, **kwargs)


def _inventory(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._inventory(*args, **kwargs)


def _prepare_next(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._prepare_next(*args, **kwargs)


def _finish_current(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._finish_current(*args, **kwargs)


def _activate_batch_queue(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._activate_batch_queue(*args, **kwargs)


def _cmd_plan(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._cmd_plan(*args, **kwargs)


def _cmd_start(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._cmd_start(*args, **kwargs)


def _cmd_advance(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._cmd_advance(*args, **kwargs)


def _cmd_report(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._cmd_report(*args, **kwargs)


def _cmd_status(*args: Any, **kwargs: Any):
    _sync_dependencies()
    return _impl._cmd_status(*args, **kwargs)


def _parser():
    _sync_dependencies()
    return _impl._parser()


def main(argv=None) -> int:
    _sync_dependencies()
    return _impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
