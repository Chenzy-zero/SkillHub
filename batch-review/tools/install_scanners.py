#!/usr/bin/env python3
"""Install the two approved scanners into isolated virtual environments.

The script is intentionally compatible with Windows and Linux.  It bootstraps
a pinned uv wheel through pip, then uses uv's resolver for the scanner dependency
graphs.  It never clones a Git repository.  The configured package index must
already contain uv, Cisco, and all transitive dependencies.  The pinned
official ``skillspector`` wheel is shipped with this repository and verified
before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SUPPORTED_PYTHON = {(3, 12), (3, 13), (3, 14)}
SKILLSPECTOR_PYTHON = {(3, 12), (3, 13)}
UV_VERSION = "0.12.9"
WINDOWS_SOURCE_BUILD_PACKAGE = "win-unicode-console"
WINDOWS_SOURCE_BUILD_REQUIREMENT = "win-unicode-console==0.5"
BUNDLED_PACKAGES_DIR = Path(__file__).resolve().parents[1] / "packages"
WINDOWS_PYTHON_FILENAME = "python-3.13.15-amd64.exe"
WINDOWS_PYTHON_SHA256 = "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403"
SKILLSPECTOR_RUNTIME_INPUT = BUNDLED_PACKAGES_DIR / "skillspector-runtime.in"
SKILLSPECTOR_WINDOWS_LOCK = (
    BUNDLED_PACKAGES_DIR / "skillspector-runtime-windows-py313.txt"
)
SKILLSPECTOR_WINDOWS_LOCK_SHA256 = (
    "7661004da68119b3350158d809cbfc6d8060c10c0c3d3bf9f45a3c2db1cce367"
)
SKILLSPECTOR_EXCLUDED_RUNTIME_DEPENDENCY = "langgraph-cli"


@dataclass(frozen=True)
class ScannerPackage:
    name: str
    distribution: str
    version: str
    executable: str
    bundled_filename: str | None = None
    bundled_sha256: str | None = None

    @property
    def requirement(self) -> str:
        return f"{self.distribution}=={self.version}"


SCANNERS = (
    ScannerPackage("cisco", "cisco-ai-skill-scanner", "2.0.13", "skill-scanner"),
    ScannerPackage(
        "skillspector",
        "skillspector",
        "2.5.1",
        "skillspector",
        "skillspector-2.5.1-py3-none-any.whl",
        "56196f2f8689cc6e7f565181f06db5e489ba010ef0e5da19855d99043a5f6415",
    ),
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


def _python_identity(command: Sequence[str]) -> tuple[Path, tuple[int, int]] | None:
    """Return an interpreter path and minor version without importing project code."""

    try:
        completed = subprocess.run(
            (
                *command,
                "-c",
                "import sys; print(sys.executable); print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            ),
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return None
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) < 2:
        return None
    try:
        major, minor = (int(item) for item in lines[-1].strip().split(".", 1))
    except ValueError:
        return None
    return Path(lines[-2].strip()).resolve(), (major, minor)


def _bundled_windows_python_installer() -> Path:
    installer = BUNDLED_PACKAGES_DIR / WINDOWS_PYTHON_FILENAME
    if not installer.is_file():
        raise InstallError(f"bundled Python installer is missing: {installer}")
    actual_sha256 = _sha256(installer)
    if actual_sha256 != WINDOWS_PYTHON_SHA256:
        raise InstallError(
            f"bundled Python installer SHA-256 mismatch: {installer} "
            f"(expected {WINDOWS_PYTHON_SHA256}, got {actual_sha256})"
        )
    return installer.resolve()


def _windows_python_install_command(installer: Path, target: Path) -> tuple[str, ...]:
    """Build a quiet, current-user install that does not change PATH."""

    return (
        str(installer),
        "/quiet",
        "InstallAllUsers=0",
        f"TargetDir={target}",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "Include_pip=1",
        "Include_test=0",
        "Include_doc=0",
        "Include_tcltk=0",
        "Include_symbols=0",
        "Include_debug=0",
        "Shortcuts=0",
        "AssociateFiles=0",
        "PrependPath=0",
        "AppendPath=0",
    )


def _install_bundled_windows_python(runtime_root: Path) -> Path:
    target = (runtime_root / "_python313").resolve()
    python = target / "python.exe"
    existing = _python_identity((str(python),)) if python.is_file() else None
    if existing is not None and existing[1] == (3, 13):
        return existing[0]

    installer = _bundled_windows_python_installer()
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"正在安装项目专用 Python 3.13: {target}")
    _run(_windows_python_install_command(installer, target), env=os.environ.copy())
    installed = _python_identity((str(python),))
    if installed is None or installed[1] != (3, 13):
        raise InstallError(f"bundled Python 3.13 installation did not complete: {target}")
    return installed[0]


def _skillspector_python(runtime_root: Path | None = None) -> Path:
    """Find a wheel-compatible Python for SkillSpector and yara-python."""

    override = os.environ.get("SKILL_REVIEW_SCANNER_PYTHON", "").strip()
    if override:
        identity = _python_identity((override,))
        if identity is None or identity[1] not in SKILLSPECTOR_PYTHON:
            raise InstallError(
                "SKILL_REVIEW_SCANNER_PYTHON must point to Python 3.12 or 3.13"
            )
        return identity[0]

    current = _python_identity((sys.executable,))
    if current is not None and current[1] in SKILLSPECTOR_PYTHON:
        return current[0]

    candidates: tuple[tuple[str, ...], ...]
    if os.name == "nt":
        windows_paths: list[tuple[str, ...]] = []
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        program_files = os.environ.get("ProgramFiles", "").strip()
        if local_app_data:
            windows_paths.extend(
                (
                    (
                        str(
                            Path(local_app_data)
                            / "Programs"
                            / "Python"
                            / "Python313"
                            / "python.exe"
                        ),
                    ),
                    (
                        str(
                            Path(local_app_data)
                            / "Python"
                            / "pythoncore-3.13-64"
                            / "python.exe"
                        ),
                    ),
                )
            )
        if program_files:
            windows_paths.append(
                (str(Path(program_files) / "Python313" / "python.exe"),)
            )
        candidates = (
            ("py", "-3.13"),
            ("py", "-V:3.13"),
            ("py", "-3.12"),
            ("python3.13",),
            ("python3.12",),
            *windows_paths,
        )
    else:
        candidates = (("python3.13",), ("python3.12",))
    for command in candidates:
        identity = _python_identity(command)
        if identity is not None and identity[1] in SKILLSPECTOR_PYTHON:
            return identity[0]

    if os.name == "nt" and runtime_root is not None:
        return _install_bundled_windows_python(runtime_root)

    raise InstallError(
        "SkillSpector requires Python 3.12 or 3.13 because yara-python has no "
        "Python 3.14 wheel. On Windows, run review.cmd so the bundled project "
        "runtime can be installed; on Linux, install Python 3.12 or 3.13."
    )


def _ensure_scanner_environment(environment: Path, base_python: Path) -> Path:
    """Create or safely refresh one disposable scanner virtual environment."""

    expected = _python_identity((str(base_python),))
    if expected is None:
        raise InstallError(f"scanner Python is not executable: {base_python}")
    environment_python = _venv_python(environment)
    actual = (
        _python_identity((str(environment_python),))
        if environment_python.is_file()
        else None
    )
    if actual is None or actual[1] != expected[1]:
        environment.parent.mkdir(parents=True, exist_ok=True)
        command = [str(base_python), "-m", "venv"]
        if environment.exists():
            command.append("--clear")
        command.append(str(environment))
        _run(command, env=os.environ.copy())
    if not environment_python.is_file():
        raise InstallError(f"scanner environment was not created: {environment}")
    return environment_python


def _pip_environment(index_url: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    if index_url:
        environment["PIP_INDEX_URL"] = index_url
        environment["UV_INDEX_URL"] = index_url
    return environment


def _configured_index_url(python: Path, explicit: str | None) -> str | None:
    """Reuse an explicit URL, environment value, or pip.ini/pip.conf default."""

    if explicit:
        return explicit
    environment_value = os.environ.get("PIP_INDEX_URL", "").strip()
    if environment_value:
        return environment_value
    for key in ("global.index-url", "install.index-url"):
        completed = subprocess.run(
            (str(python), "-m", "pip", "config", "get", key),
            check=False,
            text=True,
            capture_output=True,
        )
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            return value
    return None


def _ensure_uv(*, root: Path, index_url: str | None) -> tuple[Path, dict[str, str]]:
    installer_environment = root / "_installer"
    installer_python = _venv_python(installer_environment)
    if not installer_python.is_file():
        installer_environment.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(installer_environment)

    resolved_index = _configured_index_url(installer_python, index_url)
    package_environment = _pip_environment(resolved_index)
    _run(
        (
            str(installer_python),
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--no-deps",
            "--upgrade",
            f"uv=={UV_VERSION}",
        ),
        env=package_environment,
    )
    executable = _venv_executable(installer_environment, "uv")
    if not executable.is_file():
        raise InstallError(f"uv executable was not created: {executable}")
    _run((str(executable), "--version"), env=package_environment)
    return executable.resolve(), package_environment


def _uv_install_command(
    uv: Path,
    python: Path,
    package: ScannerPackage,
    *,
    platform_name: str | None = None,
    requirement: str | None = None,
    no_deps: bool = False,
) -> tuple[str, ...]:
    command = [
        str(uv),
        "pip",
        "install",
        "--python",
        str(python),
        "--only-binary",
        ":all:",
        "--upgrade",
    ]
    if no_deps:
        command.append("--no-deps")
    command.append(requirement or package.requirement)
    platform_value = os.name if platform_name is None else platform_name
    if platform_value == "nt" and package.name == "cisco":
        # oletools -> pcodedmp requires this legacy Windows-only package, for
        # which PyPI publishes no wheel.  Keep the exception exact and local;
        # every other direct and transitive package remains wheel-only.
        command.extend(
            (
                "--no-binary",
                WINDOWS_SOURCE_BUILD_PACKAGE,
                WINDOWS_SOURCE_BUILD_REQUIREMENT,
            )
        )
    return tuple(command)


def _uv_requirements_command(
    uv: Path,
    python: Path,
    requirements: Path,
) -> tuple[str, ...]:
    """Install an explicitly curated runtime graph without optional dev servers."""

    return (
        str(uv),
        "pip",
        "sync",
        "--python",
        str(python),
        "--only-binary",
        ":all:",
        str(requirements),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_requirement(package: ScannerPackage) -> str:
    """Prefer and verify an approved wheel shipped with this repository."""

    if not package.bundled_filename:
        return package.requirement
    wheel = BUNDLED_PACKAGES_DIR / package.bundled_filename
    if not wheel.is_file():
        raise InstallError(f"bundled scanner wheel is missing: {wheel}")
    actual_sha256 = _sha256(wheel)
    if actual_sha256 != package.bundled_sha256:
        raise InstallError(
            f"bundled scanner wheel SHA-256 mismatch: {wheel} "
            f"(expected {package.bundled_sha256}, got {actual_sha256})"
        )
    return str(wheel.resolve())


def _requirement_name(value: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", value)
    if not match:
        raise InstallError(f"invalid requirement in SkillSpector metadata: {value}")
    return match.group(1).lower().replace("_", "-")


def _skillspector_runtime_requirements(
    package: ScannerPackage,
    python: Path,
    *,
    platform_name: str | None = None,
) -> Path:
    """Return and validate the real CLI runtime dependencies for SkillSpector.

    NVIDIA 2.5.1 declares ``langgraph-cli[inmem]`` as a mandatory dependency,
    although no module in the wheel imports it.  It is a LangGraph Studio
    development server and pulls source-only packages on Windows.  Keep the
    official wheel unchanged, validate that every other direct runtime
    dependency is covered, and install the wheel itself with ``--no-deps``.
    """

    wheel = Path(_install_requirement(package))
    declared: set[str] = set()
    imports_dev_server = False
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            raise InstallError(f"SkillSpector wheel has no METADATA: {wheel}")
        for line in archive.read(metadata_name).decode("utf-8").splitlines():
            if not line.startswith("Requires-Dist:"):
                continue
            requirement = line.partition(":")[2].strip()
            if "extra ==" not in requirement:
                declared.add(_requirement_name(requirement))
        for name in archive.namelist():
            if not name.startswith("skillspector/") or not name.endswith(".py"):
                continue
            source = archive.read(name).decode("utf-8", errors="replace")
            if "langgraph_cli" in source:
                imports_dev_server = True
                break

    input_names = {
        _requirement_name(line)
        for line in SKILLSPECTOR_RUNTIME_INPUT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    expected = declared - {SKILLSPECTOR_EXCLUDED_RUNTIME_DEPENDENCY}
    if imports_dev_server or input_names != expected:
        missing = sorted(expected - input_names)
        extra = sorted(input_names - expected)
        raise InstallError(
            "SkillSpector runtime dependency manifest no longer matches the verified wheel "
            f"(missing={missing}, extra={extra}, imports_langgraph_cli={imports_dev_server})"
        )

    identity = _python_identity((str(python),))
    platform_value = os.name if platform_name is None else platform_name
    if platform_value == "nt" and identity is not None and identity[1] == (3, 13):
        if _sha256(SKILLSPECTOR_WINDOWS_LOCK) != SKILLSPECTOR_WINDOWS_LOCK_SHA256:
            raise InstallError(
                f"SkillSpector Windows dependency lock SHA-256 mismatch: "
                f"{SKILLSPECTOR_WINDOWS_LOCK}"
            )
        return SKILLSPECTOR_WINDOWS_LOCK.resolve()
    return SKILLSPECTOR_RUNTIME_INPUT.resolve()


def _metadata_version_command(
    python: Path,
    package: ScannerPackage,
) -> tuple[str, ...]:
    """Build an offline version check that does not import scanner code.

    Some scanner console entry points import optional analyzers before argument
    parsing.  Starting the executable merely to ask for ``--version`` can
    therefore initialize third-party packages and unexpectedly access the
    network.  Distribution metadata is sufficient to verify the pinned wheel
    that uv installed and has no scanner import side effects.
    """

    program = (
        "import importlib.metadata as metadata, sys; "
        "actual = metadata.version(sys.argv[1]); "
        "expected = sys.argv[2]; "
        "print(f'{sys.argv[1]} {actual}'); "
        "raise SystemExit(0 if actual == expected else 1)"
    )
    return (
        str(python),
        "-c",
        program,
        package.distribution,
        package.version,
    )


def install_scanner(
    package: ScannerPackage,
    *,
    root: Path,
    uv: Path,
    package_environment: dict[str, str],
    base_python: Path,
) -> Path:
    environment = root / package.name
    python = _ensure_scanner_environment(environment, base_python)
    requirement = _install_requirement(package)
    if package.name == "skillspector":
        runtime_requirements = _skillspector_runtime_requirements(package, python)
        _run(
            _uv_requirements_command(uv, python, runtime_requirements),
            env=package_environment,
        )
    _run(
        _uv_install_command(
            uv,
            python,
            package,
            requirement=requirement,
            no_deps=package.name == "skillspector",
        ),
        env=package_environment,
    )

    executable = _venv_executable(environment, package.executable)
    if not executable.is_file():
        raise InstallError(f"scanner executable was not created: {executable}")
    _run(_metadata_version_command(python, package), env=package_environment)
    return executable.resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从公司 Python 包源安装固定版本的 uv、Cisco 与 SkillSpector。"
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
        skillspector_python = _skillspector_python(root)
        print(f"SkillSpector Python: {skillspector_python}")
        uv, package_environment = _ensure_uv(root=root, index_url=args.index_url)
        for package in SCANNERS:
            base_python = (
                skillspector_python
                if package.name == "skillspector"
                else Path(sys.executable).resolve()
            )
            installed[package.name] = str(
                install_scanner(
                    package,
                    root=root,
                    uv=uv,
                    package_environment=package_environment,
                    base_python=base_python,
                )
            )
    except (InstallError, OSError) as exc:
        print(f"扫描器安装失败: {exc}", file=sys.stderr)
        print(
            f"请确认公司 pip 源已同步 uv=={UV_VERSION}、"
            "cisco-ai-skill-scanner==2.0.13，以及 SkillSpector Windows 锁定清单中的 wheel；"
            "官方 SkillSpector wheel 和运行依赖清单应存在于 batch-review/packages。",
            file=sys.stderr,
        )
        return 1

    payload = {
        "python": f"{version[0]}.{version[1]}",
        "resolver": f"uv=={UV_VERSION}",
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
