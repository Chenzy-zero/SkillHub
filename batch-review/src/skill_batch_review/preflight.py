"""Read-only deployment preflight checks for a real review batch."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ReviewConfig


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    message: str
    blocking: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "blocking": self.blocking}


_PLACEHOLDERS = {
    "configured",
    "pin-in-deployment",
    "policy-not-pinned",
    "model-not-configured",
    "set-company-intranet-model-id",
}


def _executable_available(value: str) -> bool:
    path = Path(value).expanduser()
    if path.is_absolute() or "/" in value:
        return path.is_file()
    return shutil.which(value) is not None


def review_preflight(config: ReviewConfig) -> tuple[PreflightIssue, ...]:
    """Check local prerequisites without contacting Gerrit or running tools."""

    issues: list[PreflightIssue] = []
    if not config.batch.inventory_csv.is_file():
        issues.append(PreflightIssue("INVENTORY_MISSING", f"CSV 不存在: {config.batch.inventory_csv}"))
    if config.gerrit.host.endswith(".example.com") or config.gerrit.host == "gerrit.example.com":
        issues.append(PreflightIssue("GERRIT_NOT_CONFIGURED", "Gerrit 地址仍是示例值"))
    if (
        config.gerrit.ssh_identity_file is not None
        and not config.gerrit.ssh_identity_file.is_file()
    ):
        issues.append(
            PreflightIssue(
                "SSH_IDENTITY_MISSING",
                f"SSH 私钥文件不存在: {config.gerrit.ssh_identity_file}",
            )
        )
    for name in ("cisco", "skillspector"):
        scanner = config.scanner(name)
        if not scanner.enabled:
            issues.append(PreflightIssue("SCANNER_DISABLED", f"必需扫描器未启用: {name}"))
        if scanner.version in _PLACEHOLDERS:
            issues.append(PreflightIssue("SCANNER_VERSION_NOT_PINNED", f"扫描器版本未固定: {name}"))
        if not _executable_available(scanner.executable):
            issues.append(
                PreflightIssue("SCANNER_NOT_FOUND", f"扫描器命令不可用: {scanner.executable}")
            )
    if not (config.ai.skill_path / "SKILL.md").is_file():
        issues.append(PreflightIssue("AI_SKILL_MISSING", f"AI 审查 Skill 不存在: {config.ai.skill_path}"))
    if not config.ai.result_schema_path.is_file():
        issues.append(
            PreflightIssue("AI_SCHEMA_MISSING", f"AI 结果 Schema 不存在: {config.ai.result_schema_path}")
        )
    if config.ai.policy_version in _PLACEHOLDERS:
        issues.append(PreflightIssue("POLICY_NOT_PINNED", "审查规则版本尚未固定"))
    if config.ai.reviewer_model in _PLACEHOLDERS:
        issues.append(PreflightIssue("AI_MODEL_NOT_CONFIGURED", "公司内网模型标识尚未配置"))
    return tuple(issues)


__all__ = ["PreflightIssue", "review_preflight"]
