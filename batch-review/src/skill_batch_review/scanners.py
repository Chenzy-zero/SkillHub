"""Shell-free adapters for the approved static Skill scanners.

This module deliberately has no knowledge of the contents of a Skill beyond
reading it to calculate a directory summary.  In particular, it never imports
or executes files from ``skill_root``.  The two concrete adapters only invoke
the scanner executable with an argv tuple and preserve the scanner's JSON
report for later evidence processing.

The public surface is intentionally useful to the later orchestration stage:

* :class:`CommandRunner` executes a command with ``shell=False`` and bounded
  stdout/stderr capture;
* :func:`summarize_directory` and :func:`compare_directory_summaries` provide
  a pre/post input-integrity check without following symlinks;
* :class:`CiscoSkillScannerAdapter` and :class:`SkillSpectorAdapter` render
  the approved command lines and apply their different exit-code rules;
* :class:`Finding` and :class:`ScanResult` are scanner-neutral result models.

The report parser is deliberately permissive about additional fields and
common container names.  Required finding fields (severity and a textual
message) remain explicit: a report containing an incomplete finding is marked
``INCOMPLETE`` and can never be treated as a clean scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence


SCANNER_STATUS_COMPLETED = "COMPLETED"
SCANNER_STATUS_INCOMPLETE = "INCOMPLETE"
SCANNER_STATUS_ERROR = "ERROR"
SCANNER_STATUS_TIMEOUT = "TIMEOUT"

DECISION_PASS = "PASS"
DECISION_DO_NOT_INSTALL = "DO_NOT_INSTALL"
DECISION_BLOCKED = "BLOCKED"
DECISION_REVIEW_REQUIRED = "REVIEW_REQUIRED"
DECISION_UNKNOWN = "UNKNOWN"

DEFAULT_CAPTURE_BYTES = 64 * 1024
DEFAULT_REPORT_BYTES = 4 * 1024 * 1024
PARSER_VERSION = "1"


class ScannerError(RuntimeError):
    """Base exception for scanner execution and evidence errors."""


class ScannerConfigurationError(ValueError):
    """A scanner invocation would be unsafe or cannot be represented."""


class DirectorySummaryError(ScannerError):
    """A Skill directory could not be read without following symlinks."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _require_path(value: str | os.PathLike[str], field_name: str) -> Path:
    path = Path(value).expanduser()
    if "\x00" in str(path):
        raise ScannerConfigurationError(f"{field_name} must not contain NUL")
    return path


def _absolute(path: Path) -> Path:
    # ``absolute`` does not follow a symlink and therefore preserves the
    # lexical location used for the input-boundary check.
    return Path(os.path.abspath(os.fspath(path)))


def _path_inside(path: Path, directory: Path) -> bool:
    """Return true if either lexical or resolved path is inside directory."""

    path_abs = _absolute(path)
    directory_abs = _absolute(directory)
    if path_abs == directory_abs or path_abs.is_relative_to(directory_abs):
        return True
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class DirectoryEntrySummary:
    """Stable metadata for one entry in a Skill Root.

    Symlinks are represented by their target text and are never traversed.
    Regular file content is hashed; executable permission bits are included so
    a mode-only change is detected as well.
    """

    relative_path: str
    kind: str
    mode: int
    size: int
    content_sha256: str | None = None
    link_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "mode": self.mode,
            "size": self.size,
            "content_sha256": self.content_sha256,
            "link_target": self.link_target,
        }


@dataclass(frozen=True, slots=True)
class DirectorySummary:
    """A content summary suitable for pre/post scanner comparison."""

    root: str
    digest: str
    entry_count: int
    file_count: int
    directory_count: int
    symlink_count: int
    special_count: int
    total_bytes: int
    entries: tuple[DirectoryEntrySummary, ...] = field(default_factory=tuple)

    @property
    def skill_digest(self) -> str:
        """Alias used by the content-version model."""

        return self.digest

    def to_dict(self, *, include_entries: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "root": self.root,
            "digest": self.digest,
            "entry_count": self.entry_count,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "symlink_count": self.symlink_count,
            "special_count": self.special_count,
            "total_bytes": self.total_bytes,
        }
        if include_entries:
            result["entries"] = [entry.to_dict() for entry in self.entries]
        return result


@dataclass(frozen=True, slots=True)
class DirectoryComparison:
    """Result of comparing summaries taken around one scanner execution."""

    before_digest: str
    after_digest: str
    unchanged: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "unchanged": self.unchanged,
            "reason": self.reason,
        }


