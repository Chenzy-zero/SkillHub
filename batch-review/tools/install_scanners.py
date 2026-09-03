#!/usr/bin/env python3
"""Install the two approved scanners into isolated virtual environments.

The script is intentionally compatible with Windows and Linux and only calls
pip.  It never clones a Git repository.  The configured pip index must already
contain both pinned packages, including the internally republished
``skillspector`` wheel.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SUPPORTED_PYTHON = {(3, 12), (3, 13), (3, 14)}


@dataclass(frozen=True)
class ScannerPackage:
    name: str
    distribution: str
    version: str
    executable: str

    @property
    def requirement(self) -> str:
        return f"{self.distribution}=={self.version}"


SCANNERS = (
    ScannerPackage("cisco", "cisco-ai-skill-scanner", "2.0.13", "skill-scanner"),
    ScannerPackage("skillspector", "skillspector", "2.5.1", "skillspector"),
)


class InstallError(RuntimeError):
    pass


def _venv_executable(environment: Path, executable: str) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / f"{executable}.exe"
    return environment / "bin" / executable


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _run(argv: Sequence[str], *, env: dict[str, str]) -> None:
    completed = subprocess.run(tuple(argv), env=env, check=False)
    if completed.returncode != 0:
        raise InstallError(f"command failed with exit code {completed.returncode}: {argv[0]}")


def _pip_environment(index_url: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    if index_url:
        environment["PIP_INDEX_URL"] = index_url
    return environment


def install_scanner(
    package: ScannerPackage,
    *,
    root: Path,
    index_url: str | None,
) -> Path:
    environment = root / package.name
    environment.parent.mkdir(parents=True, exist_ok=True)
    if not _venv_python(environment).is_file():
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(environment)

    pip_environment = _pip_environment(index_url)
    python = _venv_python(environment)
    _run(
        (
            str(python),
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--upgrade",
            package.requirement,
        ),
        env=pip_environment,
    )

    executable = _venv_executable(environment, package.executable)
    if not executable.is_file():
        raise InstallError(f"scanner executable was not created: {executable}")
    _run((str(executable), "--version"), env=pip_environment)
    return executable.resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从公司 pip 源安装固定版本的 Cisco 与 SkillSpector。"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".scanner-tools"),
        help="隔离虚拟环境根目录，默认 .scanner-tools",
    )
    parser.add_argument(
        "--index-url",
        help="可选的公司 pip simple 地址；省略时使用 pip.conf/pip.ini 配置",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="安装完成后输出 JSON 路径信息",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    version = sys.version_info[:2]
    if version not in SUPPORTED_PYTHON:
        supported = ", ".join(".".join(map(str, item)) for item in sorted(SUPPORTED_PYTHON))
        print(
            f"不支持使用 Python {version[0]}.{version[1]} 安装扫描器；请使用 {supported}。",
            file=sys.stderr,
        )
        return 2

    root = args.root.expanduser().resolve()
    installed: dict[str, str] = {}
    try:
        for package in SCANNERS:
            installed[package.name] = str(
                install_scanner(package, root=root, index_url=args.index_url)
            )
    except (InstallError, OSError) as exc:
        print(f"扫描器安装失败: {exc}", file=sys.stderr)
        print(
            "请确认公司 pip 源已同步 cisco-ai-skill-scanner==2.0.13，"
            "并已人工构建和上传 skillspector==2.5.1 wheel 及其依赖。",
            file=sys.stderr,
        )
        return 1

    payload = {
        "python": f"{version[0]}.{version[1]}",
        "root": str(root),
        "scanners": installed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("扫描器安装完成：")
        for name, executable in installed.items():
            print(f"- {name}: {executable}")
        print("请把以上绝对路径填写到 review.toml 的 scanners.*.command[0]。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
