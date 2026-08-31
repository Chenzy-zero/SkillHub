"""Safe evidence writes and local private-candidate export.

This module is the boundary between review results and files that are kept on
disk.  It intentionally does not know how a scanner or a model was run.  A
caller must provide an explicit final eligibility decision and a snapshot
whose source revision and content digest are frozen.  The module only writes
local files; it never invokes Git, a scanner, a model, or a network client.

Two storage areas are kept separate:

``EvidenceStore``
    Writes append-only-style evidence files below
    ``restricted-evidence/<batch_id>/<task_id>/``.  JSON and text are written
    through a same-directory temporary file, an ``fsync`` and an atomic
    rename.  Existing different evidence is never silently replaced.

``export_private_candidate``
    Copies only the verified package files to
    ``<candidate-root>/<repo-slug>/<path-slug>/<digest>/package`` and writes a
    small source manifest and review summary.  Scanner reports are never
    copied as candidate metadata.  Before the final rename, every copied file
    is hashed again and the snapshot digest is recomputed.

The source snapshot is treated as untrusted input.  Symlinks, special files,
path traversal, and source/candidate/evidence directory overlap are rejected.
The caller remains responsible for policy decisions; an ``eligible=True``
value is required and is never inferred from a score or from a scanner field.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import normalize_skill_path


class ArtifactError(ValueError):
    """Base class for an unsafe or incomplete artifact operation."""


class ArtifactPathError(ArtifactError):
    """A path is absolute, traverses its root, or contains a symlink."""


class ArtifactWriteError(ArtifactError):
    """An artifact could not be written atomically or would be overwritten."""


class ArtifactIntegrityError(ArtifactError):
    """An evidence source is not the regular immutable file expected."""


class CandidateNotEligibleError(ArtifactError):
    """The caller did not provide an explicit eligible decision."""


class DigestMismatchError(ArtifactError):
    """The package, result, or requested content digest does not match."""


class RevisionMismatchError(ArtifactError):
    """The requested source revision does not match the frozen snapshot."""


class CandidateIntegrityError(ArtifactIntegrityError):
    """A candidate package contains unexpected or unsafe content."""


_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _absolute(path: str | os.PathLike[str]) -> Path:
    value = Path(path).expanduser()
    if "\x00" in os.fspath(value):
        raise ArtifactPathError("path must not contain NUL")
    # ``absolute`` is deliberately lexical.  ``resolve`` is used separately
    # for overlap checks, so an existing source symlink cannot hide a path
    # traversal at this stage.
    return Path(os.path.abspath(os.fspath(value)))


def _validate_relative_path(value: str | os.PathLike[str]) -> tuple[str, ...]:
    """Validate an artifact-relative POSIX path and return its components."""

    text = os.fspath(value)
    if not isinstance(text, str) or not text:
        raise ArtifactPathError("relative artifact path must not be empty")
    if "\x00" in text or "\\" in text:
        raise ArtifactPathError(f"relative artifact path is unsafe: {text!r}")
    if text.startswith("/") or text.startswith("~") or re.match(r"^[A-Za-z]:", text):
        raise ArtifactPathError(f"artifact path must be relative: {text!r}")
    components = text.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ArtifactPathError(f"artifact path contains an unsafe component: {text!r}")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in component) for component in components):
        raise ArtifactPathError(f"artifact path contains a control character: {text!r}")
    return tuple(components)


def _ensure_directory(path: Path) -> Path:
    """Create a directory and reject a symlink at its final component."""

    path = _absolute(path)
    try:
        if path.is_symlink():
            raise ArtifactPathError(f"directory must not be a symlink: {path}")
        if path.exists():
            if not path.is_dir():
                raise ArtifactPathError(f"path is not a directory: {path}")
        else:
            path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ArtifactPathError(f"directory became a symlink: {path}")
    except OSError as exc:
        raise ArtifactWriteError(f"cannot prepare directory {path}: {exc}") from exc
    return path.resolve()


def _check_no_symlink_components(root: Path, components: Sequence[str]) -> Path:
    """Resolve a child path while rejecting symlinks below ``root``."""

    root = _ensure_directory(root)
    current = root
    for component in components:
        current = current / component
        try:
            if current.is_symlink():
                raise ArtifactPathError(f"artifact path component is a symlink: {current}")
        except OSError as exc:
            raise ArtifactPathError(f"cannot inspect artifact path {current}: {exc}") from exc
    return current


def safe_join(root: str | os.PathLike[str], relative: str | os.PathLike[str]) -> Path:
    """Return a path below ``root`` after lexical and symlink checks.

    This helper is public because callers use it to construct report paths
    before passing them to an evidence writer.  It does not create the target.
    """

    components = _validate_relative_path(relative)
    root_abs = _ensure_directory(Path(root))
    target = _check_no_symlink_components(root_abs, components)
    try:
        if not target.resolve(strict=False).is_relative_to(root_abs.resolve()):
            raise ArtifactPathError(f"artifact path escapes root: {relative!r}")
    except OSError as exc:
        raise ArtifactPathError(f"cannot resolve artifact path {target}: {exc}") from exc
    return target


def _path_overlap(left: Path, right: Path) -> bool:
    """Return whether two roots overlap after resolving existing links."""

    left_abs = _absolute(left)
    right_abs = _absolute(right)
    try:
        left_real = left_abs.resolve(strict=False)
        right_real = right_abs.resolve(strict=False)
    except OSError as exc:
        raise ArtifactPathError(f"cannot resolve artifact roots: {exc}") from exc
    return (
        left_abs == right_abs
        or left_abs.is_relative_to(right_abs)
        or right_abs.is_relative_to(left_abs)
        or left_real == right_real
        or left_real.is_relative_to(right_real)
        or right_real.is_relative_to(left_real)
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hash a regular file without following a symlink at open time."""

    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise ArtifactWriteError(f"cannot stat artifact file {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ArtifactIntegrityError(f"artifact source is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise ArtifactWriteError(f"cannot open artifact file {path}: {exc}") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    except OSError as exc:
        raise ArtifactWriteError(f"cannot read artifact file {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    return digest.hexdigest(), total


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync on platforms that support it."""

    try:
        descriptor = os.open(os.fspath(path), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            return
    finally:
        os.close(descriptor)


def _atomic_write_bytes(
    root: Path,
    relative: str | os.PathLike[str],
    data: bytes,
    *,
    mode: int = 0o600,
    refuse_different_existing: bool = False,
) -> Path:
    """Write bytes to a child path using a same-directory atomic rename."""

    target = safe_join(root, relative)
    parent = _ensure_directory(target.parent)
    try:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ArtifactPathError(f"artifact target is not a regular file: {target}")
        if refuse_different_existing and target.exists():
            existing_digest, existing_size = _sha256_file(target)
            if existing_size != len(data) or existing_digest != _sha256_bytes(data):
                raise ArtifactWriteError(f"refusing to overwrite existing artifact: {target}")
            return target
    except OSError as exc:
        raise ArtifactWriteError(f"cannot inspect artifact target {target}: {exc}") from exc

    temporary: Path | None = None
    descriptor: int | None = None
    try:
        for _ in range(20):
            name = f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
            candidate = parent / name
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(os.fspath(candidate), flags, mode)
                temporary = candidate
                break
            except FileExistsError:
                continue
        if descriptor is None or temporary is None:
            raise ArtifactWriteError(f"cannot allocate temporary artifact next to {target}")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ArtifactWriteError(f"short write while writing {target}")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(os.fspath(temporary), os.fspath(target))
        temporary = None
        _fsync_directory(parent)
        return target
    except ArtifactError:
        raise
    except OSError as exc:
        raise ArtifactWriteError(f"cannot atomically write artifact {target}: {exc}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Reference to one immutable evidence file."""

    path: Path
    relative_path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


class EvidenceStore:
    """Write evidence below one batch/task directory."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        batch_id: str,
        task_id: str,
        *,
        candidate_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = _ensure_directory(Path(root))
        self.batch_id = self._identifier(batch_id, "batch_id")
        self.task_id = self._identifier(task_id, "task_id")
        self.task_root = safe_join(self.root, f"{self.batch_id}/{self.task_id}")
        if candidate_root is not None and _path_overlap(self.root, Path(candidate_root)):
            raise ArtifactPathError("restricted evidence and candidate roots must be separate")

    @staticmethod
    def _identifier(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ArtifactPathError(f"{field_name} must not be empty")
        value = value.strip()
        if not _SAFE_COMPONENT_RE.fullmatch(value) or value in {".", ".."}:
            raise ArtifactPathError(f"{field_name} must be one safe path component")
        return value

    def write_json(self, relative_path: str | os.PathLike[str], payload: Any) -> EvidenceReference:
        """Serialize JSON deterministically and write it atomically."""

        try:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ArtifactWriteError(f"cannot serialize JSON evidence: {exc}") from exc
        return self._write(relative_path, data)

    def write_text(
        self,
        relative_path: str | os.PathLike[str],
        text: str,
        *,
        encoding: str = "utf-8",
    ) -> EvidenceReference:
        """Write text evidence atomically without interpreting its contents."""

        if not isinstance(text, str):
            raise ArtifactWriteError("text evidence must be a string")
        try:
            data = text.encode(encoding)
        except (LookupError, UnicodeError) as exc:
            raise ArtifactWriteError(f"cannot encode text evidence: {exc}") from exc
        return self._write(relative_path, data)

    def _write(self, relative_path: str | os.PathLike[str], data: bytes) -> EvidenceReference:
        relative = "/".join(_validate_relative_path(relative_path))
        # Evidence is an audit record.  Retries may observe the same bytes,
        # but a later run must not replace a different record at the same
        # path.  The compare-before-write is performed before the atomic
        # rename, so a failed retry cannot damage the previous evidence.
        path = _atomic_write_bytes(
            self.task_root,
            relative,
            data,
            refuse_different_existing=True,
        )
        digest, size = _sha256_file(path)
        return EvidenceReference(path, relative, digest, size)

    # Explicit names make it difficult for a caller to mistake these writes
    # for an in-place update.  Both aliases keep the same atomic behavior.
    write_json_atomic = write_json
    write_text_atomic = write_text

    def copy_raw_report(
        self,
        source: str | os.PathLike[str],
        relative_path: str | os.PathLike[str] | None = None,
        *,
        scanner: str | None = None,
    ) -> EvidenceReference:
        """Copy one raw report exactly into the restricted evidence area.

        A report may be addressed either by an explicit relative path (for
        example ``cisco/raw-report.json``) or by ``scanner="cisco"``.  A
        different existing report is never overwritten; an identical one is
        treated as an idempotent retry.
        """

        if relative_path is None:
            if scanner is None:
                raise ArtifactPathError("relative_path or scanner must be provided")
            scanner_value = self._identifier(scanner, "scanner")
            relative_path = f"{scanner_value}/raw-report.json"
        relative = "/".join(_validate_relative_path(relative_path))
        source_path = _absolute(source)
        try:
            source_stat = os.lstat(source_path)
        except OSError as exc:
            raise ArtifactWriteError(f"cannot stat raw report {source_path}: {exc}") from exc
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise ArtifactIntegrityError(f"raw report source is not a regular file: {source_path}")

        target = safe_join(self.task_root, relative)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise ArtifactPathError(f"raw report target is not a regular file: {target}")
            source_digest, source_size = _sha256_file(source_path)
            target_digest, target_size = _sha256_file(target)
            if source_digest != target_digest or source_size != target_size:
                raise ArtifactWriteError(f"refusing to overwrite different raw report: {target}")
            return EvidenceReference(target, relative, target_digest, target_size)

        parent = _ensure_directory(target.parent)
        temporary: Path | None = None
        descriptor: int | None = None
        source_descriptor: int | None = None
        digest = hashlib.sha256()
        total = 0
        try:
            source_descriptor = os.open(
                os.fspath(source_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            for _ in range(20):
                name = f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
                candidate = parent / name
                try:
                    descriptor = os.open(
                        os.fspath(candidate),
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if descriptor is None or temporary is None:
                raise ArtifactWriteError(f"cannot allocate temporary report next to {target}")
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise ArtifactWriteError(f"short write while copying raw report {source_path}")
                    view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.close(source_descriptor)
            source_descriptor = None
            os.replace(os.fspath(temporary), os.fspath(target))
            temporary = None
            _fsync_directory(parent)
            return EvidenceReference(target, relative, digest.hexdigest(), total)
        except ArtifactError:
            raise
        except OSError as exc:
            raise ArtifactWriteError(f"cannot preserve raw report {target}: {exc}") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

            if source_descriptor is not None:
                try:
                    os.close(source_descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    # Names used by orchestration code that treats reports and generic files
    # uniformly.  They intentionally retain the same immutable/idempotent
    # semantics as ``copy_raw_report``.
    copy_report = copy_raw_report


@dataclass(frozen=True, slots=True)
class CandidateExportRequest:
    """Inputs required for a private candidate export.

    ``eligible`` is deliberately mandatory and must be exactly ``True``.  No
    implementation detail such as a quality score or scanner decision can
    infer this flag.
    """

    snapshot: Any
    repository: str
    skill_path: str
    source_revision: str
    skill_digest: str
    eligible: bool
    branch: str | None = None
    skill_name: str | None = None
    security_decision: str | None = None
    quality_score: int | float | None = None
    evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateExportResult:
    """Paths and integrity values produced by a local candidate export."""

    candidate_path: Path
    package_path: Path
    source_manifest_path: Path
    review_summary_path: Path
    verification_path: Path
    repository: str
    skill_path: str
    source_revision: str
    skill_digest: str
    verified_digest: str
    created: bool

    @property
    def path(self) -> Path:
        """Short alias for the candidate directory."""

        return self.candidate_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_path": str(self.candidate_path),
            "package_path": str(self.package_path),
            "source_manifest_path": str(self.source_manifest_path),
            "review_summary_path": str(self.review_summary_path),
            "verification_path": str(self.verification_path),
            "repository": self.repository,
            "skill_path": self.skill_path,
            "source_revision": self.source_revision,
            "skill_digest": self.skill_digest,
            "verified_digest": self.verified_digest,
            "created": self.created,
        }


def _normalize_digest(value: Any, field_name: str = "skill_digest") -> str:
    if not isinstance(value, str) or not _HEX_SHA256_RE.fullmatch(value.strip()):
        raise DigestMismatchError(f"{field_name} must be a 64-character SHA-256")
    return value.strip().lower()


def _normalize_revision(value: Any, field_name: str = "source_revision") -> str:
    if not isinstance(value, str) or not _HEX_REVISION_RE.fullmatch(value.strip()):
        raise RevisionMismatchError(f"{field_name} must be a complete Git revision")
    return value.strip().lower()


def _safe_slug(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactPathError(f"{field_name} must not be empty")
    text = value.strip()
    if "\x00" in text or "\\" in text or text == "..":
        raise ArtifactPathError(f"{field_name} contains an unsafe path value")
    # Keep repository/path visible while ensuring that one value maps to one
    # filesystem component.  The digest suffix prevents collisions caused by
    # punctuation normalisation (for example ``a/b`` and ``a-b``).
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "item"
    suffix = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{suffix}"


def _normalize_repository_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactPathError("repository must not be empty")
    text = value.strip()
    if (
        "\x00" in text
        or "\\" in text
        or text.startswith("/")
        or text.endswith("/")
        or "//" in text
        or "://" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise ArtifactPathError(f"repository must be a normalized project path: {value!r}")
    return text


def _snapshot_values(snapshot: Any) -> tuple[Path, str, str, tuple[Any, ...], bool]:
    """Extract the minimum immutable facts from a SnapshotResult-like value."""

    required = ("snapshot_path", "source_revision", "skill_digest", "entries")
    missing = [name for name in required if not hasattr(snapshot, name)]
    if missing:
        raise CandidateIntegrityError(
            "snapshot must provide " + ", ".join(required) + f"; missing {', '.join(missing)}"
        )
    root = _absolute(getattr(snapshot, "snapshot_path"))
    revision = _normalize_revision(getattr(snapshot, "source_revision"), "snapshot.source_revision")
    digest = _normalize_digest(getattr(snapshot, "skill_digest"), "snapshot.skill_digest")
    entries = tuple(getattr(snapshot, "entries"))
    coverage_complete = bool(getattr(snapshot, "coverage_complete", True))
    return root, revision, digest, entries, coverage_complete


def _entry_value(entry: Any, name: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        if name in entry:
            return entry[name]
        aliases = {"relative_path": ("path",), "file_type": ("type",), "sha256": ("file_sha256",)}
        for alias in aliases.get(name, ()):
            if alias in entry:
                return entry[alias]
        return default
    return getattr(entry, name, default)


def _manifest_entry_data(entry: Any) -> dict[str, Any]:
    relative = _entry_value(entry, "relative_path")
    if not isinstance(relative, str):
        raise CandidateIntegrityError("snapshot manifest entry has no relative_path")
    relative = "/".join(_validate_relative_path(relative))
    file_type = str(_entry_value(entry, "file_type", _entry_value(entry, "type", "file")))
    mode = str(_entry_value(entry, "mode", "100644"))
    size = _entry_value(entry, "size", 0)
    sha256 = _entry_value(entry, "sha256", _entry_value(entry, "file_sha256"))
    target = _entry_value(entry, "symlink_target")
    if file_type in {"symlink", "submodule"}:
        raise CandidateIntegrityError(
            f"candidate export does not materialize unsafe package entry {relative} ({file_type})"
        )
    if file_type not in {"file", "binary", "lfs_pointer"}:
        # A SnapshotResult currently emits exactly the three regular content
        # types above; refusing unknown types avoids silently dropping a file.
        raise CandidateIntegrityError(f"unsupported package entry type at {relative}: {file_type}")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise CandidateIntegrityError(f"invalid package entry size at {relative}")
    if not isinstance(mode, str) or mode not in {"100644", "100755"}:
        raise CandidateIntegrityError(f"invalid regular-file mode at {relative}: {mode!r}")
    if not isinstance(sha256, str) or not _HEX_SHA256_RE.fullmatch(sha256):
        raise CandidateIntegrityError(f"missing or invalid package hash at {relative}")
    return {
        "relative_path": relative,
        "file_type": file_type,
        "mode": mode,
        "size": size,
        "sha256": sha256.lower(),
        "symlink_target": target,
    }


def _canonical_digest(entries: Iterable[Mapping[str, Any]]) -> str:
    values = [
        {
            "relative_path": item["relative_path"],
            # ``binary`` and ``lfs_pointer`` are coverage labels.  Snapshot
            # content identity classifies all materialized blobs as files so
            # scanner/classifier-version changes cannot alter the digest.
            "type": "file",
            "mode": item["mode"],
            "hash_or_target": item["sha256"],
        }
        for item in entries
    ]
    values.sort(key=lambda item: item["relative_path"].encode("utf-8"))
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _copy_regular_file(
    source: Path,
    destination: Path,
    expected: Mapping[str, Any],
    *,
    package_root: Path,
) -> str:
    """Copy and hash one expected file, rejecting links and special files."""

    try:
        source_stat = os.lstat(source)
    except OSError as exc:
        raise CandidateIntegrityError(f"snapshot file is missing: {source}") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise CandidateIntegrityError(f"snapshot file is not a regular file: {source}")
    expected_mode = 0o755 if expected["mode"] == "100755" else 0o644
    if stat.S_IMODE(source_stat.st_mode) not in {0o644, 0o755}:
        # Git's manifest only tracks the executable bit.  Non-standard local
        # modes are not copied into a candidate because they would invalidate
        # the source package's declared mode.
        raise CandidateIntegrityError(f"snapshot file has an unsupported mode: {source}")
    # ``destination`` is always below the staging root, but checking the full
    # parent chain before and after mkdir prevents a pre-existing link in a
    # nested package path from being followed.  The second check also makes a
    # concurrent replacement visible before the file is opened.
    relative_parent = destination.parent.relative_to(package_root)
    parent_components = tuple(relative_parent.parts)
    _check_no_symlink_components(package_root, parent_components)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_no_symlink_components(package_root, parent_components)
    if destination.exists() or destination.is_symlink():
        raise CandidateIntegrityError(f"candidate target already exists: {destination}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(os.fspath(destination), flags, expected_mode)
        source_descriptor = os.open(os.fspath(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise CandidateIntegrityError(f"short write copying {source}")
                    view = view[written:]
        finally:
            os.close(source_descriptor)
        os.fchmod(descriptor, expected_mode)
        os.fsync(descriptor)
    except ArtifactError:
        raise
    except OSError as exc:
        raise ArtifactWriteError(f"cannot copy package file {source}: {exc}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    actual = digest.hexdigest()
    if size != expected["size"] or actual != expected["sha256"]:
        raise DigestMismatchError(
            f"package file changed at {expected['relative_path']}: "
            f"expected {expected['sha256']}/{expected['size']}, got {actual}/{size}"
        )
    return actual


def _source_file_set(root: Path) -> set[str]:
    """List source package files without following symlinks."""

    files: set[str] = set()
    if not root.exists() or root.is_symlink() or not root.is_dir():
        raise CandidateIntegrityError(f"snapshot path is not a real directory: {root}")
    stack = [(root, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            children = list(os.scandir(current))
        except OSError as exc:
            raise CandidateIntegrityError(f"cannot list snapshot directory {current}: {exc}") from exc
        for child in children:
            child_path = Path(child.path)
            relative = f"{prefix}/{child.name}" if prefix else child.name
            "/".join(_validate_relative_path(relative))
            try:
                child_stat = os.lstat(child_path)
            except OSError as exc:
                raise CandidateIntegrityError(f"cannot stat snapshot entry {child_path}: {exc}") from exc
            if stat.S_ISLNK(child_stat.st_mode) or stat.S_ISREG(child_stat.st_mode):
                files.add(relative)
            elif stat.S_ISDIR(child_stat.st_mode):
                stack.append((child_path, relative))
            else:
                raise CandidateIntegrityError(f"snapshot contains a special file: {relative}")
    return files


def _verify_candidate_files(root: Path, expected: Sequence[Mapping[str, Any]]) -> str:
    expected_paths = {str(item["relative_path"]) for item in expected}
    actual_paths = _source_file_set(root)
    if actual_paths != expected_paths:
        raise CandidateIntegrityError(
            "candidate file set differs from snapshot: "
            f"missing={sorted(expected_paths - actual_paths)!r}, "
            f"unexpected={sorted(actual_paths - expected_paths)!r}"
        )
    actual_entries: list[dict[str, Any]] = []
    for item in expected:
        path = root.joinpath(*str(item["relative_path"]).split("/"))
        digest, size = _sha256_file(path)
        expected_mode = 0o755 if item["mode"] == "100755" else 0o644
        if digest != item["sha256"] or size != item["size"] or stat.S_IMODE(os.lstat(path).st_mode) != expected_mode:
            raise DigestMismatchError(f"candidate verification failed at {item['relative_path']}")
        actual_entries.append(dict(item))
    verified = _canonical_digest(actual_entries)
    return verified


def _write_candidate_metadata(path: Path, relative: str, payload: Mapping[str, Any]) -> Path:
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")).encode("utf-8") + b"\n"
    return _atomic_write_bytes(path, relative, data)


def _build_request(
    snapshot: Any,
    *,
    repository: str | None,
    skill_path: str | None,
    source_revision: str | None,
    skill_digest: str | None,
    eligible: bool | None,
    branch: str | None,
    skill_name: str | None,
    security_decision: str | None,
    quality_score: int | float | None,
    evidence_ref: str | None,
) -> CandidateExportRequest:
    if isinstance(snapshot, CandidateExportRequest):
        if any(value is not None for value in (repository, skill_path, source_revision, skill_digest, eligible)):
            raise CandidateNotEligibleError("do not combine CandidateExportRequest with override arguments")
        return snapshot
    missing = [
        name
        for name, value in {
            "repository": repository,
            "skill_path": skill_path,
            "source_revision": source_revision,
            "skill_digest": skill_digest,
            "eligible": eligible,
        }.items()
        if value is None
    ]
    if missing:
        raise CandidateIntegrityError("candidate export is missing: " + ", ".join(missing))
    return CandidateExportRequest(
        snapshot=snapshot,
        repository=repository,  # type: ignore[arg-type]
        skill_path=skill_path,  # type: ignore[arg-type]
        source_revision=source_revision,  # type: ignore[arg-type]
        skill_digest=skill_digest,  # type: ignore[arg-type]
        eligible=eligible,  # type: ignore[arg-type]
        branch=branch,
        skill_name=skill_name,
        security_decision=security_decision,
        quality_score=quality_score,
        evidence_ref=evidence_ref,
    )


def export_private_candidate(
    snapshot: Any,
    *,
    candidate_root: str | os.PathLike[str],
    repository: str | None = None,
    skill_path: str | None = None,
    source_revision: str | None = None,
    skill_digest: str | None = None,
    eligible: bool | None = None,
    branch: str | None = None,
    skill_name: str | None = None,
    security_decision: str | None = None,
    quality_score: int | float | None = None,
    evidence_ref: str | None = None,
    evidence_root: str | os.PathLike[str] | None = None,
) -> CandidateExportResult:
    """Export one explicitly eligible, digest-verified private candidate.

    ``snapshot`` must be a :class:`snapshot.SnapshotResult` (or an equivalent
    object exposing ``snapshot_path``, ``entries``, ``source_revision`` and
    ``skill_digest``).  The explicit ``eligible`` argument is required when
    passing the long form; callers can instead pass a
    :class:`CandidateExportRequest` as the first argument.
    """

    request = _build_request(
        snapshot,
        repository=repository,
        skill_path=skill_path,
        source_revision=source_revision,
        skill_digest=skill_digest,
        eligible=eligible,
        branch=branch,
        skill_name=skill_name,
        security_decision=security_decision,
        quality_score=quality_score,
        evidence_ref=evidence_ref,
    )
    if request.eligible is not True:
        raise CandidateNotEligibleError("candidate export requires explicit eligible=True")
    try:
        repository_name = _normalize_repository_name(request.repository)
        normalized_skill_path = normalize_skill_path(request.skill_path)
    except (TypeError, ValueError) as exc:
        raise CandidateIntegrityError(f"invalid candidate source identity: {exc}") from exc
    requested_digest = _normalize_digest(request.skill_digest)
    requested_revision = _normalize_revision(request.source_revision)
    snapshot_root, snapshot_revision, snapshot_digest, raw_entries, coverage_complete = _snapshot_values(request.snapshot)
    if requested_digest != snapshot_digest:
        raise DigestMismatchError("requested skill_digest does not match snapshot skill_digest")
    if requested_revision != snapshot_revision:
        raise RevisionMismatchError("requested source_revision does not match snapshot source_revision")
    if not coverage_complete:
        raise CandidateNotEligibleError("snapshot coverage is incomplete; candidate export is blocked")
    entries = [_manifest_entry_data(item) for item in raw_entries]
    if not entries:
        raise CandidateIntegrityError("snapshot package manifest is empty")
    if not any(item["relative_path"] == "SKILL.md" for item in entries):
        raise CandidateIntegrityError("snapshot package manifest is missing SKILL.md")
    manifest_digest = _canonical_digest(entries)
    if manifest_digest != requested_digest:
        raise DigestMismatchError("snapshot manifest does not match snapshot skill_digest")

    candidate_root_path = _ensure_directory(Path(candidate_root))
    if evidence_root is not None and _path_overlap(candidate_root_path, Path(evidence_root)):
        raise ArtifactPathError("candidate and restricted evidence roots must be separate")
    if _path_overlap(candidate_root_path, snapshot_root):
        raise ArtifactPathError("candidate root must not overlap the source snapshot")
    if security_decision is not None and str(security_decision).upper() != "PASS":
        raise CandidateNotEligibleError("candidate export requires security_decision=PASS when supplied")

    repo_slug = _safe_slug(repository_name, "repository")
    path_slug = _safe_slug(normalized_skill_path, "skill_path")
    candidate_parent = safe_join(candidate_root_path, f"{repo_slug}/{path_slug}")
    _ensure_directory(candidate_parent)
    candidate_path = safe_join(candidate_parent, requested_digest)
    if candidate_path.exists() or candidate_path.is_symlink():
        if candidate_path.is_symlink() or not candidate_path.is_dir():
            raise ArtifactPathError(f"candidate destination is not a directory: {candidate_path}")
        package_path = safe_join(candidate_path, "package")
        if not package_path.is_dir() or package_path.is_symlink():
            raise CandidateIntegrityError(f"existing candidate has no safe package directory: {candidate_path}")
        verified = _verify_candidate_files(package_path, entries)
        if verified != requested_digest:
            raise DigestMismatchError("existing candidate package digest does not match")
        source_manifest_path = safe_join(candidate_path, "source-manifest.json")
        review_summary_path = safe_join(candidate_path, "review-summary.json")
        verification_path = safe_join(candidate_path, "export-verification.json")
        try:
            with source_manifest_path.open("r", encoding="utf-8") as handle:
                existing_manifest = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CandidateIntegrityError(
                f"existing candidate source manifest is unreadable: {source_manifest_path}"
            ) from exc
        if not isinstance(existing_manifest, Mapping):
            raise CandidateIntegrityError("existing candidate source manifest is not an object")
        if (
            str(existing_manifest.get("skill_digest", "")).lower() != requested_digest
            or str(existing_manifest.get("source_revision", "")).lower() != requested_revision
        ):
            raise RevisionMismatchError("existing candidate metadata does not match frozen source")
        return CandidateExportResult(
            candidate_path,
            package_path,
            source_manifest_path,
            review_summary_path,
            verification_path,
            repository_name,
            normalized_skill_path,
            requested_revision,
            requested_digest,
            verified,
            False,
        )

    staging = candidate_parent / f".{requested_digest}.staging-{uuid.uuid4().hex}"
    _ensure_directory(staging)
    package_path = safe_join(staging, "package")
    _ensure_directory(package_path)
    try:
        source_files = _source_file_set(snapshot_root)
        expected_paths = {str(item["relative_path"]) for item in entries}
        if source_files != expected_paths:
            raise CandidateIntegrityError(
                "snapshot file set differs from its manifest: "
                f"missing={sorted(expected_paths - source_files)!r}, "
                f"unexpected={sorted(source_files - expected_paths)!r}"
            )
        for item in entries:
            relative = str(item["relative_path"])
            source = snapshot_root.joinpath(*relative.split("/"))
            destination = package_path.joinpath(*relative.split("/"))
            _copy_regular_file(source, destination, item, package_root=package_path)
        verified = _verify_candidate_files(package_path, entries)
        if verified != requested_digest:
            raise DigestMismatchError("candidate package digest does not match requested digest")

        source_manifest = {
            "schema_version": "1",
            "repository": repository_name,
            "branch": request.branch,
            "skill_name": request.skill_name,
            "skill_path": normalized_skill_path,
            "source_revision": requested_revision,
            "skill_digest": requested_digest,
        }
        review_summary = {
            "schema_version": "1",
            "repository": repository_name,
            "branch": request.branch,
            "skill_name": request.skill_name,
            "skill_path": normalized_skill_path,
            "source_revision": requested_revision,
            "skill_digest": requested_digest,
            "security_decision": request.security_decision,
            "quality_score": request.quality_score,
            "private_candidate_eligible": True,
            "evidence_ref": request.evidence_ref,
        }
        verification = {
            "schema_version": "1",
            "skill_digest": requested_digest,
            "verified_digest": verified,
            "source_revision": requested_revision,
            "package_file_count": len(entries),
            "raw_scanner_reports_in_candidate": False,
        }
        source_manifest_path = _write_candidate_metadata(staging, "source-manifest.json", source_manifest)
        review_summary_path = _write_candidate_metadata(staging, "review-summary.json", review_summary)
        verification_path = _write_candidate_metadata(staging, "export-verification.json", verification)
        os.replace(os.fspath(staging), os.fspath(candidate_path))
        staging = Path()
        _fsync_directory(candidate_parent)
    except ArtifactError:
        raise
    except OSError as exc:
        raise ArtifactWriteError(f"cannot finalize private candidate {candidate_path}: {exc}") from exc
    finally:
        # Remove only this operation's incomplete staging directory.  No source
        # workspace, evidence directory, or prior candidate is ever cleaned.
        if staging != Path() and staging.exists():
            import shutil

            try:
                shutil.rmtree(staging)
            except OSError:
                pass
    return CandidateExportResult(
        candidate_path,
        candidate_path / "package",
        candidate_path / "source-manifest.json",
        candidate_path / "review-summary.json",
        candidate_path / "export-verification.json",
        repository_name,
        normalized_skill_path,
        requested_revision,
        requested_digest,
        verified,
        True,
    )


# Descriptive aliases used by orchestration code and downstream callers.
export_candidate = export_private_candidate
RestrictedEvidenceStore = EvidenceStore


__all__ = [
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactPathError",
    "ArtifactWriteError",
    "CandidateExportRequest",
    "CandidateExportResult",
    "CandidateIntegrityError",
    "CandidateNotEligibleError",
    "DigestMismatchError",
    "EvidenceReference",
    "EvidenceStore",
    "RestrictedEvidenceStore",
    "RevisionMismatchError",
    "export_candidate",
    "export_private_candidate",
    "safe_join",
]
