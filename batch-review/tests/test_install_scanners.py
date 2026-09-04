"""Tests for the cross-platform scanner installer helpers."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "install_scanners.py"
SPEC = importlib.util.spec_from_file_location("install_scanners", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ScannerInstallerTests(unittest.TestCase):
    def test_packages_are_pinned(self):
        self.assertEqual(
            [package.requirement for package in MODULE.SCANNERS],
            ["cisco-ai-skill-scanner==2.0.13", "skillspector==2.5.1"],
        )
        self.assertEqual(MODULE.UV_VERSION, "0.12.9")

    def test_bundled_skillspector_wheel_matches_official_digest(self):
        package = MODULE.SCANNERS[1]
        requirement = MODULE._install_requirement(package)
        self.assertTrue(requirement.endswith("skillspector-2.5.1-py3-none-any.whl"))
        self.assertEqual(
            MODULE._sha256(Path(requirement)),
            "56196f2f8689cc6e7f565181f06db5e489ba010ef0e5da19855d99043a5f6415",
        )

    def test_bundled_windows_python_matches_official_digest(self):
        installer = MODULE._bundled_windows_python_installer()
        self.assertEqual(installer.name, "python-3.13.15-amd64.exe")
        self.assertEqual(MODULE._sha256(installer), MODULE.WINDOWS_PYTHON_SHA256)

    def test_windows_python_install_is_private_and_does_not_change_path(self):
        command = MODULE._windows_python_install_command(
            Path("python-3.13.15-amd64.exe"),
            Path("scanner-tools/python313"),
        )
        self.assertIn("InstallAllUsers=0", command)
        self.assertIn("Include_launcher=0", command)
        self.assertIn("PrependPath=0", command)
        self.assertIn("AppendPath=0", command)
        self.assertIn("Shortcuts=0", command)
        self.assertIn("AssociateFiles=0", command)

    def test_uv_uses_bundled_skillspector_wheel(self):
        package = MODULE.SCANNERS[1]
        requirement = MODULE._install_requirement(package)
        command = MODULE._uv_install_command(
            Path("resolver/uv"),
            Path("scanner/python"),
            package,
            requirement=requirement,
        )
        self.assertEqual(command[-1], requirement)
        self.assertNotEqual(command[-1], package.requirement)

    def test_skillspector_wheel_is_installed_without_metadata_dependencies(self):
        package = MODULE.SCANNERS[1]
        command = MODULE._uv_install_command(
            Path("resolver/uv"),
            Path("scanner/python"),
            package,
            requirement=MODULE._install_requirement(package),
            no_deps=True,
        )
        self.assertIn("--no-deps", command)

    def test_skillspector_runtime_excludes_unused_langgraph_dev_server(self):
        package = MODULE.SCANNERS[1]
        requirements = MODULE._skillspector_runtime_requirements(package)
        content = "\n".join(
            line
            for line in requirements.read_text(encoding="utf-8").lower().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("langgraph-cli", content)
        self.assertNotIn("forbiddenfruit", content)
        self.assertNotIn("blockbuster", content)
        self.assertNotIn("botocore", content)
        self.assertIn("boto3>=1.34.0", content)
        self.assertIn("yara-python>=4.5.0", content)

    def test_runtime_requirements_install_resolves_full_wheel_only_closure(self):
        command = MODULE._uv_requirements_command(
            Path("resolver/uv"),
            Path("scanner/python.exe"),
            Path("packages/runtime.txt"),
        )
        self.assertEqual(command[0:3], ("resolver/uv", "pip", "install"))
        self.assertIn("--only-binary", command)
        self.assertIn("--upgrade", command)
        self.assertIn("-r", command)
        self.assertEqual(command[-1], "packages/runtime.txt")

    def test_cisco_static_profile_uninstalls_litellm(self):
        command = MODULE._uv_uninstall_command(
            Path("resolver/uv"), Path("scanner/python.exe"), "litellm"
        )
        self.assertEqual(
            command,
            (
                "resolver/uv",
                "pip",
                "uninstall",
                "--python",
                "scanner/python.exe",
                "litellm",
            ),
        )

    def test_smoke_environment_blocks_optional_network_clients(self):
        environment = MODULE._smoke_environment({}, Path("empty-cache"))
        self.assertEqual(environment["TIKTOKEN_CACHE_DIR"], "empty-cache")
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:9")
        self.assertEqual(environment["LITELLM_LOCAL_MODEL_COST_MAP"], "True")

    def test_smoke_commands_keep_both_scanners_in_static_mode(self):
        cisco = MODULE._smoke_command(
            MODULE.SCANNERS[0], Path("cisco"), Path("skill"), Path("cisco.json")
        )
        spector = MODULE._smoke_command(
            MODULE.SCANNERS[1], Path("spector"), Path("skill"), Path("spector.json")
        )
        spector_stdout = MODULE._smoke_command(
            MODULE.SCANNERS[1], Path("spector"), Path("skill"), None
        )
        self.assertNotIn("--use-llm", cisco)
        self.assertIn("--no-llm", spector)
        self.assertEqual(cisco[-1], "cisco.json")
        self.assertEqual(spector[-1], "spector.json")
        self.assertNotIn("--output", spector_stdout)

    def test_skillspector_smoke_falls_back_to_json_stdout_on_windows_output_issue(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "SKILL.md").write_text("# smoke\n", encoding="utf-8")
            executions = [
                MODULE.subprocess.CompletedProcess(("skillspector",), 0, "", ""),
                MODULE.subprocess.CompletedProcess(
                    ("skillspector",), 0, '{"findings": []}', ""
                ),
            ]
            with (
                patch.object(MODULE, "SCANNER_SMOKE_SKILL", fixture),
                patch.object(MODULE.subprocess, "run", side_effect=executions) as run,
            ):
                MODULE._smoke_scanner(
                    MODULE.SCANNERS[1],
                    Path("skillspector"),
                    root=root,
                    package_environment={},
                )

            self.assertEqual(run.call_count, 2)
            self.assertIn("--output", run.call_args_list[0].args[0])
            self.assertNotIn("--output", run.call_args_list[1].args[0])

    def test_index_url_is_passed_by_environment_not_command_line(self):
        environment = MODULE._pip_environment("https://mirror.example/simple")
        self.assertEqual(environment["PIP_INDEX_URL"], "https://mirror.example/simple")
        self.assertEqual(environment["UV_INDEX_URL"], "https://mirror.example/simple")
        self.assertEqual(environment["UV_LINK_MODE"], "copy")

    def test_cisco_repair_recreates_same_version_environment(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            environment = root / "cisco"
            environment.mkdir()
            base_python = root / "python.exe"
            base_python.write_text("python", encoding="utf-8")
            with (
                patch.object(MODULE, "_venv_python", return_value=base_python),
                patch.object(
                    MODULE,
                    "_python_identity",
                    return_value=(base_python, (3, 14)),
                ),
                patch.object(MODULE, "_run") as run,
            ):
                selected = MODULE._ensure_scanner_environment(
                    environment,
                    base_python,
                    recreate=True,
                )

            self.assertEqual(selected, base_python)
            command = run.call_args.args[0]
            self.assertIn("--clear", command)
            self.assertEqual(command[-1], str(environment))

    def test_uv_installs_exact_scanner_version_into_selected_environment(self):
        command = MODULE._uv_install_command(
            Path("resolver/uv"),
            Path("scanner/python"),
            MODULE.SCANNERS[0],
        )
        self.assertEqual(command[0:3], ("resolver/uv", "pip", "install"))
        self.assertIn("--python", command)
        self.assertIn("--only-binary", command)
        self.assertEqual(command[-1], "cisco-ai-skill-scanner==2.0.13")

    def test_windows_cisco_has_one_pinned_source_build_exception(self):
        command = MODULE._uv_install_command(
            Path("resolver/uv"),
            Path("scanner/python.exe"),
            MODULE.SCANNERS[0],
            platform_name="nt",
        )
        self.assertIn("--only-binary", command)
        self.assertEqual(command.count("--no-binary"), 1)
        self.assertIn("win-unicode-console", command)
        self.assertEqual(command[-1], "win-unicode-console==0.5")

        linux_command = MODULE._uv_install_command(
            Path("resolver/uv"),
            Path("scanner/python"),
            MODULE.SCANNERS[0],
            platform_name="posix",
        )
        self.assertNotIn("--no-binary", linux_command)

    def test_scanner_version_check_uses_distribution_metadata(self):
        command = MODULE._metadata_version_command(
            Path("scanner/python.exe"),
            MODULE.SCANNERS[0],
        )
        self.assertEqual(command[0:2], ("scanner/python.exe", "-c"))
        self.assertIn("importlib.metadata", command[2])
        self.assertEqual(command[-2:], ("cisco-ai-skill-scanner", "2.0.13"))
        self.assertNotIn("skill-scanner", command[0])

    def test_explicit_index_has_priority_over_environment(self):
        with patch.dict(MODULE.os.environ, {"PIP_INDEX_URL": "https://env.example/simple"}):
            value = MODULE._configured_index_url(
                Path("unused-python"), "https://explicit.example/simple"
            )
        self.assertEqual(value, "https://explicit.example/simple")

    def test_platform_executable_layout(self):
        environment = Path("scanner-env")
        path = MODULE._venv_executable(environment, "skillspector")
        if os.name == "nt":
            self.assertEqual(path, environment / "Scripts" / "skillspector.exe")
        else:
            self.assertEqual(path, environment / "bin" / "skillspector")

    def test_python_314_is_supported(self):
        self.assertIn((3, 14), MODULE.SUPPORTED_PYTHON)
        self.assertNotIn((3, 14), MODULE.SKILLSPECTOR_PYTHON)

    def test_current_compatible_python_is_selected_for_skillspector(self):
        with patch.object(
            MODULE,
            "_python_identity",
            return_value=(Path("scanner-python").resolve(), (3, 13)),
        ):
            selected = MODULE._skillspector_python()
        self.assertEqual(selected, Path("scanner-python").resolve())

    def test_python_314_without_compatible_runtime_has_clear_error(self):
        with (
            patch.dict(MODULE.os.environ, {}, clear=True),
            patch.object(MODULE, "_python_identity", return_value=None),
        ):
            with self.assertRaisesRegex(MODULE.InstallError, "Python 3.12 or 3.13"):
                MODULE._skillspector_python()

    def test_python_311_is_rejected_before_installation(self):
        with patch.object(MODULE.sys, "version_info", (3, 11, 0)):
            self.assertEqual(MODULE.main(["--root", "unused"]), 2)


if __name__ == "__main__":
    unittest.main()