def _hash_regular_file(path: Path) -> tuple[str, int]:
    """Hash a regular file without following a symlink at open time."""

    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    digest = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise DirectorySummaryError(f"cannot open file while summarizing {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise DirectorySummaryError(f"cannot read file while summarizing {path}: {exc}") from exc
    return digest.hexdigest(), total


def summarize_directory(root: str | os.PathLike[str]) -> DirectorySummary:
    """Summarize a Skill Root without executing or following its contents.

    The summary includes hidden files, empty directories, file bytes, mode
    bits, and symlink target text.  A symlink to a directory is recorded as a
    symlink and is not traversed.  Special files are recorded as metadata but
    are not opened.
    """

    root_path = _require_path(root, "skill_root")
    root_abs = _absolute(root_path)
    try:
        root_stat = os.lstat(root_abs)
    except OSError as exc:
        raise DirectorySummaryError(f"skill_root does not exist: {root_abs}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise DirectorySummaryError(f"skill_root must be a real directory: {root_abs}")

    entries: list[DirectoryEntrySummary] = []
    stack: list[tuple[Path, str]] = [(root_abs, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError as exc:
            raise DirectorySummaryError(f"cannot list directory {current}: {exc}") from exc
        # Reverse push keeps the traversal deterministic when using a stack.
        for child in reversed(children):
            child_path = Path(child.path)
            relative = f"{prefix}/{child.name}" if prefix else child.name
            relative = relative.replace(os.sep, "/")
            try:
                child_stat = os.lstat(child_path)
            except OSError as exc:
                raise DirectorySummaryError(
                    f"cannot stat directory entry {child_path}: {exc}"
                ) from exc
            mode = stat.S_IMODE(child_stat.st_mode)
            if stat.S_ISREG(child_stat.st_mode):
                content_sha256, actual_size = _hash_regular_file(child_path)
                entries.append(
                    DirectoryEntrySummary(
                        relative_path=relative,
                        kind="file",
                        mode=mode,
                        size=actual_size,
                        content_sha256=content_sha256,
                    )
                )
            elif stat.S_ISDIR(child_stat.st_mode):
                entries.append(
                    DirectoryEntrySummary(
                        relative_path=relative,
                        kind="directory",
                        mode=mode,
                        size=0,
                    )
                )
                stack.append((child_path, relative))
            elif stat.S_ISLNK(child_stat.st_mode):
                try:
                    link_target = os.readlink(child_path)
                except OSError as exc:
                    raise DirectorySummaryError(
                        f"cannot read symlink {child_path}: {exc}"
                    ) from exc
                entries.append(
                    DirectoryEntrySummary(
                        relative_path=relative,
                        kind="symlink",
                        mode=mode,
                        size=0,
                        link_target=link_target,
                    )
                )
            else:
                entries.append(
                    DirectoryEntrySummary(
                        relative_path=relative,
                        kind="special",
                        mode=mode,
                        size=0,
                    )
                )

    entries.sort(key=lambda item: item.relative_path)
    digest = _digest_json([entry.to_dict() for entry in entries])
    file_count = sum(entry.kind == "file" for entry in entries)
    directory_count = sum(entry.kind == "directory" for entry in entries)
    symlink_count = sum(entry.kind == "symlink" for entry in entries)
    special_count = sum(entry.kind == "special" for entry in entries)
    total_bytes = sum(entry.size for entry in entries if entry.kind == "file")
    return DirectorySummary(
        root=str(root_abs),
        digest=digest,
        entry_count=len(entries),
        file_count=file_count,
        directory_count=directory_count,
        symlink_count=symlink_count,
        special_count=special_count,
        total_bytes=total_bytes,
        entries=tuple(entries),
    )


def compare_directory_summaries(
    before: DirectorySummary,
    after: DirectorySummary,
) -> DirectoryComparison:
    """Compare two summaries and explain a root/path mismatch if present."""

    if before.root != after.root:
        return DirectoryComparison(
            before_digest=before.digest,
            after_digest=after.digest,
            unchanged=False,
            reason="summary roots differ",
        )
    if before.digest == after.digest:
        return DirectoryComparison(before.digest, after.digest, True, None)
    return DirectoryComparison(
        before_digest=before.digest,
        after_digest=after.digest,
        unchanged=False,
        reason="Skill Root content or metadata changed during scan",
    )


@dataclass(frozen=True, slots=True)
class ToolVersionRecord:
    """Version and invocation metadata recorded with every scan."""

    scanner: str
    executable: str
    configured_version: str
    detected_version: str | None = None
    version_command: tuple[str, ...] = field(default_factory=tuple)
    version_exit_code: int | None = None
    recorded_at: str = field(default_factory=_utc_now)

    @property
    def version(self) -> str:
        return self.detected_version or self.configured_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanner": self.scanner,
            "executable": self.executable,
            "configured_version": self.configured_version,
            "detected_version": self.detected_version,
            "effective_version": self.version,
            "version_command": list(self.version_command),
            "version_exit_code": self.version_exit_code,
            "recorded_at": self.recorded_at,
        }


class _LimitedCapture:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._buffer = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        remaining = self.max_bytes - len(self._buffer)
        if remaining > 0:
            self._buffer.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """Bounded result from one shell-free child-process invocation."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    launch_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "launch_error": self.launch_error,
        }


def _capture_pipe(stream: Any, capture: _LimitedCapture) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            capture.append(chunk)
    except (OSError, ValueError):
        # A killed process can close its pipe while a reader is active.  The
        # process result still records the bounded bytes received so far.
        return


class CommandRunner:
    """Run commands without a shell and with bounded output capture."""

    def __init__(self, *, max_capture_bytes: int = DEFAULT_CAPTURE_BYTES):
        if max_capture_bytes < 1:
            raise ScannerConfigurationError("max_capture_bytes must be positive")
        self.max_capture_bytes = max_capture_bytes

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandExecution:
        command = tuple(argv)
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ScannerConfigurationError("argv must contain non-empty strings")
        if any("\x00" in item for item in command):
            raise ScannerConfigurationError("argv must not contain NUL")
        if timeout_seconds <= 0:
            raise ScannerConfigurationError("timeout_seconds must be positive")
        started_clock = time.monotonic()
        started_at = _utc_now()
        stdout_capture = _LimitedCapture(self.max_capture_bytes)
        stderr_capture = _LimitedCapture(self.max_capture_bytes)
        process: subprocess.Popen[bytes] | None = None
        launch_error: str | None = None
        timed_out = False
        returncode: int | None = None

        try:
            kwargs: dict[str, Any] = {
                "shell": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": os.fspath(cwd) if cwd is not None else None,
                "env": dict(env) if env is not None else None,
                "close_fds": True,
            }
            if os.name == "posix":
                # This lets timeout cleanup terminate descendants too, without
                # using preexec_fn or running any target content.
                kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)
            stdout_thread = threading.Thread(
                target=_capture_pipe,
                args=(process.stdout, stdout_capture),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_capture_pipe,
                args=(process.stderr, stderr_capture),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
                returncode = process.returncode
            stdout_thread.join(timeout=2.0)
            stderr_thread.join(timeout=2.0)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        except OSError as exc:
            launch_error = f"cannot start scanner: {exc}"
        finally:
            if process is not None and process.poll() is None:
                self._terminate(process)
                try:
                    returncode = process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    returncode = process.returncode

        finished_at = _utc_now()
        return CommandExecution(
            argv=command,
            returncode=returncode,
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            timed_out=timed_out,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_clock),
            launch_error=launch_error,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - exercised on Windows deployments
                process.terminate()
            process.wait(timeout=0.5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if process.poll() is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover
                    process.kill()
                process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            return


@dataclass(frozen=True, slots=True)
class Finding:
    """Scanner-neutral representation of one reported issue."""

    finding_id: str
    scanner: str
    rule_id: str | None
    category: str | None
    severity: str | None
    title: str | None
    message: str
    path: str | None
    line: int | None
    column: int | None
    evidence: str | None
    remediation: str | None
    confidence: float | None
    complete: bool = True
    missing_fields: tuple[str, ...] = field(default_factory=tuple)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "scanner": self.scanner,
            "rule_id": self.rule_id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "confidence": self.confidence,
            "complete": self.complete,
            "missing_fields": list(self.missing_fields),
            "raw": dict(self.raw),
        }


@dataclass(frozen=True, slots=True)
class ParsedReport:
    """Parser output before tool-specific exit-code handling."""

    findings: tuple[Finding, ...]
    complete: bool
    decision: str | None
    errors: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Unified result from Cisco or SkillSpector."""

    scanner: str
    status: str
    decision: str | None
    tool_ok: bool
    report_complete: bool
    exit_code: int | None
    command: tuple[str, ...]
    command_digest: str
    config_digest: str
    tool_version: ToolVersionRecord
    skill_root: str
    skill_digest: str | None
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    report_path: str | None
    raw_report_path: str | None
    report_sha256: str | None
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    before_summary: DirectorySummary | None = None
    after_summary: DirectorySummary | None = None
    directory_comparison: DirectoryComparison | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    report_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        """Whether a complete report was produced for an unchanged input."""

        return (
            self.status == SCANNER_STATUS_COMPLETED
            and self.report_complete
            and (self.directory_comparison is None or self.directory_comparison.unchanged)
        )

    @property
    def scanner_config_digest(self) -> str:
        """Naming alias used by the task-id/data model."""

        return self.config_digest

    @property
    def input_unchanged(self) -> bool | None:
        return None if self.directory_comparison is None else self.directory_comparison.unchanged

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanner": self.scanner,
            "status": self.status,
            "decision": self.decision,
            "tool_ok": self.tool_ok,
            "completed": self.completed,
            "report_complete": self.report_complete,
            "exit_code": self.exit_code,
            "command": list(self.command),
            "command_digest": self.command_digest,
            "config_digest": self.config_digest,
            "scanner_config_digest": self.config_digest,
            "tool_version": self.tool_version.to_dict(),
            "skill_root": self.skill_root,
            "skill_digest": self.skill_digest,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "report_path": self.report_path,
            "raw_report_path": self.raw_report_path,
            "report_sha256": self.report_sha256,
            "findings": [finding.to_dict() for finding in self.findings],
            "before_summary": self.before_summary.to_dict() if self.before_summary else None,
            "after_summary": self.after_summary.to_dict() if self.after_summary else None,
            "directory_comparison": (
                self.directory_comparison.to_dict() if self.directory_comparison else None
            ),
            "errors": list(self.errors),
            "report_metadata": dict(self.report_metadata),
        }


_KEY_RE = re.compile(r"[^a-z0-9]+")


def _key(value: Any) -> str:
    return _KEY_RE.sub("", str(value).lower())


def _mapping_value(mapping: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    wanted = {_key(alias) for alias in aliases}
    for name, value in mapping.items():
        if _key(name) in wanted:
            return value
    return None


def _nested_mapping_value(mapping: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    direct = _mapping_value(mapping, aliases)
    if direct is not None:
        return direct
    for location_name in ("location", "position", "source", "match", "finding"):
        nested = _mapping_value(mapping, (location_name,))
        if isinstance(nested, Mapping):
            value = _mapping_value(nested, aliases)
            if value is not None:
                return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (Mapping, list, tuple)):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    result = str(value).strip()
    return result or None


def _severity(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    normalized = re.sub(r"[\s-]+", "_", text.upper())
    aliases = {
        "CRITICAL": "CRITICAL",
        "SEVERE": "CRITICAL",
        "HIGH": "HIGH",
        "MEDIUM": "MEDIUM",
        "MODERATE": "MEDIUM",
        "LOW": "LOW",
        "INFO": "INFO",
        "INFORMATIONAL": "INFO",
        "WARNING": "MEDIUM",
        "WARN": "MEDIUM",
    }
    return aliases.get(normalized, normalized)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _confidence(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result > 1:
        result /= 100
    return max(0.0, min(1.0, result))


def _decision(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    normalized = re.sub(r"[\s-]+", "_", text.upper())
    aliases = {
        "PASS": DECISION_PASS,
        "PASSED": DECISION_PASS,
        "CLEAN": DECISION_PASS,
        "OK": DECISION_PASS,
        "DO_NOT_INSTALL": DECISION_DO_NOT_INSTALL,
        "DONT_INSTALL": DECISION_DO_NOT_INSTALL,
        "BLOCK": DECISION_BLOCKED,
        "BLOCKED": DECISION_BLOCKED,
        "FAIL": DECISION_BLOCKED,
        "FAILED": DECISION_BLOCKED,
        "REVIEW": DECISION_REVIEW_REQUIRED,
        "REVIEW_REQUIRED": DECISION_REVIEW_REQUIRED,
    }
    return aliases.get(normalized)


def _finding_from_mapping(raw: Mapping[str, Any], scanner: str, index: int) -> Finding:
    rule_id = _text(
        _nested_mapping_value(raw, ("rule_id", "rule", "check_id", "check", "ruleId"))
    )
    category = _text(_nested_mapping_value(raw, ("category", "type", "finding_type", "kind")))
    severity = _severity(
        _nested_mapping_value(raw, ("severity", "risk_level", "risk", "level", "priority"))
    )
    title = _text(_nested_mapping_value(raw, ("title", "name", "summary")))
    message = _text(
        _nested_mapping_value(raw, ("message", "description", "detail", "reason", "finding"))
    )
    if message is None:
        # A title-only official finding still has useful human-readable text,
        # but its missing message is recorded as an incomplete contract.
        message = title or ""
    path = _text(
        _nested_mapping_value(raw, ("path", "file", "file_path", "filename", "file_name"))
    )
    line = _integer(_nested_mapping_value(raw, ("line", "line_number", "start_line")))
    column = _integer(_nested_mapping_value(raw, ("column", "column_number", "start_column")))
    evidence = _text(_nested_mapping_value(raw, ("evidence", "snippet", "code", "match")))
    remediation = _text(
        _nested_mapping_value(raw, ("remediation", "recommendation", "fix", "solution"))
    )
    confidence = _confidence(_nested_mapping_value(raw, ("confidence", "score")))
    explicit_id = _text(_nested_mapping_value(raw, ("finding_id", "id", "uuid")))
    generated_id = _digest_json({"scanner": scanner, "raw": raw})[:16]
    finding_id = explicit_id or f"finding-{generated_id}"
    missing: list[str] = []
    if severity is None:
        missing.append("severity")
    if not message:
        missing.append("message")
    return Finding(
        finding_id=finding_id,
        scanner=scanner,
        rule_id=rule_id,
        category=category,
        severity=severity,
        title=title,
        message=message,
        path=path,
        line=line,
        column=column,
        evidence=evidence,
        remediation=remediation,
        confidence=confidence,
        complete=not missing,
        missing_fields=tuple(missing),
        raw=dict(raw),
    )


_FINDING_CONTAINER_KEYS = (
    "findings",
    "issues",
    "vulnerabilities",
    "violations",
    "detections",
    "alerts",
    "results",
    "checks",
)


def _find_finding_container(value: Any, depth: int = 0) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping) or depth > 8:
        return None
    for wanted in _FINDING_CONTAINER_KEYS:
        candidate = _mapping_value(value, (wanted,))
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, Mapping):
            nested = _find_finding_container(candidate, depth + 1)
            if nested is not None:
                return nested
    for child in value.values():
        if isinstance(child, Mapping):
            nested = _find_finding_container(child, depth + 1)
            if nested is not None:
                return nested
    return None


def parse_json_report(raw: bytes | str, *, scanner: str) -> ParsedReport:
    """Parse a scanner JSON report while retaining unknown extensions.

    A report must be a JSON object containing a recognized findings container,
    a JSON array, or a single finding object.  Each finding needs severity and
    a textual message.  Other fields are optional and retained in ``raw``.
    """

    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    try:
        document = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return ParsedReport((), False, None, (f"report is not valid JSON: {exc}",))

    errors: list[str] = []
    container = _find_finding_container(document)
    if container is None and isinstance(document, Mapping):
        if _nested_mapping_value(document, ("severity",)) is not None or _nested_mapping_value(
            document, ("message", "description", "detail")
        ) is not None:
            container = [document]
    if container is None:
        return ParsedReport(
            (),
            False,
            None,
            ("report is missing a recognized findings array",),
            document if isinstance(document, Mapping) else {},
        )

    findings: list[Finding] = []
    for index, item in enumerate(container):
        if not isinstance(item, Mapping):
            errors.append(f"finding {index} is not a JSON object")
            continue
        finding = _finding_from_mapping(item, scanner, index)
        findings.append(finding)
        if not finding.complete:
            errors.append(
                f"finding {finding.finding_id} is missing: {', '.join(finding.missing_fields)}"
            )

    metadata: Mapping[str, Any] = document if isinstance(document, Mapping) else {}
    report_decision: str | None = None
    if isinstance(document, Mapping):
        report_decision = _decision(
            _mapping_value(document, ("decision", "verdict", "outcome", "scan_decision"))
        )
        if report_decision is None:
            status_value = _mapping_value(document, ("status", "result"))
            report_decision = _decision(status_value)
        complete_value = _mapping_value(document, ("complete", "scan_complete"))
        partial_value = _mapping_value(document, ("partial", "incomplete", "truncated"))
        if complete_value is False or partial_value is True:
            errors.append("report declares incomplete or partial coverage")
    return ParsedReport(
        findings=tuple(findings),
        complete=not errors,
        decision=report_decision,
        errors=tuple(errors),
        metadata=metadata,
    )


def _highest_severity(findings: Sequence[Finding]) -> str | None:
    order = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1}
    values = [finding.severity for finding in findings if finding.severity is not None]
    return max(values, key=lambda value: order.get(value, 0), default=None)


def _derived_decision(
    parsed: ParsedReport,
    *,
    returncode: int | None,
    scanner_kind: str,
) -> str:
    if scanner_kind == "skillspector" and returncode == 1:
        return DECISION_DO_NOT_INSTALL
    if parsed.decision is not None:
        return parsed.decision
    if returncode != 0:
        return DECISION_UNKNOWN
    highest = _highest_severity(parsed.findings)
    if highest in {"CRITICAL"}:
        return DECISION_BLOCKED
    if highest in {"HIGH", "MEDIUM", "LOW", "INFO"}:
        return DECISION_REVIEW_REQUIRED
    return DECISION_PASS


def _read_file_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    if limit < 1:
        raise ScannerConfigurationError("max_report_bytes must be positive")
    try:
        with path.open("rb") as handle:
            # Read one sentinel byte beyond the parsing limit.  The complete
            # report is preserved separately; only parsing is bounded.
            buffer = handle.read(limit + 1)
    except OSError as exc:
        raise ScannerError(f"cannot read scanner report {path}: {exc}") from exc
    return buffer[:limit], len(buffer) > limit


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ScannerError(f"cannot hash scanner report {path}: {exc}") from exc
    return digest.hexdigest()


def _preserve_report(source: Path, destination: Path) -> None:
    """Copy report bytes exactly, refusing to overwrite different evidence."""

    if source == destination:
        return
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise ScannerError(f"raw report destination is not a regular file: {destination}")
        if _file_sha256(source) != _file_sha256(destination):
            raise ScannerError(f"refusing to overwrite different raw report: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    except OSError as exc:
        raise ScannerError(f"cannot preserve raw scanner report {destination}: {exc}") from exc


class ScannerAdapter:
    """Common implementation shared by the two approved scanner commands."""

    scanner_name = "scanner"
    scanner_kind = "scanner"
    executable_name = "scanner"
    static_flags: tuple[str, ...] = ()
    static_environment: tuple[tuple[str, str], ...] = ()

    def __init__(
        self,
        *,
        executable: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 600,
        max_capture_bytes: int = DEFAULT_CAPTURE_BYTES,
        max_report_bytes: int = DEFAULT_REPORT_BYTES,
        tool_version: str = "configured",
        runner: CommandRunner | Any | None = None,
    ):
        executable_value = str(executable or self.executable_name)
        if not executable_value or "\x00" in executable_value:
            raise ScannerConfigurationError("scanner executable must be non-empty and NUL-free")
        if timeout_seconds <= 0:
            raise ScannerConfigurationError("timeout_seconds must be positive")
        if max_report_bytes < 1:
            raise ScannerConfigurationError("max_report_bytes must be positive")
        if not isinstance(tool_version, str) or not tool_version.strip():
            raise ScannerConfigurationError("tool_version must be non-empty")
        self.executable = executable_value
        self.timeout_seconds = timeout_seconds
        self.max_capture_bytes = max_capture_bytes
        self.max_report_bytes = max_report_bytes
        self.tool_version = tool_version.strip()
        self.runner = runner or CommandRunner(max_capture_bytes=max_capture_bytes)

    def build_command(self, skill_root: Path, output_file: Path) -> tuple[str, ...]:
        raise NotImplementedError

    def _configuration_payload(self) -> dict[str, Any]:
        return {
            "parser_version": PARSER_VERSION,
            "scanner": self.scanner_name,
            "scanner_kind": self.scanner_kind,
            "executable": self.executable,
            "timeout_seconds": self.timeout_seconds,
            "max_capture_bytes": self.max_capture_bytes,
            "max_report_bytes": self.max_report_bytes,
            "static_flags": list(self.static_flags),
            "static_environment": dict(self.static_environment),
            "tool_version": self.tool_version,
        }

    def _execution_environment(
        self,
        environment: Mapping[str, str] | None,
    ) -> Mapping[str, str] | None:
        if not self.static_environment:
            return environment
        effective = os.environ.copy() if environment is None else dict(environment)
        # Governance-required values take priority over ambient caller values.
        effective.update(self.static_environment)
        return effective

    @property
    def config_digest(self) -> str:
        return _digest_json(self._configuration_payload())

    def version_record(self) -> ToolVersionRecord:
        """Return configured version metadata without executing the scanner."""

        return ToolVersionRecord(
            scanner=self.scanner_name,
            executable=self.executable,
            configured_version=self.tool_version,
        )

    def detect_version(self, *, timeout_seconds: float | None = None) -> ToolVersionRecord:
        """Return pinned version metadata without starting scanner code.

        Kept for API compatibility.  Console entry points are deliberately not
        invoked for version discovery because their imports may initialize
        optional packages before parsing ``--version``.  Installation validates
        the pinned distribution using ``importlib.metadata`` instead.
        """

        del timeout_seconds
        return self.version_record()

    def scan(
        self,
        skill_root: str | os.PathLike[str],
        *,
        output_file: str | os.PathLike[str],
        raw_report_file: str | os.PathLike[str] | None = None,
        skill_digest: str | None = None,
        before_summary: DirectorySummary | None = None,
        timeout_seconds: float | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ScanResult:
        """Run one scanner against a frozen Skill Root.

        ``output_file`` and ``raw_report_file`` must be outside ``skill_root``
        so the scanner cannot make its own input appear to change merely by
        writing a report.  A caller may pass a previously captured
        ``before_summary``; otherwise this method captures it immediately
        before launching the scanner.
        """

        root = _require_path(skill_root, "skill_root")
        output = _require_path(output_file, "output_file")
        raw_output = _require_path(raw_report_file or output, "raw_report_file")
        root_abs = _absolute(root)
        output_abs = _absolute(output)
        raw_output_abs = _absolute(raw_output)
        if _path_inside(output_abs, root_abs) or _path_inside(raw_output_abs, root_abs):
            raise ScannerConfigurationError("scanner report paths must be outside skill_root")
        if output_abs.exists() or output_abs.is_symlink():
            raise ScannerConfigurationError(
                "output_file must be a new attempt-specific path to prevent stale report reuse"
            )
        if raw_output_abs.is_symlink():
            raise ScannerConfigurationError("raw_report_file must not be a symlink")
        output_abs.parent.mkdir(parents=True, exist_ok=True)
        command = self.build_command(root_abs, output_abs)
        command_digest = _digest_json(list(command))
        effective_timeout = timeout_seconds or self.timeout_seconds
        version_record = self.version_record()
        started_at = _utc_now()

        before = before_summary
        errors: list[str] = []
        precondition_failed = False
        if before is None:
            try:
                before = summarize_directory(root_abs)
            except DirectorySummaryError as exc:
                return self._error_result(
                    root_abs,
                    command,
                    command_digest,
                    version_record,
                    status=SCANNER_STATUS_ERROR,
                    skill_digest=skill_digest,
                    started_at=started_at,
                    errors=[str(exc)],
                )
        elif before.root != str(root_abs):
            errors.append("provided before_summary does not describe skill_root")
            precondition_failed = True

        # ``skill_digest`` is the canonical Git snapshot digest.  The local
        # before/after summary intentionally uses a different representation
        # for mutation detection, so the two values must not be compared.

        try:
            execution: CommandExecution = self.runner.run(
                command,
                timeout_seconds=effective_timeout,
                cwd=cwd,
                env=self._execution_environment(env),
            )
        except Exception as exc:  # runner adapters must not turn tool errors into passes
            execution = CommandExecution(
                argv=command,
                returncode=None,
                stdout="",
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
                started_at=started_at,
                finished_at=_utc_now(),
                duration_seconds=0.0,
                launch_error=f"scanner runner failed: {exc}",
            )

        after: DirectorySummary | None = None
        comparison: DirectoryComparison | None = None
        try:
            after = summarize_directory(root_abs)
            if before is not None:
                comparison = compare_directory_summaries(before, after)
                if not comparison.unchanged:
                    errors.append(comparison.reason or "Skill Root changed during scan")
        except DirectorySummaryError as exc:
            errors.append(str(exc))

        report_bytes: bytes | None = None
        report_hash: str | None = None
        report_path: str | None = None
        raw_report_path: str | None = None
        report_too_large = False
        if output_abs.is_file() and not output_abs.is_symlink():
            report_path = str(output_abs)
            raw_report_path = str(raw_output_abs)
            try:
                if raw_output_abs != output_abs:
                    _preserve_report(output_abs, raw_output_abs)
                report_hash = _file_sha256(output_abs)
                report_bytes, report_too_large = _read_file_bounded(output_abs, self.max_report_bytes)
            except ScannerError as exc:
                errors.append(str(exc))
        else:
            errors.append("scanner did not produce a regular JSON report file")

        parsed = ParsedReport((), False, None, ())
        if report_bytes is not None:
            if report_too_large:
                parsed = ParsedReport(
                    (),
                    False,
                    None,
                    (f"scanner report exceeds {self.max_report_bytes} bytes",),
                )
            else:
                parsed = parse_json_report(report_bytes, scanner=self.scanner_name)
            errors.extend(parsed.errors)

        status = SCANNER_STATUS_COMPLETED
        if execution.launch_error:
            status = SCANNER_STATUS_ERROR
            errors.append(execution.launch_error)
        elif execution.timed_out:
            status = SCANNER_STATUS_TIMEOUT
            errors.append("scanner timed out")
        elif self.scanner_kind == "skillspector" and execution.returncode == 2:
            status = SCANNER_STATUS_ERROR
            errors.append("SkillSpector exited with code 2")
        elif report_bytes is None:
            status = SCANNER_STATUS_ERROR
        elif not parsed.complete or report_too_large:
            status = SCANNER_STATUS_INCOMPLETE
        elif comparison is not None and not comparison.unchanged:
            status = SCANNER_STATUS_INCOMPLETE
        elif precondition_failed:
            status = SCANNER_STATUS_INCOMPLETE

        # SkillSpector 0 and 1 are both completed scans.  Cisco's non-zero
        # status is retained as tool_ok=False, but a valid JSON report remains
        # parseable rather than being trusted solely by exit code.
        allowed_codes = {0, 1} if self.scanner_kind == "skillspector" else {0}
        tool_ok = execution.returncode in allowed_codes and not execution.launch_error
        if self.scanner_kind == "skillspector" and execution.returncode == 2:
            tool_ok = False
        if status != SCANNER_STATUS_COMPLETED:
            tool_ok = False
        decision = _derived_decision(
            parsed,
            returncode=execution.returncode,
            scanner_kind=self.scanner_kind,
        ) if parsed.complete else None
        if errors and status == SCANNER_STATUS_COMPLETED:
            # Only non-fatal diagnostics (for example an unexpected exit code
            # with a usable Cisco report) remain on a completed result.
            if not tool_ok:
                decision = decision or DECISION_UNKNOWN

        return ScanResult(
            scanner=self.scanner_name,
            status=status,
            decision=decision,
            tool_ok=tool_ok,
            report_complete=parsed.complete,
            exit_code=execution.returncode,
            command=command,
            command_digest=command_digest,
            config_digest=self.config_digest,
            tool_version=version_record,
            skill_root=str(root_abs),
            skill_digest=skill_digest or (before.digest if before else None),
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            duration_seconds=execution.duration_seconds,
            stdout=execution.stdout,
            stderr=execution.stderr,
            stdout_truncated=execution.stdout_truncated,
            stderr_truncated=execution.stderr_truncated,
            report_path=report_path,
            raw_report_path=raw_report_path,
            report_sha256=report_hash,
            findings=parsed.findings,
            before_summary=before,
            after_summary=after,
            directory_comparison=comparison,
            errors=tuple(dict.fromkeys(errors)),
            report_metadata=parsed.metadata,
        )

    def _error_result(
        self,
        root: Path,
        command: tuple[str, ...],
        command_digest: str,
        version_record: ToolVersionRecord,
        *,
        status: str,
        skill_digest: str | None,
        started_at: str,
        errors: Sequence[str],
    ) -> ScanResult:
        finished_at = _utc_now()
        return ScanResult(
            scanner=self.scanner_name,
            status=status,
            decision=None,
            tool_ok=False,
            report_complete=False,
            exit_code=None,
            command=command,
            command_digest=command_digest,
            config_digest=self.config_digest,
            tool_version=version_record,
            skill_root=str(root),
            skill_digest=skill_digest,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=0.0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            report_path=None,
            raw_report_path=None,
            report_sha256=None,
            errors=tuple(errors),
        )


class CiscoSkillScannerAdapter(ScannerAdapter):
    """Cisco AI Skill Scanner in the approved local static mode."""

    scanner_name = "cisco"
    scanner_kind = "cisco"
    executable_name = "skill-scanner"
    # Deliberately no --use-llm, --use-behavioral, --use-virustotal, or
    # --use-aidefense flags.  Those capabilities remain disabled by omission.
    static_flags = ("--format", "json", "--compact")
    # Cisco imports its optional LiteLLM analyzer while loading the CLI even
    # when --use-llm is absent.  LiteLLM otherwise fetches its model cost map
    # from GitHub at import time.  Force the bundled map for air-gapped scans.
    static_environment = (("LITELLM_LOCAL_MODEL_COST_MAP", "True"),)

    def build_command(self, skill_root: Path, output_file: Path) -> tuple[str, ...]:
        return (
            self.executable,
            "scan",
            str(skill_root),
            "--format",
            "json",
            "--compact",
            "--output",
            str(output_file),
        )


class SkillSpectorAdapter(ScannerAdapter):
    """NVIDIA SkillSpector in no-LLM JSON mode."""

    scanner_name = "skillspector"
    scanner_kind = "skillspector"
    executable_name = "skillspector"
    static_flags = ("--no-llm", "--format", "json")

    def build_command(self, skill_root: Path, output_file: Path) -> tuple[str, ...]:
        return (
            self.executable,
            "scan",
            str(skill_root),
            "--no-llm",
            "--format",
            "json",
            "--output",
            str(output_file),
        )


# Short aliases make orchestration code less verbose while keeping the
# descriptive class names in tracebacks and documentation.
CiscoAdapter = CiscoSkillScannerAdapter
SkillSpectorScannerAdapter = SkillSpectorAdapter


__all__ = [
    "CiscoAdapter",
    "CiscoSkillScannerAdapter",
    "CommandExecution",
    "CommandRunner",
    "DECISION_BLOCKED",
    "DECISION_DO_NOT_INSTALL",
    "DECISION_PASS",
    "DECISION_REVIEW_REQUIRED",
    "DECISION_UNKNOWN",
    "DirectoryComparison",
    "DirectoryEntrySummary",
    "DirectorySummary",
    "DirectorySummaryError",
    "Finding",
    "ParsedReport",
    "SCANNER_STATUS_COMPLETED",
    "SCANNER_STATUS_ERROR",
    "SCANNER_STATUS_INCOMPLETE",
    "SCANNER_STATUS_TIMEOUT",
    "ScanResult",
    "ScannerAdapter",
    "ScannerConfigurationError",
    "ScannerError",
    "SkillSpectorAdapter",
    "SkillSpectorScannerAdapter",
    "ToolVersionRecord",
    "compare_directory_summaries",
    "parse_json_report",
    "summarize_directory",
]
