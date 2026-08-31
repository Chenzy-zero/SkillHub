"""Data models shared by the batch-review stages.

These models describe state and identity; they do not perform I/O.
``SourceKey``/``ReviewTargetKey`` make the two identity levels explicit:
source identity includes a branch, while the normal batch selection view does
not. CSV evidence is represented by ``inventory.InventoryRow``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class _StringEnum(str, Enum):
    """Enum base with predictable JSON/string behaviour on Python 3.11+."""

    def __str__(self) -> str:
        return self.value


class ScanStatus(_StringEnum):
    NOT_SCANNED = "NOT_SCANNED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"


class SecurityDecision(_StringEnum):
    NOT_REVIEWED = "NOT_REVIEWED"
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INCOMPLETE = "INCOMPLETE"


class QualityDecision(_StringEnum):
    NOT_REVIEWED = "NOT_REVIEWED"
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class TaskStatus(_StringEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class SourceSelectionStatus(_StringEnum):
    PENDING = "PENDING"
    SELECTED = "SELECTED"
    SKIPPED_SUPERSEDED_BRANCH = "SKIPPED_SUPERSEDED_BRANCH"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


def _required_text(value: Any, field_name: str) -> str:
    if value is None:
        raise ValueError(f"{field_name} must not be empty")
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in result:
        raise ValueError(f"{field_name} must not contain NUL")
    return result


def normalize_branch(value: Any) -> str:
    """Return the canonical short branch name.

    ``refs/heads/main`` and ``main`` identify the same branch.  Other Git ref
    namespaces are rejected here because this input is explicitly a branch
    column, not an arbitrary refspec.
    """

    branch = _required_text(value, "branch")
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    if not branch or branch.startswith("refs/"):
        raise ValueError(f"branch must be a branch name, got {value!r}")
    if "\\" in branch:
        raise ValueError(f"branch must use POSIX separators: {value!r}")
    if branch.startswith("/") or branch.endswith("/") or "//" in branch:
        raise ValueError(f"branch is not normalized: {value!r}")
    if any(part in {".", ".."} for part in branch.split("/")):
        raise ValueError(f"branch contains an invalid path component: {value!r}")
    return branch


def normalize_skill_path(value: Any) -> str:
    """Normalize a repository-relative Skill Root path.

    The root path is represented by ``.``. Only POSIX separators are accepted;
    traversal and absolute paths are always rejected.
    """

    path = _required_text(value, "skill_path")
    if path == "/" or path == ".":
        return "."
    if "\\" in path:
        raise ValueError(f"skill_path must use POSIX separators: {value!r}")
    if path.startswith("/"):
        raise ValueError(f"skill_path must be repository-relative: {value!r}")
    if "//" in path:
        raise ValueError(f"skill_path is not normalized: {value!r}")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts:
        return "."
    if any(part == ".." for part in parts):
        raise ValueError(f"skill_path must not traverse outside the repository: {value!r}")
    if any("\x00" in part for part in parts):
        raise ValueError("skill_path must not contain NUL")
    return "/".join(parts)


@dataclass(frozen=True, order=True)
class SourceKey:
    """Stable source identity, including branch and display name."""

    repository: str
    branch: str
    skill_path: str
    skill_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", _required_text(self.repository, "repository"))
        object.__setattr__(self, "branch", normalize_branch(self.branch))
        object.__setattr__(self, "skill_path", normalize_skill_path(self.skill_path))
        object.__setattr__(self, "skill_name", _required_text(self.skill_name, "skill_name"))

    def to_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "skill_path": self.skill_path,
            "skill_name": self.skill_name,
        }


@dataclass(frozen=True, order=True)
class ReviewTargetKey:
    """Normal batch-selection identity, intentionally excluding branch/name."""

    repository: str
    skill_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", _required_text(self.repository, "repository"))
        object.__setattr__(self, "skill_path", normalize_skill_path(self.skill_path))

    def to_dict(self) -> dict[str, str]:
        return {"repository": self.repository, "skill_path": self.skill_path}


__all__ = [
    "QualityDecision",
    "ReviewTargetKey",
    "ScanStatus",
    "SecurityDecision",
    "SourceKey",
    "SourceSelectionStatus",
    "TaskStatus",
    "normalize_branch",
    "normalize_skill_path",
]
