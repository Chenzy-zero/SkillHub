"""Unit tests for the shell-free static scanner adapters."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from skill_batch_review.scanners import (
    CiscoSkillScannerAdapter,
    CommandExecution,
    CommandRunner,
    SCANNER_STATUS_COMPLETED,
    SCANNER_STATUS_ERROR,
    SCANNER_STATUS_INCOMPLETE,
    SkillSpectorAdapter,
    ScannerConfigurationError,
    compare_directory_summaries,
    parse_json_report,
    summarize_directory,
)


class FakeRunner:
    """A runner double that writes a report and never executes Skill content."""

    def __init__(self, *, returncode: int | None = 0, report: object | None = None, mutate=False):
        self.returncode = returncode
        self.report = report
        self.mutate = mutate
        self.calls: list[dict[str, object]] = []

    def run(self, argv, *, timeout_seconds, cwd=None, env=None):
        command = tuple(argv)
        self.calls.append(
            {
                "argv": command,
                "timeout_seconds": timeout_seconds,
                "cwd": cwd,
                "env": env,
            }
        )
        root = Path(command[2])
        output = Path(command[command.index("--output") + 1])
        if self.report is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(self.report, ensure_ascii=False), encoding="utf-8")
        if self.mutate:
            (root / "scanner-created.txt").write_text("unexpected", encoding="utf-8")
        return CommandExecution(
            argv=command,
            returncode=self.returncode,
            stdout="runner stdout",
            stderr="runner stderr",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            started_at="2026-08-31T00:00:00Z",
            finished_at="2026-08-31T00:00:01Z",
            duration_seconds=1.0,
        )


class ScannerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.skill_root = self.base / "skill"
        self.skill_root.mkdir()
        (self.skill_root / "SKILL.md").write_text("# Read-only fixture\n", encoding="utf-8")
        (self.skill_root / ".hidden").write_text("hidden\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_approved_commands_have_only_static_flags(self) -> None:
        cisco = CiscoSkillScannerAdapter()
        spector = SkillSpectorAdapter()
        cisco_command = cisco.build_command(self.skill_root, self.base / "cisco.json")
        spector_command = spector.build_command(self.skill_root, self.base / "spector.json")

        self.assertEqual(
            cisco_command,
            (
                "skill-scanner",
                "scan",
                str(self.skill_root),
                "--format",
                "json",
                "--compact",
                "--output",
                str(self.base / "cisco.json"),
            ),
        )
        self.assertEqual(
            spector_command,
            (
                "skillspector",
                "scan",
                str(self.skill_root),
                "--no-llm",
                "--format",
                "json",
                "--output",
                str(self.base / "spector.json"),
            ),
        )
        self.assertFalse(any("llm" in item.lower() for item in cisco_command))
        self.assertFalse(any("behavioral" in item.lower() for item in cisco_command))
        self.assertFalse(any("virustotal" in item.lower() for item in cisco_command))
        self.assertFalse(any("aidefense" in item.lower() for item in cisco_command))

    def test_version_record_never_starts_scanner_entry_point(self) -> None:
        runner = FakeRunner()
        adapter = CiscoSkillScannerAdapter(
            executable="scanner/skill-scanner.exe",
            tool_version="2.0.13",
            runner=runner,
        )

        record = adapter.detect_version()

        self.assertEqual(record.version, "2.0.13")
        self.assertEqual(record.version_command, ())
        self.assertIsNone(record.version_exit_code)
        self.assertEqual(runner.calls, [])

    def test_summary_includes_hidden_files_and_does_not_follow_symlink(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.skill_root / "outside-link"
        try:
            link.symlink_to(outside)
        except OSError as exc:  # pragma: no cover - unusual restricted filesystems
            self.skipTest(f"symlinks unavailable: {exc}")

        summary = summarize_directory(self.skill_root)
        paths = {entry.relative_path for entry in summary.entries}
        self.assertIn("SKILL.md", paths)
        self.assertIn(".hidden", paths)
        self.assertIn("outside-link", paths)
        link_entry = next(entry for entry in summary.entries if entry.relative_path == "outside-link")
        self.assertEqual(link_entry.kind, "symlink")
        self.assertEqual(summary.file_count, 2)

        changed = summarize_directory(self.skill_root)
        (self.skill_root / "new-file").write_text("new", encoding="utf-8")
        after = summarize_directory(self.skill_root)
        comparison = compare_directory_summaries(changed, after)
        self.assertFalse(comparison.unchanged)
        self.assertEqual(comparison.before_digest, changed.digest)

    def test_cisco_preserves_raw_report_and_records_digests(self) -> None:
        report = {
            "findings": [],
            "scanner_extension": {"new_field": "retained in raw report"},
        }
        runner = FakeRunner(report=report)
        raw_path = self.base / "evidence" / "cisco.raw.json"
        result = CiscoSkillScannerAdapter(
            runner=runner,
            tool_version="1.4.0",
            timeout_seconds=42,
        ).scan(
            self.skill_root,
            output_file=self.base / "work" / "cisco.json",
            raw_report_file=raw_path,
        )

        self.assertEqual(result.status, SCANNER_STATUS_COMPLETED)
        self.assertTrue(result.tool_ok)
        self.assertTrue(result.completed)
        self.assertEqual(result.decision, "PASS")
        self.assertEqual(result.tool_version.version, "1.4.0")
        self.assertEqual(result.report_sha256, __import__("hashlib").sha256(raw_path.read_bytes()).hexdigest())
        self.assertEqual(raw_path.read_bytes(), json.dumps(report).encode("utf-8"))
        self.assertEqual(result.command_digest, result.command_digest)
        self.assertEqual(result.config_digest, result.scanner_config_digest)
        self.assertEqual(len(runner.calls), 1)
        scanner_environment = runner.calls[0]["env"]
        self.assertIsInstance(scanner_environment, dict)
        self.assertEqual(
            scanner_environment["LITELLM_LOCAL_MODEL_COST_MAP"],
            "True",
        )

    def test_cisco_nonzero_with_valid_report_is_not_a_pass(self) -> None:
        runner = FakeRunner(
            returncode=1,
            report={
                "findings": [
                    {
                        "id": "C-1",
                        "severity": "high",
                        "description": "dangerous command",
                    }
                ]
            },
        )
        result = CiscoSkillScannerAdapter(runner=runner).scan(
            self.skill_root,
            output_file=self.base / "cisco.json",
        )
        self.assertEqual(result.status, SCANNER_STATUS_COMPLETED)
        self.assertTrue(result.report_complete)
        self.assertFalse(result.tool_ok)
        self.assertNotEqual(result.decision, "PASS")
        self.assertEqual(result.findings[0].severity, "HIGH")

    def test_skillspector_exit_one_is_completed_do_not_install(self) -> None:
        runner = FakeRunner(returncode=1, report={"findings": []})
        result = SkillSpectorAdapter(runner=runner).scan(
            self.skill_root,
            output_file=self.base / "spector.json",
        )
        self.assertEqual(result.status, SCANNER_STATUS_COMPLETED)
        self.assertTrue(result.report_complete)
        self.assertTrue(result.tool_ok)
        self.assertEqual(result.decision, "DO_NOT_INSTALL")

    def test_skillspector_exit_two_is_execution_error(self) -> None:
        runner = FakeRunner(returncode=2, report={"findings": []})
        result = SkillSpectorAdapter(runner=runner).scan(
            self.skill_root,
            output_file=self.base / "spector.json",
        )
        self.assertEqual(result.status, SCANNER_STATUS_ERROR)
        self.assertFalse(result.tool_ok)
        self.assertIn("code 2", " ".join(result.errors))

    def test_missing_required_finding_fields_is_incomplete(self) -> None:
        parsed = parse_json_report(
            json.dumps({"findings": [{"rule_id": "R-1", "path": "SKILL.md"}]}),
            scanner="cisco",
        )
        self.assertFalse(parsed.complete)
        self.assertFalse(parsed.findings[0].complete)
        self.assertEqual(set(parsed.findings[0].missing_fields), {"severity", "message"})

        runner = FakeRunner(report={"findings": [{"rule_id": "R-1"}]})
        result = CiscoSkillScannerAdapter(runner=runner).scan(
            self.skill_root,
            output_file=self.base / "cisco.json",
        )
        self.assertEqual(result.status, SCANNER_STATUS_INCOMPLETE)
        self.assertFalse(result.completed)

    def test_parser_accepts_extended_official_style_fields(self) -> None:
        parsed = parse_json_report(
            json.dumps(
                {
                    "results": [
                        {
                            "ruleId": "prompt.tool.1",
                            "riskLevel": "high",
                            "description": "untrusted tool instruction",
                            "location": {"path": "SKILL.md", "line": "8", "column": 2},
                            "futureOfficialField": {"arbitrary": True},
                        }
                    ],
                    "futureSummary": {"new": True},
                }
            ),
            scanner="skillspector",
        )
        self.assertTrue(parsed.complete)
        finding = parsed.findings[0]
        self.assertEqual(finding.rule_id, "prompt.tool.1")
        self.assertEqual(finding.severity, "HIGH")
        self.assertEqual(finding.path, "SKILL.md")
        self.assertEqual(finding.line, 8)
        self.assertEqual(finding.column, 2)
        self.assertEqual(finding.raw["futureOfficialField"], {"arbitrary": True})

    def test_directory_mutation_during_scan_is_incomplete(self) -> None:
        runner = FakeRunner(report={"findings": []}, mutate=True)
        result = CiscoSkillScannerAdapter(runner=runner).scan(
            self.skill_root,
            output_file=self.base / "cisco.json",
        )
        self.assertEqual(result.status, SCANNER_STATUS_INCOMPLETE)
        self.assertFalse(result.tool_ok)
        self.assertIsNotNone(result.directory_comparison)
        self.assertFalse(result.directory_comparison.unchanged)

    def test_report_paths_inside_skill_root_are_rejected(self) -> None:
        with self.assertRaises(ScannerConfigurationError):
            CiscoSkillScannerAdapter(runner=FakeRunner()).scan(
                self.skill_root,
                output_file=self.skill_root / "report.json",
            )

    def test_existing_output_is_rejected_to_prevent_stale_report_reuse(self) -> None:
        output = self.base / "existing.json"
        output.write_text('{"findings": []}', encoding="utf-8")
        with self.assertRaisesRegex(ScannerConfigurationError, "stale report"):
            CiscoSkillScannerAdapter(runner=FakeRunner()).scan(
                self.skill_root,
                output_file=output,
            )

    def test_canonical_snapshot_digest_is_bound_but_not_compared_to_local_summary(self) -> None:
        canonical_digest = "a" * 64
        result = CiscoSkillScannerAdapter(runner=FakeRunner(report={"findings": []})).scan(
            self.skill_root,
            output_file=self.base / "canonical.json",
            skill_digest=canonical_digest,
        )
        self.assertEqual(result.status, SCANNER_STATUS_COMPLETED)
        self.assertEqual(result.skill_digest, canonical_digest)
        self.assertNotEqual(result.before_summary.digest, canonical_digest)

    def test_report_exactly_at_parse_limit_is_not_marked_too_large(self) -> None:
        report = {"findings": []}
        encoded = json.dumps(report, ensure_ascii=False).encode("utf-8")
        result = CiscoSkillScannerAdapter(
            runner=FakeRunner(report=report),
            max_report_bytes=len(encoded),
        ).scan(self.skill_root, output_file=self.base / "bounded.json")
        self.assertEqual(result.status, SCANNER_STATUS_COMPLETED)


class CommandRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _script(self, body: str) -> Path:
        path = self.base / "fake-scanner.py"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_shell_free_runner_bounds_stdout_and_stderr(self) -> None:
        script = self._script(
            """
            import sys
            sys.stdout.write('o' * 10000)
            sys.stderr.write('e' * 10000)
            """
        )
        runner = CommandRunner(max_capture_bytes=128)
        result = runner.run((str(script),), timeout_seconds=5)
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 128)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), 128)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_runner_timeout_does_not_execute_shell(self) -> None:
        script = self._script(
            """
            import time
            time.sleep(10)
            """
        )
        runner = CommandRunner(max_capture_bytes=128)
        result = runner.run((str(script),), timeout_seconds=0.1)
        self.assertTrue(result.timed_out)
        self.assertIsNotNone(result.returncode)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
