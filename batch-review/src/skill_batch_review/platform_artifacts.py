"""Platform-stable low-level artifact I/O.

Git/evidence identity is defined over exact bytes and Git manifest modes, not by
host text translation or NTFS permission emulation.  This module installs a
small compatibility layer over the legacy artifact primitives so Windows and
POSIX runners enforce the same digest semantics without weakening symlink,
path, or content-integrity checks.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


def _binary_flag() -> int:
    return int(getattr(os, "O_BINARY", 0))


def install_artifact_platform_compat(base: Any) -> None:
    """Install exact-byte I/O and host-appropriate mode verification."""

    def sha256_file(path: Path) -> tuple[str, int]:
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot stat artifact file {path}: {exc}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise base.ArtifactIntegrityError(f"artifact source is not a regular file: {path}")
        flags = os.O_RDONLY | _binary_flag() | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(os.fspath(path), flags)
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot open artifact file {path}: {exc}") from exc
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
            raise base.ArtifactWriteError(f"cannot read artifact file {path}: {exc}") from exc
        finally:
            os.close(descriptor)
        return digest.hexdigest(), total

    def atomic_write_bytes(
        root: Path,
        relative: str | os.PathLike[str],
        data: bytes,
        *,
        mode: int = 0o600,
        refuse_different_existing: bool = False,
    ) -> Path:
        target = base.safe_join(root, relative)
        parent = base._ensure_directory(target.parent)
        try:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise base.ArtifactPathError(f"artifact target is not a regular file: {target}")
            if refuse_different_existing and target.exists():
                existing_digest, existing_size = sha256_file(target)
                if existing_size != len(data) or existing_digest != base._sha256_bytes(data):
                    raise base.ArtifactWriteError(f"refusing to overwrite existing artifact: {target}")
                return target
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot inspect artifact target {target}: {exc}") from exc

        temporary: Path | None = None
        descriptor: int | None = None
        try:
            for _ in range(20):
                name = f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
                candidate = parent / name
                try:
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | _binary_flag()
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    descriptor = os.open(os.fspath(candidate), flags, mode)
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if descriptor is None or temporary is None:
                raise base.ArtifactWriteError(f"cannot allocate temporary artifact next to {target}")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise base.ArtifactWriteError(f"short write while writing {target}")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(os.fspath(temporary), os.fspath(target))
            temporary = None
            base._fsync_directory(parent)
            return target
        except base.ArtifactError:
            raise
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot atomically write artifact {target}: {exc}") from exc
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

    def copy_raw_report(
        self: Any,
        source: str | os.PathLike[str],
        relative_path: str | os.PathLike[str] | None = None,
        *,
        scanner: str | None = None,
    ) -> Any:
        if relative_path is None:
            if scanner is None:
                raise base.ArtifactPathError("relative_path or scanner must be provided")
            scanner_value = self._identifier(scanner, "scanner")
            relative_path = f"{scanner_value}/raw-report.json"
        relative = "/".join(base._validate_relative_path(relative_path))
        source_path = base._absolute(source)
        try:
            source_stat = os.lstat(source_path)
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot stat raw report {source_path}: {exc}") from exc
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise base.ArtifactIntegrityError(f"raw report source is not a regular file: {source_path}")

        target = base.safe_join(self.task_root, relative)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise base.ArtifactPathError(f"raw report target is not a regular file: {target}")
            source_digest, source_size = sha256_file(source_path)
            target_digest, target_size = sha256_file(target)
            if source_digest != target_digest or source_size != target_size:
                raise base.ArtifactWriteError(f"refusing to overwrite different raw report: {target}")
            return base.EvidenceReference(target, relative, target_digest, target_size)

        parent = base._ensure_directory(target.parent)
        temporary: Path | None = None
        destination_fd: int | None = None
        source_fd: int | None = None
        digest = hashlib.sha256()
        total = 0
        try:
            source_fd = os.open(
                os.fspath(source_path),
                os.O_RDONLY | _binary_flag() | getattr(os, "O_NOFOLLOW", 0),
            )
            for _ in range(20):
                candidate = parent / f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
                try:
                    destination_fd = os.open(
                        os.fspath(candidate),
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | _binary_flag()
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if destination_fd is None or temporary is None:
                raise base.ArtifactWriteError(f"cannot allocate temporary report next to {target}")
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise base.ArtifactWriteError(f"short write while copying raw report {source_path}")
                    view = view[written:]
            os.fsync(destination_fd)
            os.close(destination_fd)
            destination_fd = None
            os.close(source_fd)
            source_fd = None
            os.replace(os.fspath(temporary), os.fspath(target))
            temporary = None
            base._fsync_directory(parent)
            return base.EvidenceReference(target, relative, digest.hexdigest(), total)
        except base.ArtifactError:
            raise
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot preserve raw report {target}: {exc}") from exc
        finally:
            for descriptor in (destination_fd, source_fd):
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

    def copy_regular_file(
        source: Path,
        destination: Path,
        expected: Mapping[str, Any],
        *,
        package_root: Path,
    ) -> str:
        try:
            source_stat = os.lstat(source)
        except OSError as exc:
            raise base.CandidateIntegrityError(f"snapshot file is missing: {source}") from exc
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise base.CandidateIntegrityError(f"snapshot file is not a regular file: {source}")
        expected_mode = 0o755 if expected["mode"] == "100755" else 0o644
        if os.name != "nt" and stat.S_IMODE(source_stat.st_mode) not in {0o644, 0o755}:
            raise base.CandidateIntegrityError(f"snapshot file has an unsupported mode: {source}")

        relative_parent = destination.parent.relative_to(package_root)
        parent_components = tuple(relative_parent.parts)
        base._check_no_symlink_components(package_root, parent_components)
        destination.parent.mkdir(parents=True, exist_ok=True)
        base._check_no_symlink_components(package_root, parent_components)
        if destination.exists() or destination.is_symlink():
            raise base.CandidateIntegrityError(f"candidate target already exists: {destination}")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _binary_flag()
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(os.fspath(destination), flags, expected_mode)
            source_descriptor = os.open(
                os.fspath(source),
                os.O_RDONLY | _binary_flag() | getattr(os, "O_NOFOLLOW", 0),
            )
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
                            raise base.CandidateIntegrityError(f"short write copying {source}")
                        view = view[written:]
            finally:
                os.close(source_descriptor)
            if os.name != "nt":
                os.fchmod(descriptor, expected_mode)
            os.fsync(descriptor)
        except base.ArtifactError:
            raise
        except OSError as exc:
            raise base.ArtifactWriteError(f"cannot copy package file {source}: {exc}") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        actual = digest.hexdigest()
        if size != expected["size"] or actual != expected["sha256"]:
            raise base.DigestMismatchError(
                f"package file changed at {expected['relative_path']}: "
                f"expected {expected['sha256']}/{expected['size']}, got {actual}/{size}"
            )
        return actual

    def verify_candidate_files(root: Path, expected: Sequence[Mapping[str, Any]]) -> str:
        expected_paths = {str(item["relative_path"]) for item in expected}
        actual_paths = base._source_file_set(root)
        if actual_paths != expected_paths:
            raise base.CandidateIntegrityError(
                "candidate file set differs from snapshot: "
                f"missing={sorted(expected_paths - actual_paths)!r}, "
                f"unexpected={sorted(actual_paths - expected_paths)!r}"
            )
        actual_entries: list[dict[str, Any]] = []
        for item in expected:
            path = root.joinpath(*str(item["relative_path"]).split("/"))
            digest, size = sha256_file(path)
            expected_mode = 0o755 if item["mode"] == "100755" else 0o644
            mode_mismatch = (
                os.name != "nt"
                and stat.S_IMODE(os.lstat(path).st_mode) != expected_mode
            )
            if digest != item["sha256"] or size != item["size"] or mode_mismatch:
                raise base.DigestMismatchError(
                    f"candidate verification failed at {item['relative_path']}"
                )
            actual_entries.append(dict(item))
        return base._canonical_digest(actual_entries)

    base._sha256_file = sha256_file
    base._atomic_write_bytes = atomic_write_bytes
    base.EvidenceStore.copy_raw_report = copy_raw_report
    base.EvidenceStore.copy_report = copy_raw_report
    base._copy_regular_file = copy_regular_file
    base._verify_candidate_files = verify_candidate_files


__all__ = ["install_artifact_platform_compat"]
