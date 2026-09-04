"""Read-only snapshots of a Skill Package from an immutable Git revision.

This module is deliberately implemented with ``git ls-tree`` and
``git cat-file`` instead of a checkout.  That gives the caller the bytes and
the modes that Git stores while avoiding repository hooks, filters, build
steps, and execution of anything from the Skill.  The exported directory is
also safe to scan: symbolic links are represented in the manifest and are
never created or followed.

The public entry point is :func:`export_skill_snapshot`.  It returns a
:class:`SnapshotResult` even when the package is not fully reviewable.  Any
condition that means that the review material is incomplete is represented by
a blocking ``coverage_issue`` and ``coverage_complete`` is false.  A caller
must therefore check both the digest and the coverage result before treating a
snapshot as an input for a security decision.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import normalize_skill_path


_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_REVISION_RE = _OBJECT_ID_RE
_LFS_VERSION = b"https://git-lfs.github.com/spec/v1"
_LFS_OID_RE = re.compile(rb"^oid sha256:([0-9a-fA-F]{64})$")
_LFS_SIZE_RE = re.compile(rb"^size ([0-9]+)$")
_UNSAFE_CONTROL_CHARS = frozenset(chr(value) for value in range(32)) | {chr(127)}


class SnapshotError(ValueError):
    """Base error for invalid snapshot requests or a broken Git source."""


class UnsafePathError(SnapshotError):
    """Raised when a Git path cannot be safely represented as relative."""


class GitSourceError(SnapshotError):
    """Raised when the requested local repository/revision cannot be read."""


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    """Resource limits applied before content is copied from Git.

    The defaults are intentionally conservative for a Skill package.  They
    are limits on source bytes, not the size of the JSON manifest.
    """

    max_file_size_bytes: int = 10 * 1024 * 1024
    max_package_size_bytes: int = 100 * 1024 * 1024
    max_file_count: int = 10_000
    git_timeout_seconds: int = 120

    def __post_init__(self) -> None:
        for name in (
            "max_file_size_bytes",
            "max_package_size_bytes",
            "max_file_count",
            "git_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class CoverageIssue:
    """A condition that affects how completely a package was captured."""

    code: str
    path: str | None
    detail: str
    blocking: bool = True

    @property
    def severity(self) -> str:
        return "BLOCKING" if self.blocking else "REVIEW"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "detail": self.detail,
            "blocking": self.blocking,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """One Git tree entry in the normalized package manifest.

    ``sha256`` is the hash of the raw blob for regular entries.  It is null
    for a symlink (where ``symlink_target`` is the identity value) and for
    entries whose blob was intentionally not read because a limit or a Git
    error prevented complete capture.  ``git_object_id`` remains available in
    those cases so that the incomplete digest is deterministic without
    pretending that an object ID is a SHA-256 content hash.
    """

    relative_path: str
    file_type: str
    mode: str
    size: int
    sha256: str | None = None
    symlink_target: str | None = None
    git_object_id: str | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        if self.file_type not in {"file", "binary", "lfs_pointer", "symlink", "submodule"}:
            raise ValueError(f"unsupported package entry type: {self.file_type}")
        if not re.fullmatch(r"(?:100644|100755|120000|160000)", self.mode):
            raise ValueError(f"unsupported Git mode: {self.mode}")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("package entry size must be a non-negative integer")
        if self.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("package entry sha256 must be a lower-case SHA-256")
        if self.file_type == "symlink" and self.symlink_target is None:
            # An unread symlink can still be represented in an incomplete
            # manifest, but callers must not accidentally confuse it with a
            # normal symlink whose target was captured.
            pass

    @property
    def is_complete(self) -> bool:
        return self.file_type == "symlink" and self.symlink_target is not None or (
            self.file_type != "symlink" and self.sha256 is not None
        )

    def digest_value(self) -> str | None:
        """Return the value used by the canonical digest for this entry."""

        if self.file_type == "symlink" and self.symlink_target is not None:
            return self.symlink_target
        if self.sha256 is not None:
            return self.sha256
        if self.git_object_id is not None:
            # This marker is deliberately distinct from a SHA-256.  It makes
            # an incomplete result reproducible while coverage_complete=False
            # prevents it from being used as a complete content version.
            return f"git-object:{self.git_object_id.lower()}"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "type": self.file_type,
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
            "symlink_target": self.symlink_target,
            "git_object_id": self.git_object_id,
        }


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """Result of exporting one immutable Skill Package revision."""

    repository: str
    source_revision: str
    skill_path: str
    snapshot_path: Path
    entries: tuple[PackageEntry, ...]
    skill_digest: str
    coverage_issues: tuple[CoverageIssue, ...] = field(default_factory=tuple)
    package_size_bytes: int = 0

    @property
    def package_manifest(self) -> tuple[PackageEntry, ...]:
        return self.entries

    @property
    def coverage_complete(self) -> bool:
        return not any(issue.blocking for issue in self.coverage_issues)

    @property
    def complete(self) -> bool:
        """Alias used by callers that treat coverage as a single gate."""

        return self.coverage_complete

    @property
    def requires_manual_review(self) -> bool:
        return bool(self.coverage_issues)

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def blocking_issues(self) -> tuple[CoverageIssue, ...]:
        return tuple(issue for issue in self.coverage_issues if issue.blocking)

    def manifest_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable package-manifest document."""

        return {
            "schema_version": "1",
            "repository": self.repository,
            "source_revision": self.source_revision,
            "skill_path": self.skill_path,
            "skill_digest": self.skill_digest,
            "coverage_complete": self.coverage_complete,
            "package_size_bytes": self.package_size_bytes,
            "file_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
            "coverage_issues": [issue.to_dict() for issue in self.coverage_issues],
        }

    def write_manifest(self, path: str | Path) -> Path:
        """Write a new JSON manifest without following or replacing a link."""

        target = _prepare_new_file_path(Path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        payload = json.dumps(
            self.manifest_dict(), ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        try:
            descriptor = os.open(str(target), flags, 0o600)
        except OSError as exc:
            raise SnapshotError(f"cannot create manifest {target}: {exc}") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            raise SnapshotError(f"cannot write manifest {target}: {exc}") from exc
        return target


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    relative_path: str
    mode: str
    git_type: str
    object_id: str
    size: int


def _validate_relative_path(path: str) -> str:
    """Validate a POSIX relative path before it is used on the filesystem."""

    if not isinstance(path, str) or not path:
        raise UnsafePathError("Git path must not be empty")
    if "\x00" in path or "\\" in path:
        raise UnsafePathError(f"Git path contains NUL or backslash: {path!r}")
    if path.startswith("/") or path.startswith("~"):
        raise UnsafePathError(f"Git path must be relative: {path!r}")
    if re.match(r"^[A-Za-z]:", path):
        raise UnsafePathError(f"Git path must not be a drive path: {path!r}")
    if any(char in _UNSAFE_CONTROL_CHARS for char in path):
        raise UnsafePathError(f"Git path contains a control character: {path!r}")
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise UnsafePathError(f"Git path contains an unsafe component: {path!r}")
    return path


def _decode_path(raw_path: bytes) -> str:
    try:
        decoded = raw_path.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise UnsafePathError("Git path is not valid UTF-8") from exc
    return _validate_relative_path(decoded)


def _resolve_repository(repository: str | Path) -> Path:
    if isinstance(repository, (str, Path)):
        path = Path(repository).expanduser()
    else:
        raise SnapshotError("repository must be a local filesystem path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GitSourceError(f"repository cannot be resolved: {path}: {exc}") from exc
    if not resolved.is_dir():
        raise GitSourceError(f"repository is not a directory: {resolved}")
    return resolved


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Git plumbing commands used below do not run hooks, but these settings
    # keep the command non-interactive and avoid a pager or lock side effects.
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    limits: SnapshotLimits,
) -> bytes:
    command = ["git", "-C", str(repository), *arguments]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=limits.git_timeout_seconds,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitSourceError(f"Git command failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise GitSourceError(
            f"Git command failed with exit {completed.returncode}: {detail or 'no details'}"
        )
    return completed.stdout


def _resolve_commit(
    repository: Path, source_revision: str, *, limits: SnapshotLimits
) -> str:
    if not isinstance(source_revision, str) or not _REVISION_RE.fullmatch(source_revision.strip()):
        raise GitSourceError("source_revision must be a complete 40- or 64-character Git object ID")
    revision = source_revision.strip().lower()
    resolved = _run_git(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        limits=limits,
    ).strip()
    try:
        canonical = resolved.decode("ascii").lower()
    except UnicodeDecodeError as exc:
        raise GitSourceError("Git returned a non-ASCII commit ID") from exc
    if not _REVISION_RE.fullmatch(canonical):
        raise GitSourceError("Git did not return a complete commit ID")
    return canonical


def _tree_entries(
    repository: Path,
    revision: str,
    skill_path: str,
    *,
    limits: SnapshotLimits,
) -> list[_TreeEntry]:
    arguments = [
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--long",
        "--full-tree",
        revision,
        "--",
    ]
    if skill_path != ".":
        arguments.append(skill_path)
    output = _run_git(repository, arguments, limits=limits)
    prefix = "" if skill_path == "." else skill_path + "/"
    entries: list[_TreeEntry] = []
    for record in output.split(b"\x00"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
        except ValueError as exc:
            raise GitSourceError("malformed git ls-tree record") from exc
        fields = header.split()
        if len(fields) != 4:
            raise GitSourceError("malformed git ls-tree header")
        raw_mode, raw_type, raw_object_id, raw_size = fields
        try:
            mode = raw_mode.decode("ascii")
            git_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii").lower()
            size_text = raw_size.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GitSourceError("git ls-tree returned a non-ASCII metadata field") from exc
        if not re.fullmatch(r"(?:100644|100755|120000|160000)", mode):
            raise GitSourceError(f"unsupported Git tree mode {mode!r}")
        if not _OBJECT_ID_RE.fullmatch(object_id):
            raise GitSourceError("git ls-tree returned an invalid object ID")
        if size_text == "-":
            size = 0
        else:
            try:
                size = int(size_text, 10)
            except ValueError as exc:
                raise GitSourceError("git ls-tree returned an invalid object size") from exc
            if size < 0:
                raise GitSourceError("git ls-tree returned a negative object size")
        full_path = _decode_path(raw_path)
        if prefix:
            if not full_path.startswith(prefix):
                raise GitSourceError("Git path escaped the requested Skill Root")
            relative_path = full_path[len(prefix) :]
        else:
            relative_path = full_path
        _validate_relative_path(relative_path)
        entries.append(_TreeEntry(relative_path, mode, git_type, object_id, size))
    entries.sort(key=lambda item: item.relative_path.encode("utf-8"))
    return entries


def _read_blob(
    repository: Path,
    object_id: str,
    *,
    limits: SnapshotLimits,
) -> bytes:
    return _run_git(repository, ["cat-file", "blob", object_id], limits=limits)


def _is_lfs_pointer(content: bytes) -> bool:
    lines = content.splitlines()
    if len(lines) < 3 or lines[0] != b"version " + _LFS_VERSION:
        return False
    has_oid = any(_LFS_OID_RE.fullmatch(line) for line in lines[1:])
    has_size = any(_LFS_SIZE_RE.fullmatch(line) for line in lines[1:])
    return has_oid and has_size


def _is_binary(content: bytes) -> bool:
    if b"\x00" in content:
        return True
    try:
        content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return True
    # Valid UTF-8 can still contain control bytes that are not normal text
    # formatting.  Treating these as binary makes scanner coverage explicit.
    return any(byte < 32 and byte not in {9, 10, 12, 13} or byte == 127 for byte in content)


def _decode_symlink_target(content: bytes, relative_path: str, issues: list[CoverageIssue]) -> str:
    if not content:
        issues.append(CoverageIssue("EMPTY_SYMLINK_TARGET", relative_path, "symlink target is empty"))
        return "base64:" + base64.b64encode(content).decode("ascii")
    try:
        target = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        issues.append(
            CoverageIssue(
                "INVALID_SYMLINK_TARGET",
                relative_path,
                "symlink target is not valid UTF-8",
            )
        )
        return "base64:" + base64.b64encode(content).decode("ascii")
    if any(char in _UNSAFE_CONTROL_CHARS for char in target):
        issues.append(
            CoverageIssue(
                "INVALID_SYMLINK_TARGET",
                relative_path,
                "symlink target contains a control character",
            )
        )
    if target.startswith("/") or target.startswith("\\") or re.match(r"^[A-Za-z]:", target):
        issues.append(
            CoverageIssue(
                "SYMLINK_OUTSIDE_ROOT",
                relative_path,
                "absolute or drive-qualified symlink target is not followed",
            )
        )
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative_path), target))
        if resolved == ".." or resolved.startswith("../"):
            issues.append(
                CoverageIssue(
                    "SYMLINK_OUTSIDE_ROOT",
                    relative_path,
                    "symlink target escapes the Skill Root and is not followed",
                )
            )
    return target


