"""Execution-policy identity and safe legacy-state handling."""

from __future__ import annotations

from typing import Any, Mapping


CURRENT_WORKFLOW_VERSION = "repository_archive_v1"

_EXECUTION_FIELDS = (
    "task_id",
    "source_revision",
    "download_transport",
    "download_snapshot",
    "download_root",
    "handoff_path",
    "ai_result_path",
    "result_csv",
    "result_json",
)


def legacy_state_is_pristine(state: Mapping[str, Any]) -> bool:
    """Return whether an unversioned plan has never downloaded or reviewed data."""

    if state.get("status") != "READY" or state.get("current_task_id"):
        return False
    for field in (
        "active_repository",
        "completed_repositories",
        "result_csv",
        "result_json",
    ):
        if state.get(field):
            return False
    items = state.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, Mapping) or item.get("status") != "PENDING":
            return False
        if item.get("workspace_cleaned"):
            return False
        if any(item.get(field) for field in _EXECUTION_FIELDS):
            return False
    return True


__all__ = ["CURRENT_WORKFLOW_VERSION", "legacy_state_is_pristine"]
