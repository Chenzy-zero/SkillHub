"""Unit tests for the CSV inventory boundary.

These tests intentionally do not invoke Git or any network service.  The
inventory loader is only responsible for validating source evidence; branch
existence and revision resolution are later pipeline stages.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from skill_batch_review.inventory import (  # noqa: E402
    INVENTORY_COLUMNS,
    LEGACY_INVENTORY_COLUMNS,
    InventoryError,
    InventoryHeaderError,
    InventoryLoader,
    InventoryRowError,
    UnknownStatusError,
    load_inventory_csv,
    make_source_row_id,
    normalize_branch,
    normalize_skill_path,
    parse_inventory_csv,
    validate_revision,
)


ACTIVE = "a" * 40
ACTIVE_2 = "b" * 40
ACTIVE_3 = "c" * 40
STATUS_MAP = {"active": "ACTIVE", "disabled": "DISABLED"}


def csv_text(*rows: tuple[str, ...], header: tuple[str, ...] = INVENTORY_COLUMNS) -> str:
    lines = [",".join(header)]
    lines.extend(",".join(row) for row in rows)
    return "\n".join(lines) + "\n"


def inventory_row(
    *,
    skill_name: str = "demo",
    repo_name: str = "team/demo",
    branch: str = "main",
    skill_path: str = "skills/demo",
    revision: str = ACTIVE,
    security_reviewed: str = "否",
    status: str = "active",
) -> tuple[str, ...]:
    return (
        skill_name,
        repo_name,
        branch,
        skill_path,
        revision,
        security_reviewed,
        status,
    )


class InventoryNormalisationTests(unittest.TestCase):
    def test_branch_normalisation_removes_heads_prefix_and_separators(self):
        self.assertEqual(normalize_branch(" refs/heads/feature/demo "), "feature/demo")
        for value in ("feature\\demo", "feature//demo", "feature/./demo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_branch(value)

    def test_branch_rejects_parent_component(self):
        with self.assertRaises(ValueError):
            normalize_branch("refs/heads/feature/../main")
        with self.assertRaises(ValueError):
            normalize_branch("refs/tags/v1")

    def test_path_normalisation_and_repository_root(self):
        self.assertEqual(normalize_skill_path("/"), ".")
        self.assertEqual(normalize_skill_path("./skills/demo/"), "skills/demo")
        self.assertEqual(normalize_skill_path("."), ".")

    def test_path_rejects_absolute_and_traversal_forms(self):
        for value in ("/tmp/skill", "//server/share", "C:\\skills\\demo", "a/../b", "a\\..\\b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_skill_path(value)

    def test_revision_basic_shape_is_checked_without_git_lookup(self):
        self.assertEqual(validate_revision("A" * 40), "a" * 40)
        self.assertEqual(validate_revision("A" * 64), "a" * 64)
        for value in ("", "abc1234", "not-a-revision", "g" * 40, "a" * 41, "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_revision(value)


class InventoryParsingTests(unittest.TestCase):
    def test_utf8_bom_and_original_bytes_sha256_are_preserved(self):
        raw = ("\ufeff" + ",".join(INVENTORY_COLUMNS) + "\r\n" + ",".join(
            inventory_row(skill_name="中文技能", security_reviewed="是")
        ) + "\r\n").encode("utf-8")
        document = parse_inventory_csv(raw, status_mapping=STATUS_MAP)

        self.assertEqual(document.raw_csv_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(document.source_csv_sha256, document.raw_csv_sha256)
        self.assertEqual(document.source_encoding, "utf-8-sig")
        self.assertEqual(document.headers, INVENTORY_COLUMNS)
        self.assertEqual(len(document.rows), 1)
        row = document.rows[0]
        self.assertEqual(row.skill_name, "中文技能")
        self.assertEqual(row.status, "ACTIVE")
        self.assertEqual(row.raw_status, "active")
        self.assertEqual(row.security_reviewed, "是")
        self.assertEqual(row.source_row_numbers, (2,))

    def test_gbk_csv_is_auto_decoded_and_source_bytes_are_preserved(self):
        text = csv_text(
            inventory_row(skill_name="中文技能", security_reviewed="否", status="启用")
        )
        raw = text.encode("gbk")
        document = parse_inventory_csv(
            raw,
            status_mapping={"启用": "ACTIVE"},
        )

        self.assertEqual(document.source_encoding, "gb18030")
        self.assertEqual(document.raw_csv_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(document.rows[0].skill_name, "中文技能")
        self.assertEqual(document.rows[0].status, "ACTIVE")

    def test_utf16_excel_csv_is_auto_decoded(self):
        text = csv_text(
            inventory_row(skill_name="Excel技能", security_reviewed="否", status="启用")
        )
        raw = text.encode("utf-16")
        document = parse_inventory_csv(
            raw,
            status_mapping={"启用": "ACTIVE"},
        )

        self.assertEqual(document.source_encoding, "utf-16-le")
        self.assertEqual(document.raw_csv_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(document.rows[0].skill_name, "Excel技能")

    def test_unknown_binary_encoding_is_rejected_clearly(self):
        with self.assertRaisesRegex(InventoryError, "UTF-8, UTF-16 with BOM, or GBK/GB18030"):
            parse_inventory_csv(b"\xff\xff\xff", status_mapping=STATUS_MAP)

    def test_header_requires_core_columns_and_rejects_duplicates(self):
        base = list(INVENTORY_COLUMNS)
        cases = [
            tuple(column for column in base if column != "status"),
            tuple(base[:-1] + ["status", "status"]),
        ]
        for header in cases:
            with self.subTest(header=header), self.assertRaises(InventoryHeaderError):
                parse_inventory_csv(csv_text(inventory_row(), header=header), status_mapping=STATUS_MAP)

    def test_unknown_extension_columns_are_accepted_and_retained(self):
        header = tuple(INVENTORY_COLUMNS) + ("department", "custom_owner")
        values = inventory_row() + ("研发一部", "owner@example.test")
        document = parse_inventory_csv(
            csv_text(values, header=header),
            status_mapping=STATUS_MAP,
        )

        self.assertEqual(document.headers, header)
        self.assertEqual(document.rows[0].raw["department"], "研发一部")
        self.assertEqual(
            document.rows[0].to_dict()["raw_values"]["custom_owner"],
            "owner@example.test",
        )

    def test_extension_values_keep_otherwise_equal_rows_distinct(self):
        header = tuple(INVENTORY_COLUMNS) + ("department",)
        first = inventory_row() + ("研发一部",)
        second = inventory_row() + ("研发二部",)
        document = parse_inventory_csv(
            csv_text(first, second, header=header),
            status_mapping=STATUS_MAP,
        )

        self.assertEqual(document.row_count, 2)
        self.assertNotEqual(document.rows[0].source_row_id, document.rows[1].source_row_id)

    def test_legacy_revision_header_remains_compatible(self):
        document = parse_inventory_csv(
            csv_text(inventory_row(), header=LEGACY_INVENTORY_COLUMNS),
            status_mapping=STATUS_MAP,
        )
        row = document.rows[0]
        self.assertEqual(row.inventory_revision, ACTIVE)
        self.assertEqual(row.inventory_revision_field, "lasted_commited")
        self.assertEqual(row.raw["lasted_commited"], ACTIVE)

        current = parse_inventory_csv(
            csv_text(inventory_row(), header=INVENTORY_COLUMNS),
            status_mapping=STATUS_MAP,
        ).rows[0]
        self.assertEqual(row.source_row_id, current.source_row_id)

    def test_release_inventory_trace_columns_are_retained(self):
        header = (
            "skill_id",
            "skill_name",
            "repo_name",
            "branch",
            "skill_path",
            "latest_commitid",
            "security_reviewed",
            "status",
            "update_time",
            "history_id",
        )
        values = (
            "skill-001",
            "demo",
            "team/demo",
            "master",
            "skills/demo",
            ACTIVE,
            "否",
            "新增",
            "2026/08/31 10:00:00",
            "7",
        )
        document = parse_inventory_csv(
            csv_text(values, header=header),
            status_mapping={"新增": "ACTIVE"},
        )
        row = document.rows[0]
        self.assertEqual(row.inventory_revision, ACTIVE)
        self.assertEqual(row.inventory_revision_field, "latest_commitid")
        self.assertEqual(row.trace_values, {
            "skill_id": "skill-001",
            "update_time": "2026/08/31 10:00:00",
            "history_id": "7",
        })
        self.assertEqual(row.to_dict()["raw_values"], dict(zip(header, values)))
        self.assertEqual(row.to_dict()["trace_values"], row.trace_values)

    def test_both_revision_spellings_are_rejected_as_ambiguous(self):
        header = tuple(INVENTORY_COLUMNS) + ("lasted_commited",)
        values = inventory_row() + (ACTIVE,)
        with self.assertRaisesRegex(InventoryHeaderError, "only one commit revision"):
            parse_inventory_csv(csv_text(values, header=header), status_mapping=STATUS_MAP)

    def test_release_inventory_csv_is_readable_without_network(self):
        path = Path(__file__).resolve().parents[2] / "test" / "skill_summary.csv"
        document = load_inventory_csv(
            path,
            status_mapping={"新增": "ACTIVE", "修改": "ACTIVE", "删除": "DELETED"},
        )
        self.assertEqual(document.raw_row_count, 29)
        self.assertEqual(document.row_count, 29)
        self.assertEqual(
            {row.raw_status for row in document.rows}, {"新增", "修改", "删除"}
        )
        self.assertEqual(
            sum(row.status == "ACTIVE" for row in document.rows), 17
        )
        self.assertEqual(
            sum(row.status == "DELETED" for row in document.rows), 12
        )
        first = document.rows[0]
        self.assertEqual(first.raw["skill_id"], "3frxmfhn764fvevrx1st2b48neglyvc7")
        self.assertEqual(first.raw["history_id"], "1")

    def test_required_values_and_record_width_are_rejected(self):
        blank_name = inventory_row(skill_name=" ")
        with self.assertRaises(InventoryRowError) as ctx:
            parse_inventory_csv(csv_text(blank_name), status_mapping=STATUS_MAP)
        self.assertEqual(ctx.exception.row_number, 2)
        self.assertIn("skill_name", str(ctx.exception))

        malformed = csv_text(inventory_row()) + "too,few,columns\n"
        with self.assertRaises(InventoryRowError) as ctx:
            parse_inventory_csv(malformed, status_mapping=STATUS_MAP)
        self.assertEqual(ctx.exception.row_number, 3)

    def test_unknown_status_is_an_error_and_mapping_is_not_guessed(self):
        with self.assertRaises(UnknownStatusError) as ctx:
            parse_inventory_csv(csv_text(inventory_row(status="pending")), status_mapping=STATUS_MAP)
        self.assertEqual(ctx.exception.row_number, 2)
        self.assertEqual(ctx.exception.status, "pending")
        self.assertIn("active", ctx.exception.known_statuses)

    def test_security_reviewed_is_retained_but_does_not_skip_row(self):
        document = parse_inventory_csv(
            csv_text(
                inventory_row(security_reviewed="是"),
                inventory_row(skill_name="not-reviewed", security_reviewed="否"),
            ),
            status_mapping=STATUS_MAP,
        )
        self.assertEqual(len(document.rows), 2)
        self.assertEqual([row.security_reviewed for row in document.rows], ["是", "否"])

    def test_exact_duplicates_are_deduplicated_and_all_line_numbers_remain(self):
        row = inventory_row()
        content = csv_text(row, row, inventory_row(skill_name="second", revision=ACTIVE_2))
        document = parse_inventory_csv(content, status_mapping=STATUS_MAP)

        self.assertEqual(document.raw_row_count, 3)
        self.assertEqual(document.row_count, 2)
        self.assertEqual(document.duplicate_count, 1)
        first = document.rows[0]
        self.assertEqual(first.source_row_numbers, (2, 3))
        self.assertEqual(first.original_row_numbers, (2, 3))
        self.assertEqual(len(document.raw_rows), 3)
        self.assertEqual(document.raw_rows[0].source_row_numbers, (2,))
        self.assertEqual(document.raw_rows[1].source_row_numbers, (3,))

        again = parse_inventory_csv(content, status_mapping=STATUS_MAP)
        self.assertEqual(
            [row.source_row_id for row in document.rows],
            [row.source_row_id for row in again.rows],
        )

    def test_source_row_id_is_content_stable_when_line_position_changes(self):
        first = parse_inventory_csv(csv_text(inventory_row()), status_mapping=STATUS_MAP)
        second = parse_inventory_csv(
            csv_text(inventory_row(skill_name="other"), inventory_row()),
            status_mapping=STATUS_MAP,
        )
        self.assertEqual(first.rows[0].source_row_id, second.rows[1].source_row_id)
        self.assertEqual(first.rows[0].source_row_id, make_source_row_id(first.rows[0].raw))

    def test_same_source_with_different_revision_or_status_is_marked_conflict(self):
        content = csv_text(
            inventory_row(revision=ACTIVE, status="active"),
            inventory_row(revision=ACTIVE_2, status="disabled"),
        )
        document = parse_inventory_csv(content, status_mapping=STATUS_MAP)

        self.assertEqual(len(document.rows), 2)
        for row in document.rows:
            self.assertTrue(row.has_input_conflict)
            self.assertEqual(row.source_selection_status, "INPUT_CONFLICT")
            self.assertEqual(row.conflict_fields, ("inventory_revision", "status"))
        self.assertTrue(all(row.has_input_conflict for row in document.raw_rows))

    def test_normalised_branch_and_path_form_one_source_key(self):
        content = csv_text(
            inventory_row(branch="refs/heads/main", skill_path="./skills/demo"),
            inventory_row(branch="main", skill_path="skills/demo"),
        )
        document = parse_inventory_csv(content, status_mapping=STATUS_MAP)
        self.assertEqual(len(document.rows), 2)  # not exact duplicates
        self.assertEqual(document.rows[0].source_key, document.rows[1].source_key)
        self.assertFalse(document.rows[0].has_input_conflict)

    def test_file_loader_and_aliases_use_same_parser(self):
        import tempfile

        content = csv_text(inventory_row())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.csv"
            path.write_bytes(content.encode("utf-8"))
            document = load_inventory_csv(path, status_mapping=STATUS_MAP)
            via_loader = InventoryLoader(STATUS_MAP).load(path)
        self.assertEqual(document.raw_csv_sha256, via_loader.raw_csv_sha256)
        self.assertEqual(document.rows[0].source_row_id, via_loader.rows[0].source_row_id)

    def test_missing_status_mapping_is_rejected_explicitly(self):
        with self.assertRaises(TypeError):
            parse_inventory_csv(csv_text(inventory_row()))


if __name__ == "__main__":
    unittest.main()
