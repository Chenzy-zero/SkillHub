"""Tests for the cross-platform scanner installer helpers."""

from __future__ import annotations

import importlib.util
import os
import sys
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

    def test_index_url_is_passed_by_environment_not_command_line(self):
        environment = MODULE._pip_environment("https://mirror.example/simple")
        self.assertEqual(environment["PIP_INDEX_URL"], "https://mirror.example/simple")
        self.assertEqual(environment["UV_INDEX_URL"], "https://mirror.example/simple")

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

    def test_python_311_is_rejected_before_installation(self):
        with patch.object(MODULE.sys, "version_info", (3, 11, 0)):
            self.assertEqual(MODULE.main(["--root", "unused"]), 2)


if __name__ == "__main__":
    unittest.main()