def _prepare_destination(path: Path) -> Path:
    try:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise SnapshotError(f"snapshot destination is not a directory: {path}")
            if any(path.iterdir()):
                raise SnapshotError(f"snapshot destination must be empty: {path}")
            return path.resolve()
        resolved = path.resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.mkdir()
        return resolved
    except OSError as exc:
        raise SnapshotError(f"cannot create snapshot destination {path}: {exc}") from exc


def _prepare_new_file_path(path: Path) -> Path:
    if path.is_symlink() or path.exists():
        raise SnapshotError(f"output path already exists: {path}")
    return path.expanduser().resolve(strict=False)


def _write_blob(destination: Path, relative_path: str, content: bytes, mode: str) -> None:
    _validate_relative_path(relative_path)
    target = destination.joinpath(*relative_path.split("/"))
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    # The destination is created empty, but explicitly reject a link in any
    # component in case a caller races this process or reuses a directory.
    current = destination
    for component in relative_path.split("/")[:-1]:
        current = current / component
        if current.is_symlink():
            raise UnsafePathError(f"snapshot path component is a symlink: {relative_path}")
    if target.is_symlink() or target.exists():
        raise SnapshotError(f"snapshot target already exists: {target}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(target), flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.chmod(target, 0o755 if mode == "100755" else 0o644)
    except OSError as exc:
        raise SnapshotError(f"cannot write snapshot file {target}: {exc}") from exc


def _canonical_manifest(entries: Iterable[PackageEntry]) -> list[dict[str, Any]]:
    ordered = sorted(entries, key=lambda entry: entry.relative_path.encode("utf-8"))
    return [
        {
            "relative_path": entry.relative_path,
            # Binary and LFS-pointer labels describe review coverage, not
            # content identity.  The canonical type remains ``file`` so a
            # future classifier-version change cannot alter the digest for
            # identical Git bytes.
            "type": (
                entry.file_type
                if entry.file_type in {"symlink", "submodule"}
                else "file"
            ),
            "mode": entry.mode,
            "hash_or_target": entry.digest_value(),
        }
        for entry in ordered
    ]


def canonical_manifest_json(entries: Iterable[PackageEntry]) -> bytes:
    """Return the exact canonical JSON bytes used for ``skill_digest``."""

    return json.dumps(
        _canonical_manifest(entries),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def calculate_skill_digest(entries: Iterable[PackageEntry]) -> str:
    """Calculate the SHA-256 digest of a normalized package manifest."""

    return hashlib.sha256(canonical_manifest_json(entries)).hexdigest()


def export_skill_archive_snapshot(
    archive: str | Path,
    repository: str,
    source_revision: str,
    skill_path: str,
    destination: str | Path,
    *,
    limits: SnapshotLimits | None = None,
) -> SnapshotResult:
    """Safely materialize a single Skill returned by ``git archive --remote``.

    Archive members are parsed individually; ``extractall`` is deliberately
    not used.  Paths, modes, sizes and symlinks receive the same treatment as
    the local Git-object snapshot path, so scanners never follow links.
    """

    active_limits = limits or SnapshotLimits()
    normalized_path = normalize_skill_path(skill_path)
    revision = source_revision.strip().lower()
    if not _REVISION_RE.fullmatch(revision):
        raise GitSourceError("source_revision must be a complete 40- or 64-character Git object ID")
    archive_path = Path(archive).expanduser().resolve(strict=True)
    if not archive_path.is_file():
        raise SnapshotError(f"remote Skill archive is not a file: {archive_path}")
    destination_path = _prepare_destination(Path(destination).expanduser())
    prefix = "" if normalized_path == "." else normalized_path + "/"
    entries: list[PackageEntry] = []
    issues: list[CoverageIssue] = []
    seen: set[str] = set()
    directories: set[str] = set()
    copied_size = 0
    declared_size = 0

    try:
        handle = tarfile.open(archive_path, mode="r:")
    except (OSError, tarfile.TarError) as exc:
        raise SnapshotError(f"cannot read remote Skill archive: {exc}") from exc
    with handle:
        for member in handle:
            name = member.name.rstrip("/")
            if not name:
                continue
            _validate_relative_path(name)
            if member.isdir() and normalized_path.startswith(name + "/"):
                # git archive includes parent directory headers before the
                # requested subtree; they contain no package content.
                continue
            if normalized_path != "." and name == normalized_path and member.isdir():
                continue
            if prefix:
                if not name.startswith(prefix):
                    raise UnsafePathError("remote archive member escaped the requested Skill Root")
                relative = name[len(prefix) :]
            else:
                relative = name
            _validate_relative_path(relative)
            if member.isdir():
                directories.add(relative)
                continue
            if relative in seen:
                raise SnapshotError(f"remote archive contains a duplicate path: {relative}")
            seen.add(relative)
            declared_size += member.size

            if member.issym():
                target_bytes = member.linkname.encode("utf-8", "surrogateescape")
                target = _decode_symlink_target(target_bytes, relative, issues)
                entries.append(
                    PackageEntry(relative, "symlink", "120000", len(target_bytes), symlink_target=target)
                )
                issues.append(
                    CoverageIssue(
                        "SYMLINK_NOT_FOLLOWED",
                        relative,
                        "symlink target is recorded in the manifest and never followed",
                        blocking=False,
                    )
                )
                continue
            if not member.isfile():
                issues.append(
                    CoverageIssue(
                        "UNSUPPORTED_ARCHIVE_ENTRY",
                        relative,
                        f"remote archive entry type {member.type!r} was not materialized",
                    )
                )
                continue

            mode = "100755" if member.mode & 0o111 else "100644"
            if member.size > active_limits.max_file_size_bytes:
                entries.append(PackageEntry(relative, "file", mode, member.size))
                issues.append(
                    CoverageIssue(
                        "FILE_TOO_LARGE",
                        relative,
                        f"file is {member.size} bytes; limit is {active_limits.max_file_size_bytes}",
                    )
                )
                continue
            if copied_size + member.size > active_limits.max_package_size_bytes:
                entries.append(PackageEntry(relative, "file", mode, member.size))
                issues.append(
                    CoverageIssue(
                        "PACKAGE_LIMIT_EXCEEDED",
                        relative,
                        "entry was not read after the package byte limit was reached",
                    )
                )
                continue
            source = handle.extractfile(member)
            if source is None:
                raise SnapshotError(f"cannot read remote archive member: {relative}")
            content = source.read(member.size + 1)
            if len(content) != member.size:
                raise SnapshotError(f"remote archive member size mismatch: {relative}")
            copied_size += len(content)
            file_type = (
                "lfs_pointer"
                if _is_lfs_pointer(content)
                else "binary"
                if _is_binary(content)
                else "file"
            )
            if file_type == "lfs_pointer":
                issues.append(
                    CoverageIssue(
                        "LFS_POINTER",
                        relative,
                        "blob is a Git LFS pointer; the real LFS object was not included",
                    )
                )
            elif file_type == "binary":
                issues.append(
                    CoverageIssue(
                        "BINARY_FILE",
                        relative,
                        "binary bytes were captured; semantic scanner coverage requires review",
                        blocking=False,
                    )
                )
            _write_blob(destination_path, relative, content, mode)
            entries.append(
                PackageEntry(
                    relative,
                    file_type,
                    mode,
                    len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )

    # JGit represents a Git submodule as an empty directory in an archive.
    # Git cannot track ordinary empty directories, so a deepest directory
    # without any archived member is an omitted gitlink and must block review.
    for directory in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        has_file = any(path.startswith(directory + "/") for path in seen)
        has_child_directory = any(
            child != directory and child.startswith(directory + "/")
            for child in directories
        )
        if not has_file and not has_child_directory:
            entries.append(PackageEntry(directory, "submodule", "160000", 0))
            issues.append(
                CoverageIssue(
                    "SUBMODULE_NOT_INCLUDED",
                    directory,
                    "remote archive exposed an empty gitlink directory; submodule content is not included",
                )
            )

    entry_count = len(entries)
    if entry_count > active_limits.max_file_count:
        issues.append(
            CoverageIssue(
                "FILE_COUNT_EXCEEDED",
                None,
                f"package contains {entry_count} entries; limit is {active_limits.max_file_count}",
            )
        )
    if declared_size > active_limits.max_package_size_bytes:
        issues.append(
            CoverageIssue(
                "PACKAGE_TOO_LARGE",
                None,
                f"package is {declared_size} bytes; limit is {active_limits.max_package_size_bytes}",
            )
        )
    skill_entry = next((entry for entry in entries if entry.relative_path == "SKILL.md"), None)
    if skill_entry is None:
        issues.append(
            CoverageIssue(
                "MISSING_SKILL_MD",
                None,
                "requested Skill Root does not contain an exact SKILL.md entry",
            )
        )
    elif skill_entry.file_type == "symlink":
        issues.append(
            CoverageIssue(
                "SKILL_MD_NOT_REGULAR",
                "SKILL.md",
                "SKILL.md is not a regular readable file",
            )
        )
    elif skill_entry.sha256 is None:
        issues.append(
            CoverageIssue(
                "SKILL_MD_UNAVAILABLE",
                "SKILL.md",
                "SKILL.md exists in the archive but its content was not captured",
            )
        )
    elif skill_entry.file_type == "lfs_pointer":
        issues.append(
            CoverageIssue(
                "SKILL_MD_LFS_POINTER",
                "SKILL.md",
                "SKILL.md is an LFS pointer rather than the real content",
            )
        )
    for entry in entries:
        if entry.relative_path != "SKILL.md" and entry.relative_path.rsplit("/", 1)[-1] == "SKILL.md":
            issues.append(
                CoverageIssue(
                    "NESTED_SKILL_MD",
                    entry.relative_path,
                    "nested SKILL.md detected; review it as a separate Skill Root",
                    blocking=False,
                )
            )
    entries.sort(key=lambda entry: entry.relative_path.encode("utf-8"))
    digest = calculate_skill_digest(entries)
    return SnapshotResult(
        repository,
        revision,
        normalized_path,
        destination_path,
        tuple(entries),
        digest,
        tuple(issues),
        declared_size,
    )


def export_skill_snapshot(
    repository: str | Path,
    source_revision: str,
    skill_path: str,
    destination: str | Path,
    *,
    limits: SnapshotLimits | None = None,
    manifest_path: str | Path | None = None,
) -> SnapshotResult:
    """Export a Skill Package from ``source_revision`` without checkout.

    ``source_revision`` must be a complete commit object ID.  The requested
    Skill Root is normalized and passed as a literal Git pathspec.  Regular
    blobs are copied with their Git mode; symlink and submodule entries are
    never materialized.  The returned result must be checked for
    ``coverage_complete`` before it is considered reviewable.
    """

    active_limits = limits or SnapshotLimits()
    normalized_path = normalize_skill_path(skill_path)
    repo_path = _resolve_repository(repository)
    revision = _resolve_commit(repo_path, source_revision, limits=active_limits)
    tree = _tree_entries(repo_path, revision, normalized_path, limits=active_limits)
    destination_path = _prepare_destination(Path(destination).expanduser())

    issues: list[CoverageIssue] = []
    declared_package_size = sum(item.size for item in tree)
    if len(tree) > active_limits.max_file_count:
        issues.append(
            CoverageIssue(
                "FILE_COUNT_EXCEEDED",
                None,
                f"package contains {len(tree)} entries; limit is {active_limits.max_file_count}",
            )
        )
    if declared_package_size > active_limits.max_package_size_bytes:
        issues.append(
            CoverageIssue(
                "PACKAGE_TOO_LARGE",
                None,
                f"package is {declared_package_size} bytes; limit is {active_limits.max_package_size_bytes}",
            )
        )

    exported: list[PackageEntry] = []
    copied_package_size = 0
    for item in tree:
        if item.mode == "160000" or item.git_type == "commit":
            exported.append(
                PackageEntry(
                    item.relative_path,
                    "submodule",
                    item.mode,
                    item.size,
                    git_object_id=item.object_id,
                )
            )
            issues.append(
                CoverageIssue(
                    "SUBMODULE_NOT_INCLUDED",
                    item.relative_path,
                    "submodule commit is recorded but its contents are not followed",
                )
            )
            continue

        is_symlink = item.mode == "120000"
        if item.size > active_limits.max_file_size_bytes:
            issues.append(
                CoverageIssue(
                    "FILE_TOO_LARGE",
                    item.relative_path,
                    f"file is {item.size} bytes; limit is {active_limits.max_file_size_bytes}",
                )
            )
            exported.append(
                PackageEntry(
                    item.relative_path,
                    "symlink" if is_symlink else "file",
                    item.mode,
                    item.size,
                    git_object_id=item.object_id,
                )
            )
            continue
        if copied_package_size + item.size > active_limits.max_package_size_bytes:
            issues.append(
                CoverageIssue(
                    "PACKAGE_LIMIT_EXCEEDED",
                    item.relative_path,
                    "entry was not read after the package byte limit was reached",
                )
            )
            exported.append(
                PackageEntry(
                    item.relative_path,
                    "symlink" if is_symlink else "file",
                    item.mode,
                    item.size,
                    git_object_id=item.object_id,
                )
            )
            continue
        try:
            content = _read_blob(repo_path, item.object_id, limits=active_limits)
        except GitSourceError as exc:
            issues.append(CoverageIssue("BLOB_READ_FAILED", item.relative_path, str(exc)))
            exported.append(
                PackageEntry(
                    item.relative_path,
                    "symlink" if is_symlink else "file",
                    item.mode,
                    item.size,
                    git_object_id=item.object_id,
                )
            )
            continue
        copied_package_size += len(content)
        if len(content) != item.size:
            issues.append(
                CoverageIssue(
                    "BLOB_SIZE_MISMATCH",
                    item.relative_path,
                    f"Git declared {item.size} bytes but returned {len(content)} bytes",
                )
            )

        if is_symlink:
            target = _decode_symlink_target(content, item.relative_path, issues)
            issues.append(
                CoverageIssue(
                    "SYMLINK_NOT_FOLLOWED",
                    item.relative_path,
                    "symlink target is recorded in the manifest and never followed",
                    blocking=False,
                )
            )
            exported.append(
                PackageEntry(
                    item.relative_path,
                    "symlink",
                    item.mode,
                    item.size,
                    symlink_target=target,
                    git_object_id=item.object_id,
                )
            )
            continue

        if _is_lfs_pointer(content):
            file_type = "lfs_pointer"
            issues.append(
                CoverageIssue(
                    "LFS_POINTER",
                    item.relative_path,
                    "blob is a Git LFS pointer; the real LFS object was not included",
                )
            )
        elif _is_binary(content):
            file_type = "binary"
            issues.append(
                CoverageIssue(
                    "BINARY_FILE",
                    item.relative_path,
                    "binary bytes were captured; semantic scanner coverage requires review",
                    blocking=False,
                )
            )
        else:
            file_type = "file"
        digest = hashlib.sha256(content).hexdigest()
        _write_blob(destination_path, item.relative_path, content, item.mode)
        exported.append(
            PackageEntry(
                item.relative_path,
                file_type,
                item.mode,
                item.size,
                sha256=digest,
                git_object_id=item.object_id,
            )
        )

    skill_entry = next(
        (entry for entry in exported if entry.relative_path == "SKILL.md"), None
    )
    if skill_entry is None:
        # A too-large/unreadable SKILL.md is still listed in the tree and must
        # be distinguished from a missing marker.
        if any(item.relative_path == "SKILL.md" for item in tree):
            issues.append(
                CoverageIssue(
                    "SKILL_MD_UNAVAILABLE",
                    "SKILL.md",
                    "SKILL.md exists in Git but its content was not captured",
                )
            )
        else:
            issues.append(
                CoverageIssue(
                    "MISSING_SKILL_MD",
                    None,
                    "requested Skill Root does not contain an exact SKILL.md entry",
                )
            )
    elif skill_entry.file_type in {"symlink", "submodule"}:
        issues.append(
            CoverageIssue(
                "SKILL_MD_NOT_REGULAR",
                skill_entry.relative_path,
                "SKILL.md is not a regular readable file",
            )
        )
    elif skill_entry.sha256 is None:
        issues.append(
            CoverageIssue(
                "SKILL_MD_UNAVAILABLE",
                skill_entry.relative_path,
                "SKILL.md exists in Git but its content was not captured",
            )
        )
    elif skill_entry.file_type == "lfs_pointer":
        issues.append(
            CoverageIssue(
                "SKILL_MD_LFS_POINTER",
                skill_entry.relative_path,
                "SKILL.md is an LFS pointer rather than the real content",
            )
        )
    for entry in exported:
        if entry.relative_path != "SKILL.md" and entry.relative_path.rsplit("/", 1)[-1] == "SKILL.md":
            issues.append(
                CoverageIssue(
                    "NESTED_SKILL_MD",
                    entry.relative_path,
                    "nested SKILL.md detected; review it as a separate Skill Root",
                    blocking=False,
                )
            )
    exported.sort(key=lambda entry: entry.relative_path.encode("utf-8"))
    digest = calculate_skill_digest(exported)
    result = SnapshotResult(
        repository=str(repo_path),
        source_revision=revision,
        skill_path=normalized_path,
        snapshot_path=destination_path,
        entries=tuple(exported),
        skill_digest=digest,
        coverage_issues=tuple(issues),
        package_size_bytes=declared_package_size,
    )
    if manifest_path is not None:
        result.write_manifest(manifest_path)
    return result


__all__ = [
    "CoverageIssue",
    "GitSourceError",
    "PackageEntry",
    "SnapshotError",
    "SnapshotLimits",
    "SnapshotResult",
    "UnsafePathError",
    "calculate_skill_digest",
    "canonical_manifest_json",
    "export_skill_archive_snapshot",
    "export_skill_snapshot",
]
