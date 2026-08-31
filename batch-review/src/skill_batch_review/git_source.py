"""Safe Git source resolution for the batch Skill review pipeline.

This module deliberately stops at source resolution.  It does not export a
Skill package, calculate a package digest, run a scanner, or execute anything
stored in a repository.  Git commands are always passed as an argument
vector to :mod:`subprocess`; no command string is ever sent to a shell.

The two levels of identity from :mod:`skill_batch_review.models` are kept
separate here as well:

* ``SourceKey`` identifies one repository/branch/path/name source;
* ``ReviewTargetKey`` groups those sources when choosing the latest branch
  candidate for a batch.

``GitMirror`` exposes clone and fetch as explicit operations.  Constructing a
mirror or a resolver never contacts a remote repository.  A caller therefore
has to make the network boundary visible in orchestration code by calling
``clone()``, ``fetch()``, or ``clone_or_fetch()`` itself.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .inventory import InventoryRow
from .models import ReviewTargetKey, normalize_branch, normalize_skill_path


# Selection values are strings because InventoryRow already preserves the
# source CSV's status values as strings, and the formal design has a few
# values (for example STALE_INVENTORY) that are not in the small base enum.
SELECTED = "SELECTED"
PENDING = "PENDING"
SKIPPED_SUPERSEDED_BRANCH = "SKIPPED_SUPERSEDED_BRANCH"
BRANCH_CONTENT_CONFLICT = "BRANCH_CONTENT_CONFLICT"
STALE_INVENTORY = "STALE_INVENTORY"
INPUT_INVALID = "INPUT_INVALID"
INPUT_CONFLICT = "INPUT_CONFLICT"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class GitSourceError(RuntimeError):
    """Base error for command, repository, or source-resolution failures."""


class GitExecutableError(GitSourceError):
    """The configured Git executable could not be started."""


class GitTimeoutError(GitSourceError):
    """A Git command exceeded its configured timeout."""


class GitCommandError(GitSourceError):
    """A Git command returned a non-zero status when checking was enabled."""

    def __init__(self, result: "GitResult") -> None:
        self.result = result
        # Keep the exception useful without embedding a potentially sensitive
        # full command line or environment.  Callers can inspect ``result``
        # when a local diagnostic needs the exact argv and stderr.
        command = " ".join(result.args)
        stderr = result.stderr.strip()
        detail = f": {stderr[:500]}" if stderr else ""
        super().__init__(f"Git command failed with exit code {result.returncode}: {command}{detail}")

    @property
    def returncode(self) -> int:
        return self.result.returncode


class BranchNotFoundError(GitSourceError):
    """A branch does not resolve to a commit in the repository."""


class CommitNotFoundError(GitSourceError):
    """A revision is missing or does not identify a commit object."""


class SkillPathNotFoundError(GitSourceError):
    """The requested Skill Root does not contain ``SKILL.md``."""


class PathChangeNotFoundError(GitSourceError):
    """No path-change commit could be found for an existing Skill Root."""


def _validate_argv(args: Sequence[str]) -> tuple[str, ...]:
    """Validate a Git argument vector before passing it to subprocess.

    Requiring a sequence of strings catches accidental command-string use at
    the API boundary.  NUL bytes are rejected because they cannot be passed to
    an OS process safely.  Empty arguments are not useful for the Git methods
    in this module and are rejected to make malformed templates visible.
    """

    if isinstance(args, (str, bytes, bytearray)):
        raise TypeError("Git arguments must be a sequence of strings, not a command string")
    values = tuple(args)
    for value in values:
        if not isinstance(value, str):
            raise TypeError("Git arguments must contain only strings")
        if not value:
            raise ValueError("Git arguments must not contain empty strings")
        if "\x00" in value:
            raise ValueError("Git arguments must not contain NUL bytes")
    return values


@dataclass(frozen=True, slots=True)
class GitResult:
    """The text result of one shell-free Git invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def argv(self) -> tuple[str, ...]:
        """Alias useful to callers that call the field ``argv``."""

        return self.args


