"""CSV inventory loading and validation for the batch Skill review.

The inventory is deliberately kept independent from Git.  This module only
validates and normalises the values supplied by the CSV file; resolving a
repository, branch, or commit belongs to the later snapshot stage.

The source CSV is treated as evidence.  Its bytes are hashed before parsing,
and the seven source columns are retained on every row.  Exact duplicate
records are represented by one execution row while all physical CSV line
numbers remain available for audit.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .models import ReviewTargetKey, SourceKey, normalize_branch, normalize_skill_path


# Keep this order stable.  It is also the order used when calculating a row
# identifier, so an ID does not change merely because a producer reordered the
# columns in an otherwise equivalent CSV file.
INVENTORY_COLUMNS: tuple[str, ...] = (
    "skill_name",
    "repo_name",
    "branch",
    "skill_path",
    "lasted_commited",
    "security_reviewed",
    "status",
)
REQUIRED_COLUMNS = INVENTORY_COLUMNS

# A full SHA-1 or SHA-256 is preferred, but abbreviated hexadecimal revisions
# are useful as an inventory hint.  The length restriction is only a basic
# shape check: resolving the revision in Git is intentionally out of scope.
# Four characters is the minimum shape accepted here; ambiguity is left for
# the later Git-resolution stage.
_REVISION_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")


class InventoryError(ValueError):
    """Base class for an invalid inventory input."""


class InventoryHeaderError(InventoryError):
    """The CSV header is missing, duplicated, or contains unknown columns."""


class InventoryRowError(InventoryError):
    """A row cannot be converted into an inventory source."""

    def __init__(self, row_number: int, message: str, *, field: str | None = None):
        self.row_number = row_number
        self.field = field
        prefix = f"CSV row {row_number}"
        if field:
            prefix += f" field {field!r}"
        super().__init__(f"{prefix}: {message}")


class UnknownStatusError(InventoryRowError):
    """A status is not present in the caller supplied status mapping."""

    def __init__(self, row_number: int, status: str, known_statuses: Iterable[str]):
        self.status = status
        self.known_statuses = tuple(sorted(str(item) for item in known_statuses))
        known = ", ".join(repr(item) for item in self.known_statuses) or "<none>"
        super().__init__(
            row_number,
            f"unknown status {status!r}; expected one of: {known}",
            field="status",
        )


class InventorySource:
    """Small protocol-like shape accepted by downstream batch code.

    The concrete :class:`InventoryRow` below is used by this module.  Keeping
    this name as an alias makes it straightforward for an application to
    substitute its own model later without changing the CSV rules.
    """


def _reject_control_characters(value: str, *, field: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains a control character")


def validate_revision(revision: str) -> str:
    """Validate and lower-case a hexadecimal Git revision hint.

    This is intentionally not a Git existence check.  Only complete SHA-1 or
    SHA-256 identifiers are accepted; later stages still verify object type,
    existence and branch reachability in Git.
    """

    if not isinstance(revision, str):
        raise TypeError("lasted_commited must be a string")
    value = revision.strip()
    if not _REVISION_RE.fullmatch(value):
        raise ValueError("lasted_commited must be a full 40- or 64-character hex revision")
    return value.lower()


def _canonical_row_values(values: Mapping[str, str]) -> str:
    # JSON avoids ambiguity when a cell itself contains a delimiter.  The
    # order is fixed by INVENTORY_COLUMNS, not by dictionary iteration.
    return json.dumps(
        [values[column] for column in INVENTORY_COLUMNS],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def make_source_row_id(values: Mapping[str, str]) -> str:
    """Create a stable ID from the seven original CSV cell values.

    The physical line number and source-file hash are deliberately excluded:
    moving a row or copying it into a new batch must not change its identity.
    Consequently exact duplicate rows naturally share an ID and can be
    grouped while retaining their individual line numbers.
    """

    canonical = _canonical_row_values(values).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class InventoryRow:
    """One normalised, validated source row.

    ``status`` is the mapped internal value; ``raw_status`` and
    ``raw_values`` preserve what was present in the source CSV.  ``rows`` in
    :class:`InventoryDocument` are de-duplicated execution rows, while
    ``source_row_numbers`` contains every original physical data-row number
    represented by the row.
    """

    source_row_id: str
    source_row_numbers: tuple[int, ...]
    skill_name: str
    repo_name: str
    branch: str
    skill_path: str
    inventory_revision: str
    security_reviewed: str
    status: str
    raw_status: str
    raw_values: tuple[tuple[str, str], ...]
    source_selection_status: str = "UNSELECTED"
    conflict_fields: tuple[str, ...] = ()

    @property
    def row_number(self) -> int:
        """First original CSV data-row number (header is line 1)."""

        return self.source_row_numbers[0]

    @property
    def line_numbers(self) -> tuple[int, ...]:
        return self.source_row_numbers

    @property
    def original_row_numbers(self) -> tuple[int, ...]:
        return self.source_row_numbers

    @property
    def repository(self) -> str:
        return self.repo_name

    @property
    def source_branch(self) -> str:
        return self.branch

    @property
    def normalized_skill_path(self) -> str:
        return self.skill_path

    @property
    def inventory_skill_name(self) -> str:
        return self.skill_name

    @property
    def inventory_status(self) -> str:
        return self.status

    @property
    def inventory_review_hint(self) -> str:
        return self.security_reviewed

    @property
    def source_key(self) -> SourceKey:
        return SourceKey(self.repo_name, self.branch, self.skill_path, self.skill_name)

    @property
    def review_target_key(self) -> ReviewTargetKey:
        return ReviewTargetKey(self.repo_name, self.skill_path)

    @property
    def raw(self) -> dict[str, str]:
        return dict(self.raw_values)

    @property
    def has_input_conflict(self) -> bool:
        return self.source_selection_status == "INPUT_CONFLICT"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_row_id": self.source_row_id,
            "source_row_numbers": list(self.source_row_numbers),
            "skill_name": self.skill_name,
            "repo_name": self.repo_name,
            "branch": self.branch,
            "skill_path": self.skill_path,
            "inventory_revision": self.inventory_revision,
            "security_reviewed": self.security_reviewed,
            "status": self.status,
            "raw_status": self.raw_status,
            "source_key": self.source_key.to_dict(),
            "review_target_key": self.review_target_key.to_dict(),
            "source_selection_status": self.source_selection_status,
            "conflict_fields": list(self.conflict_fields),
        }


InventorySource = InventoryRow


@dataclass(frozen=True, slots=True)
class InventoryDocument:
    """Parsed CSV evidence and its de-duplicated execution rows."""

    rows: tuple[InventoryRow, ...]
    raw_rows: tuple[InventoryRow, ...]
    raw_csv_sha256: str
    headers: tuple[str, ...] = INVENTORY_COLUMNS

    @property
    def csv_sha256(self) -> str:
        return self.raw_csv_sha256

    @property
    def source_csv_sha256(self) -> str:
        return self.raw_csv_sha256

    @property
    def raw_sha256(self) -> str:
        return self.raw_csv_sha256

    @property
    def source_rows(self) -> tuple[InventoryRow, ...]:
        return self.raw_rows

    @property
    def raw_row_count(self) -> int:
        return len(self.raw_rows)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def duplicate_count(self) -> int:
        return len(self.raw_rows) - len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


def _resolve_status_mapping(
    status_mapping: Mapping[str, str] | None,
    status_map: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if status_mapping is not None and status_map is not None:
        raise TypeError("pass only one of status_mapping and status_map")
    mapping = status_mapping if status_mapping is not None else status_map
    if mapping is None:
        raise TypeError("status_mapping is required; unknown statuses must not be guessed")
    if not isinstance(mapping, Mapping):
        raise TypeError("status_mapping must be a mapping")
    return mapping


def _decode_csv(raw_bytes: bytes) -> str:
    try:
        # utf-8-sig accepts ordinary UTF-8 and removes exactly one leading BOM
        # for parsing.  The original bytes remain unchanged for hashing.
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InventoryError("CSV must be UTF-8 encoded") from exc


def _read_records(text: str) -> tuple[tuple[str, ...], list[tuple[int, tuple[str, ...]]]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = tuple(next(reader))
    except StopIteration as exc:
        raise InventoryHeaderError("CSV is empty") from exc
    except csv.Error as exc:
        raise InventoryHeaderError(f"cannot parse CSV header: {exc}") from exc

    if not header:
        raise InventoryHeaderError("CSV header is empty")
    duplicate_headers = sorted(
        {name for name in header if header.count(name) > 1}
    )
    if duplicate_headers:
        raise InventoryHeaderError(
            f"CSV header contains duplicate columns: {', '.join(duplicate_headers)}"
        )
    missing = [column for column in INVENTORY_COLUMNS if column not in header]
    unknown = [column for column in header if column not in INVENTORY_COLUMNS]
    if missing or unknown or len(header) != len(INVENTORY_COLUMNS):
        problems: list[str] = []
        if missing:
            problems.append("missing " + ", ".join(missing))
        if unknown:
            problems.append("unknown " + ", ".join(unknown))
        raise InventoryHeaderError("invalid CSV header: " + "; ".join(problems))

    records: list[tuple[int, tuple[str, ...]]] = []
    previous_end_line = reader.line_num
    try:
        for values in reader:
            end_line = reader.line_num
            start_line = previous_end_line + 1
            previous_end_line = end_line
            # csv.reader represents a physically blank line as [].  Ignore
            # blank lines between records and at EOF; a non-empty malformed
            # row still fails the width check below.
            if not values:
                continue
            if len(values) != len(header):
                raise InventoryRowError(
                    start_line,
                    f"expected {len(header)} columns, got {len(values)}",
                )
            records.append((start_line, tuple(values)))
    except csv.Error as exc:
        line_number = max(reader.line_num, 1)
        raise InventoryRowError(line_number, f"cannot parse CSV row: {exc}") from exc
    return header, records


def _required_text(raw_value: str, *, row_number: int, field: str) -> str:
    value = raw_value.strip()
    if not value:
        raise InventoryRowError(row_number, "value must not be empty", field=field)
    try:
        _reject_control_characters(value, field=field)
    except ValueError as exc:
        raise InventoryRowError(row_number, str(exc), field=field) from exc
    return value


def _build_row(
    row_number: int,
    header: Sequence[str],
    values: Sequence[str],
    status_mapping: Mapping[str, str],
) -> InventoryRow:
    raw = {column: values[index] for index, column in enumerate(header)}
    # Keep all seven raw cells in fixed order for stable IDs and audit output.
    raw_fixed = {column: raw[column] for column in INVENTORY_COLUMNS}
    skill_name = _required_text(raw_fixed["skill_name"], row_number=row_number, field="skill_name")
    repo_name = _required_text(raw_fixed["repo_name"], row_number=row_number, field="repo_name")

    raw_branch = _required_text(raw_fixed["branch"], row_number=row_number, field="branch")
    try:
        branch = normalize_branch(raw_branch)
    except (TypeError, ValueError) as exc:
        raise InventoryRowError(row_number, str(exc), field="branch") from exc

    raw_path = _required_text(raw_fixed["skill_path"], row_number=row_number, field="skill_path")
    try:
        skill_path = normalize_skill_path(raw_path)
    except (TypeError, ValueError) as exc:
        raise InventoryRowError(row_number, str(exc), field="skill_path") from exc

    raw_revision = _required_text(
        raw_fixed["lasted_commited"], row_number=row_number, field="lasted_commited"
    )
    try:
        inventory_revision = validate_revision(raw_revision)
    except (TypeError, ValueError) as exc:
        raise InventoryRowError(row_number, str(exc), field="lasted_commited") from exc

    security_reviewed = _required_text(
        raw_fixed["security_reviewed"], row_number=row_number, field="security_reviewed"
    )
    raw_status = _required_text(raw_fixed["status"], row_number=row_number, field="status")
    if raw_status not in status_mapping:
        raise UnknownStatusError(row_number, raw_status, status_mapping.keys())
    mapped_status = status_mapping[raw_status]
    if not isinstance(mapped_status, str) or not mapped_status.strip():
        raise InventoryError(
            f"status mapping for {raw_status!r} must be a non-empty string"
        )
    mapped_status = mapped_status.strip()

    source_row_id = make_source_row_id(raw_fixed)
    return InventoryRow(
        source_row_id=source_row_id,
        source_row_numbers=(row_number,),
        skill_name=skill_name,
        repo_name=repo_name,
        branch=branch,
        skill_path=skill_path,
        inventory_revision=inventory_revision,
        security_reviewed=security_reviewed,
        status=mapped_status,
        raw_status=raw_status,
        raw_values=tuple((column, raw_fixed[column]) for column in INVENTORY_COLUMNS),
    )


def _mark_conflicts(rows: Sequence[InventoryRow]) -> tuple[InventoryRow, ...]:
    by_source: dict[SourceKey, list[InventoryRow]] = defaultdict(list)
    for row in rows:
        by_source[row.source_key].append(row)

    conflict_by_source: dict[SourceKey, tuple[str, ...]] = {}
    for source_key, group in by_source.items():
        fields: list[str] = []
        if len({row.inventory_revision for row in group}) > 1:
            fields.append("inventory_revision")
        if len({row.raw_status for row in group}) > 1:
            fields.append("status")
        if fields:
            conflict_by_source[source_key] = tuple(fields)

    result: list[InventoryRow] = []
    for row in rows:
        fields = conflict_by_source.get(row.source_key)
        if fields:
            result.append(
                replace(
                    row,
                    source_selection_status="INPUT_CONFLICT",
                    conflict_fields=fields,
                )
            )
        else:
            result.append(row)
    return tuple(result)


def parse_inventory_csv(
    source: bytes | bytearray | str,
    status_mapping: Mapping[str, str] | None = None,
    *,
    status_map: Mapping[str, str] | None = None,
) -> InventoryDocument:
    """Parse CSV bytes/text into an :class:`InventoryDocument`.

    For ``bytes`` input the hash is over the exact bytes, including a UTF-8
    BOM and original newline style.  For ``str`` input UTF-8 encoding provides
    the byte representation used for the hash.  No file-system or Git lookup
    occurs here.
    """

    mapping = _resolve_status_mapping(status_mapping, status_map)
    if isinstance(source, str):
        raw_bytes = source.encode("utf-8")
    elif isinstance(source, (bytes, bytearray)):
        raw_bytes = bytes(source)
    else:
        raise TypeError("source must be CSV text or bytes")

    raw_csv_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    text = _decode_csv(raw_bytes)
    header, records = _read_records(text)
    parsed_rows = tuple(
        _build_row(row_number, header, values, mapping)
        for row_number, values in records
    )

    # Exact duplicates are identified from all original cells, before branch
    # and path normalisation.  This preserves the distinction between exact
    # duplicate input and two differently-spelled rows that normalise to one
    # source and may require conflict review.
    by_exact_row: dict[str, InventoryRow] = {}
    for row in parsed_rows:
        existing = by_exact_row.get(row.source_row_id)
        if existing is None:
            by_exact_row[row.source_row_id] = row
        else:
            by_exact_row[row.source_row_id] = replace(
                existing,
                source_row_numbers=existing.source_row_numbers + row.source_row_numbers,
            )
    deduplicated = _mark_conflicts(tuple(by_exact_row.values()))

    # Apply the same conflict marker to the full audit view, while keeping
    # each duplicate's individual line number available.
    marker_by_id = {row.source_row_id: row for row in deduplicated}
    raw_rows = tuple(
        replace(
            row,
            source_selection_status=marker_by_id[row.source_row_id].source_selection_status,
            conflict_fields=marker_by_id[row.source_row_id].conflict_fields,
        )
        for row in parsed_rows
    )
    return InventoryDocument(
        rows=deduplicated,
        raw_rows=raw_rows,
        raw_csv_sha256=raw_csv_sha256,
        headers=tuple(header),
    )


def load_inventory_csv(
    path: str | Path,
    status_mapping: Mapping[str, str] | None = None,
    *,
    status_map: Mapping[str, str] | None = None,
) -> InventoryDocument:
    """Load and parse a UTF-8 CSV file from ``path``."""

    file_path = Path(path)
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"cannot read inventory CSV {file_path}: {exc}") from exc
    return parse_inventory_csv(
        raw_bytes,
        status_mapping=status_mapping,
        status_map=status_map,
    )


# Short aliases make the module convenient for callers while retaining the
# explicit names above for code that wants to distinguish file loading from
# parsing in-memory data.
read_inventory_csv = load_inventory_csv
load_inventory = load_inventory_csv
parse_csv = parse_inventory_csv


class InventoryLoader:
    """Reusable loader carrying one configured status mapping."""

    def __init__(self, status_mapping: Mapping[str, str]):
        self.status_mapping = _resolve_status_mapping(status_mapping, None)

    def parse(self, source: bytes | bytearray | str) -> InventoryDocument:
        return parse_inventory_csv(source, status_mapping=self.status_mapping)

    def load(self, path: str | Path) -> InventoryDocument:
        return load_inventory_csv(path, status_mapping=self.status_mapping)


__all__ = [
    "INVENTORY_COLUMNS",
    "REQUIRED_COLUMNS",
    "InventoryDocument",
    "InventoryError",
    "InventoryHeaderError",
    "InventoryLoader",
    "InventoryRow",
    "InventoryRowError",
    "InventorySource",
    "UnknownStatusError",
    "load_inventory",
    "load_inventory_csv",
    "make_source_row_id",
    "normalize_branch",
    "normalize_skill_path",
    "parse_csv",
    "parse_inventory_csv",
    "read_inventory_csv",
    "validate_revision",
]