class GitRunner:
    """Run Git with an explicit argv and ``shell=False``.

    The runner has no repository-specific behaviour.  It is intentionally
    small so tests can inject a fake runner or patch ``subprocess.run`` while
    the source resolver remains unchanged.
    """

    def __init__(
        self,
        executable: str = "git",
        *,
        default_timeout: float | None = 600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("Git executable must be a non-empty string")
        if "\x00" in executable:
            raise ValueError("Git executable must not contain NUL")
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive or None")
        if env is not None:
            # Copy once, both to make the runner immutable from the caller's
            # perspective and to keep subprocess from observing later edits.
            env = dict(env)
            for key, value in env.items():
                if "\x00" in key or "\x00" in value:
                    raise ValueError("Git environment must not contain NUL")
        self.executable = executable
        self.default_timeout = default_timeout
        self.env = env

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> GitResult:
        """Run one Git command without a shell.

        ``check=False`` is used only for expected probes such as checking
        whether a branch or path exists.  All callers still receive the
        complete exit code and output for audit/diagnostic purposes.
        """

        argv = _validate_argv(args)
        if timeout is None:
            timeout = self.default_timeout
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive or None")
        if input_text is not None and not isinstance(input_text, str):
            raise TypeError("input_text must be a string or None")
        command = (self.executable, *argv)
        try:
            completed = subprocess.run(
                list(command),
                cwd=str(cwd) if cwd is not None else None,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=dict(self.env) if self.env is not None else None,
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise GitExecutableError(f"Git executable is unavailable: {self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(f"Git command exceeded timeout: {self.executable}") from exc
        result = GitResult(
            args=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and not result.ok:
            raise GitCommandError(result)
        return result

    def checked(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
    ) -> GitResult:
        """Explicit alias for a command that must succeed."""

        return self.run(args, cwd=cwd, timeout=timeout, check=True)


@dataclass(frozen=True, slots=True)
class GitMirror:
    """A repository mirror whose remote operations are explicit methods."""

    url: str
    path: Path
    runner: GitRunner = field(default_factory=GitRunner)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("mirror URL must be a non-empty string")
        if "\x00" in self.url:
            raise ValueError("mirror URL must not contain NUL")
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())

    def clone(self) -> GitResult:
        """Create a mirror; this is the explicit clone/network boundary."""

        if self.path.exists():
            raise GitSourceError(f"mirror destination already exists: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self.runner.checked(
            ("clone", "--mirror", self.url, str(self.path)),
            cwd=self.path.parent,
        )

    def fetch(self) -> GitResult:
        """Update an existing mirror; this is the explicit fetch boundary."""

        if not self.path.is_dir():
            raise GitSourceError(f"mirror path does not exist: {self.path}")
        # ``clone --mirror`` configures origin to map all refs.  Fetching the
        # named remote retains that behaviour while pruning deleted refs.
        return self.runner.checked(("fetch", "--prune", "--tags", "origin"), cwd=self.path)

    def clone_or_fetch(self) -> GitResult:
        """Create or update a mirror when the caller explicitly requests it."""

        return self.fetch() if self.path.exists() else self.clone()

    def is_bare(self) -> bool:
        result = self.runner.run(("rev-parse", "--is-bare-repository"), cwd=self.path, check=False)
        return result.ok and result.stdout.strip() == "true"


# A descriptive alias for callers that prefer RepositoryMirror terminology.
RepositoryMirror = GitMirror


_HEX_REVISION_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _revision_hint(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("revision must be a hexadecimal Git object name")
    revision = value.strip()
    if not _HEX_REVISION_RE.fullmatch(revision):
        raise ValueError("revision must contain 4-64 hexadecimal characters")
    return revision.lower()


def _parse_commit_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise GitSourceError(f"Git returned an invalid commit time: {value!r}") from exc


def _skill_file_path(skill_path: str) -> str:
    path = normalize_skill_path(skill_path)
    return "SKILL.md" if path == "." else f"{path}/SKILL.md"


@dataclass(frozen=True, slots=True)
class SkillPathChange:
    """The newest commit that changed any file under a Skill Root."""

    revision: str
    commit_time: datetime

    @property
    def skill_last_change_revision(self) -> str:
        return self.revision

    @property
    def skill_last_change_time(self) -> datetime:
        return self.commit_time

    @property
    def timestamp(self) -> datetime:
        return self.commit_time


class GitRepository:
    """Read-only Git queries against an already available repository.

    ``GitRepository`` never clones or fetches.  Use :class:`GitMirror` for
    those explicit operations, then pass its path to this class.
    """

    def __init__(self, path: str | Path, *, runner: GitRunner | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.runner = runner or GitRunner()

    def _run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> GitResult:
        if not self.path.is_dir():
            raise GitSourceError(f"repository path does not exist: {self.path}")
        return self.runner.run(args, cwd=self.path, check=check, timeout=timeout)

    def _resolve_commit_expression(self, expression: str, *, error: str) -> str:
        result = self._run(("rev-parse", "--verify", "--quiet", expression), check=False)
        resolved = result.stdout.strip().splitlines()
        if not result.ok or not resolved or not _HEX_REVISION_RE.fullmatch(resolved[0]):
            raise GitSourceError(error)
        return resolved[0].lower()

    def resolve_branch_head(self, branch: str) -> str:
        """Resolve a normalized branch name to its commit object ID."""

        try:
            normalized = normalize_branch(branch)
        except (TypeError, ValueError) as exc:
            raise BranchNotFoundError(f"invalid branch: {branch!r}") from exc
        return self._resolve_commit_expression(
            f"refs/heads/{normalized}^{{commit}}",
            error=f"branch does not resolve to a commit: {normalized}",
        )

    # Short alias used by some orchestration code.
    branch_head = resolve_branch_head

    def verify_commit(self, revision: str) -> str:
        """Verify that a revision exists and is a commit object.

        Abbreviated hexadecimal object names are accepted for convenience and
        canonicalized to the full object ID.  Inventory validation still
        requires a full SHA; this method is also useful for branch-derived
        revisions and local diagnostics.
        """

        try:
            hint = _revision_hint(revision)
        except (TypeError, ValueError) as exc:
            raise CommitNotFoundError(f"invalid revision: {revision!r}") from exc
        object_type = self._run(("cat-file", "-t", hint), check=False)
        if not object_type.ok or object_type.stdout.strip() != "commit":
            raise CommitNotFoundError(f"revision is not a commit object: {revision}")
        try:
            return self._resolve_commit_expression(
                f"{hint}^{{commit}}",
                error=f"revision cannot be resolved as a commit: {revision}",
            )
        except GitSourceError as exc:
            raise CommitNotFoundError(str(exc)) from exc

    def is_revision_reachable(self, branch: str, revision: str) -> bool:
        """Return whether ``revision`` is an ancestor of ``branch``."""

        resolved_revision = self.verify_commit(revision)
        head = self.resolve_branch_head(branch)
        result = self._run(
            ("merge-base", "--is-ancestor", resolved_revision, head),
            check=False,
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise GitCommandError(result)

    # Wording used in the design document.
    revision_reachable_from_branch = is_revision_reachable

    def skill_path_exists(self, revision: str, skill_path: str) -> bool:
        """Check for a blob at ``<skill_path>/SKILL.md`` in a revision."""

        try:
            normalized_path = normalize_skill_path(skill_path)
        except (TypeError, ValueError) as exc:
            raise SkillPathNotFoundError(f"invalid Skill path: {skill_path!r}") from exc
        resolved_revision = self.verify_commit(revision)
        expression = f"{resolved_revision}:{_skill_file_path(normalized_path)}"
        result = self._run(("cat-file", "-t", expression), check=False)
        return result.ok and result.stdout.strip() == "blob"

    # Friendly aliases for callers that phrase this as an existence query.
    has_skill = skill_path_exists
    skill_exists = skill_path_exists

    def require_skill_path(self, revision: str, skill_path: str) -> str:
        """Validate and return the normalized Skill Root path."""

        normalized = normalize_skill_path(skill_path)
        if not self.skill_path_exists(revision, normalized):
            raise SkillPathNotFoundError(
                f"Skill path does not contain SKILL.md at revision {revision}: {normalized}"
            )
        return normalized

    def path_last_change(self, revision: str, skill_path: str) -> SkillPathChange:
        """Find the newest commit changing any file under a Skill Root."""

        normalized = self.require_skill_path(revision, skill_path)
        resolved_revision = self.verify_commit(revision)
        # A root-level Skill owns the whole repository snapshot, not only its
        # SKILL.md.  Using "." ensures a script/config change is considered a
        # Skill change even when the anchor file is untouched.
        pathspec = normalized if normalized != "." else "."
        result = self._run(
            (
                "log",
                "-1",
                "--no-ext-diff",
                "--format=%H%x00%cI",
                resolved_revision,
                "--",
                pathspec,
            ),
            check=True,
        )
        line = result.stdout.strip().splitlines()
        if not line:
            raise PathChangeNotFoundError(
                f"no path-change commit found at revision {resolved_revision}: {normalized}"
            )
        parts = line[0].split("\x00", 1)
        if len(parts) != 2 or not _HEX_REVISION_RE.fullmatch(parts[0].strip()):
            raise PathChangeNotFoundError(
                f"Git returned an invalid path-change record for {normalized}"
            )
        return SkillPathChange(parts[0].strip().lower(), _parse_commit_time(parts[1]))

    skill_path_last_change = path_last_change


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A source row enriched with frozen Git facts and selection state."""

    row: InventoryRow
    resolved_branch_head: str | None = None
    source_revision: str | None = None
    skill_last_change_revision: str | None = None
    skill_last_change_time: datetime | None = None
    inventory_resolved_revision: str | None = None
    source_selection_status: str = PENDING
    reasons: tuple[str, ...] = ()
    inventory_difference_reasons: tuple[str, ...] = ()
    branch_content_conflict: bool = False
    awaiting_snapshot: bool = False
    error: str | None = None

    @property
    def source_key(self):
        return self.row.source_key

    @property
    def review_target_key(self) -> ReviewTargetKey:
        return self.row.review_target_key

    @property
    def branch_head(self) -> str | None:
        return self.resolved_branch_head

    @property
    def stale_inventory(self) -> bool:
        return STALE_INVENTORY in self.source_selection_status or STALE_INVENTORY in self.reasons

    @property
    def selected(self) -> bool:
        return self.source_selection_status == SELECTED

    @property
    def needs_snapshot(self) -> bool:
        return self.awaiting_snapshot or self.branch_content_conflict

    def to_dict(self) -> dict[str, object]:
        return {
            "source_row_id": self.row.source_row_id,
            "source_key": self.source_key.to_dict(),
            "review_target_key": self.review_target_key.to_dict(),
            "inventory_revision": self.row.inventory_revision,
            "resolved_branch_head": self.resolved_branch_head,
            "skill_last_change_revision": self.skill_last_change_revision,
            "skill_last_change_time": (
                self.skill_last_change_time.isoformat() if self.skill_last_change_time else None
            ),
            "source_revision": self.source_revision,
            "inventory_resolved_revision": self.inventory_resolved_revision,
            "source_selection_status": self.source_selection_status,
            "reasons": list(self.reasons),
            "inventory_difference_reasons": list(self.inventory_difference_reasons),
            "branch_content_conflict": self.branch_content_conflict,
            "awaiting_snapshot": self.awaiting_snapshot,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SourceSelectionResult:
    """The complete source-resolution and cross-branch selection result."""

    records: tuple[ResolvedSource, ...]

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def selected(self) -> tuple[ResolvedSource, ...]:
        return tuple(item for item in self.records if item.selected)

    @property
    def selected_records(self) -> tuple[ResolvedSource, ...]:
        return self.selected

    @property
    def conflicts(self) -> tuple[ResolvedSource, ...]:
        return tuple(item for item in self.records if item.branch_content_conflict)

    @property
    def stale_inventory(self) -> tuple[ResolvedSource, ...]:
        return tuple(item for item in self.records if item.stale_inventory)

    @property
    def superseded(self) -> tuple[ResolvedSource, ...]:
        return tuple(
            item for item in self.records if item.source_selection_status == SKIPPED_SUPERSEDED_BRANCH
        )

    @property
    def awaiting_snapshot(self) -> tuple[ResolvedSource, ...]:
        return tuple(item for item in self.records if item.needs_snapshot)

    def by_target(self) -> dict[ReviewTargetKey, tuple[ResolvedSource, ...]]:
        groups: dict[ReviewTargetKey, list[ResolvedSource]] = defaultdict(list)
        for item in self.records:
            groups[item.review_target_key].append(item)
        return {key: tuple(values) for key, values in groups.items()}

    def to_dict(self) -> dict[str, object]:
        return {"records": [item.to_dict() for item in self.records]}


class GitSourceResolver(GitRepository):
    """Resolve inventory rows and choose the newest source per target."""

    def resolve_row(self, row: InventoryRow) -> ResolvedSource:
        """Resolve one inventory row without downloading or executing content."""

        if not isinstance(row, InventoryRow):
            raise TypeError("resolve_row expects an InventoryRow")
        if row.has_input_conflict:
            return ResolvedSource(
                row=row,
                source_selection_status=INPUT_CONFLICT,
                reasons=("CSV rows for this source disagree",),
            )
        try:
            head = self.resolve_branch_head(row.branch)
            normalized_path = normalize_skill_path(row.skill_path)
        except (TypeError, ValueError, BranchNotFoundError) as exc:
            return ResolvedSource(
                row=row,
                source_selection_status=INPUT_INVALID,
                reasons=("branch or Skill path is invalid",),
                error=str(exc),
            )
        except GitSourceError as exc:
            return ResolvedSource(
                row=row,
                source_selection_status=SOURCE_UNAVAILABLE,
                reasons=("branch head could not be resolved",),
                error=str(exc),
            )

        try:
            if not self.skill_path_exists(head, normalized_path):
                return ResolvedSource(
                    row=row,
                    resolved_branch_head=head,
                    source_revision=head,
                    source_selection_status=STALE_INVENTORY,
                    reasons=("SKILL.md is missing at the frozen branch head",),
                    error=f"Skill path does not contain SKILL.md: {normalized_path}",
                )
            path_change = self.path_last_change(head, normalized_path)
        except (SkillPathNotFoundError, PathChangeNotFoundError) as exc:
            return ResolvedSource(
                row=row,
                resolved_branch_head=head,
                source_revision=head,
                source_selection_status=STALE_INVENTORY,
                reasons=("Skill path could not be resolved",),
                error=str(exc),
            )
        except GitSourceError as exc:
            return ResolvedSource(
                row=row,
                resolved_branch_head=head,
                source_revision=head,
                source_selection_status=SOURCE_UNAVAILABLE,
                reasons=("Skill path lookup failed",),
                error=str(exc),
            )

        differences: list[str] = []
        stale_reasons: list[str] = []
        inventory_resolved: str | None = None
        try:
            inventory_resolved = self.verify_commit(row.inventory_revision)
            if inventory_resolved != head:
                differences.append("inventory_revision_differs_from_branch_head")
            if inventory_resolved != path_change.revision:
                differences.append("inventory_revision_differs_from_skill_path_last_change")
            reachable = self._is_ancestor(inventory_resolved, head)
            if not reachable:
                stale_reasons.append("inventory_revision_is_not_reachable_from_branch_head")
            elif inventory_resolved != head and inventory_resolved != path_change.revision:
                # An older revision that is an ancestor of the latest path
                # change is still stale for this batch.  A revision equal to
                # either the current branch head or the path's latest change
                # is a reasonable ledger hint, even when unrelated commits
                # exist after the Skill change.
                if self._is_ancestor(inventory_resolved, path_change.revision):
                    stale_reasons.append("inventory_revision_is_older_than_skill_path_change")
                else:
                    stale_reasons.append("inventory_revision_is_not_related_to_skill_path_change")
        except (CommitNotFoundError, ValueError) as exc:
            stale_reasons.append("inventory_revision_is_not_a_commit")
            differences.append("inventory_revision_cannot_be_resolved")
            inventory_resolved = None
        except GitSourceError as exc:
            stale_reasons.append("inventory_revision_reachability_could_not_be_verified")
            differences.append("inventory_revision_reachability_check_failed")

        status = STALE_INVENTORY if stale_reasons else PENDING
        reasons = tuple(stale_reasons)
        return ResolvedSource(
            row=row,
            resolved_branch_head=head,
            source_revision=head,
            skill_last_change_revision=path_change.revision,
            skill_last_change_time=path_change.commit_time,
            inventory_resolved_revision=inventory_resolved,
            source_selection_status=status,
            reasons=reasons,
            inventory_difference_reasons=tuple(differences),
        )

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(("merge-base", "--is-ancestor", ancestor, descendant), check=False)
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise GitCommandError(result)

    def resolve_sources(self, rows: Iterable[InventoryRow]) -> SourceSelectionResult:
        """Resolve all rows, then select one latest candidate per target."""

        records = [self.resolve_row(row) for row in rows]
        groups: dict[ReviewTargetKey, list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            groups[record.review_target_key].append(index)

        for indexes in groups.values():
            eligible = [
                index
                for index in indexes
                if records[index].skill_last_change_time is not None
                and records[index].source_selection_status
                in {PENDING, STALE_INVENTORY}
            ]
            if not eligible:
                continue
            latest_time = max(records[index].skill_last_change_time for index in eligible)
            latest = [
                index
                for index in eligible
                if records[index].skill_last_change_time == latest_time
            ]
            revisions = {
                records[index].skill_last_change_revision for index in latest
            }
            if len(revisions) > 1:
                for index in latest:
                    item = records[index]
                    # Conflict is deliberately emitted before a digest exists;
                    # the snapshot stage must inspect both versions.
                    records[index] = replace(
                        item,
                        source_selection_status=BRANCH_CONTENT_CONFLICT,
                        branch_content_conflict=True,
                        awaiting_snapshot=True,
                        reasons=item.reasons
                        + (
                            "same path-change time has different commits; snapshot both before selection",
                        ),
                    )
            else:
                # Stable deterministic ordering is important for repeatable
                # manifests.  Prefer a non-stale record if both branches point
                # at the same newest path-change commit, then branch and row ID.
                winner = sorted(
                    latest,
                    key=lambda index: (
                        records[index].source_selection_status != STALE_INVENTORY,
                        records[index].row.branch,
                        records[index].row.source_row_id,
                    ),
                    reverse=True,
                )[0]
                for index in eligible:
                    item = records[index]
                    if index == winner:
                        if item.source_selection_status == STALE_INVENTORY:
                            continue
                        records[index] = replace(item, source_selection_status=SELECTED)
                    elif item.source_selection_status == STALE_INVENTORY:
                        continue
                    else:
                        records[index] = replace(
                            item,
                            source_selection_status=SKIPPED_SUPERSEDED_BRANCH,
                            reasons=item.reasons
                            + ("a newer branch candidate was selected for this target",),
                        )
        return SourceSelectionResult(tuple(records))

    # Names used by orchestration code and tests.
    select_latest = resolve_sources
    resolve_and_select = resolve_sources


def select_latest_sources(
    repository: str | Path | GitRepository,
    rows: Iterable[InventoryRow],
    *,
    runner: GitRunner | None = None,
) -> SourceSelectionResult:
    """Convenience function for selecting rows from one local repository."""

    resolver = repository if isinstance(repository, GitRepository) else GitSourceResolver(repository, runner=runner)
    return resolver.resolve_sources(rows)  # type: ignore[attr-defined]


__all__ = [
    "BRANCH_CONTENT_CONFLICT",
    "BranchNotFoundError",
    "CommitNotFoundError",
    "GitCommandError",
    "GitExecutableError",
    "GitMirror",
    "GitRepository",
    "GitResult",
    "GitRunner",
    "GitSourceError",
    "GitSourceResolver",
    "GitTimeoutError",
    "INPUT_CONFLICT",
    "INPUT_INVALID",
    "PENDING",
    "PathChangeNotFoundError",
    "RepositoryMirror",
    "ResolvedSource",
    "SELECTED",
    "SKIPPED_SUPERSEDED_BRANCH",
    "SOURCE_UNAVAILABLE",
    "STALE_INVENTORY",
    "SkillPathChange",
    "SkillPathNotFoundError",
    "SourceSelectionResult",
    "select_latest_sources",
]
