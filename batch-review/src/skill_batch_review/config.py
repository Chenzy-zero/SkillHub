"""Typed TOML configuration for the batch review CLI.

The configuration is intentionally declarative. Command templates are parsed
into argument vectors and rendered without a shell. Loading this module never
contacts Gerrit and never starts a scanner; only an explicit execution command
crosses those boundaries.
"""

from __future__ import annotations

import hashlib
import shlex
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only for local Python 3.10/3.9
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - dependency error path
        tomllib = None  # type: ignore[assignment]


class ConfigError(ValueError):
    """Raised when a review configuration is missing or unsafe."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _section(root: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = root.get(name, {})
    return _mapping(value, name)


def _text(value: Any, name: str, *, default: Optional[str] = None) -> str:
    if value is None:
        if default is not None:
            return default
        raise ConfigError(f"{name} must not be empty")
    result = str(value).strip()
    if not result:
        if default is not None:
            return default
        raise ConfigError(f"{name} must not be empty")
    if "\x00" in result or "\r" in result or "\n" in result:
        raise ConfigError(f"{name} must not contain control characters")
    return result


def _bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _integer(value: Any, name: str, *, default: int, minimum: int = 0) -> int:
    if value is None:
        result = default
    elif isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    else:
        result = value
    if result < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return result


def _path(value: Any, name: str, *, base_dir: Path, default: str) -> Path:
    raw = _text(value, name, default=default)
    result = Path(raw).expanduser()
    if not result.is_absolute():
        result = base_dir / result
    return result.resolve()


def _string_tuple(value: Any, name: str, *, allow_string: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) and allow_string:
        try:
            values = shlex.split(value)
        except ValueError as exc:
            raise ConfigError(f"{name} is not a valid command: {exc}") from exc
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = list(value)
    else:
        raise ConfigError(f"{name} must be a non-empty TOML string array")
    if not values or any(not isinstance(item, str) or not item for item in values):
        raise ConfigError(f"{name} must contain non-empty strings")
    return tuple(values)


def _format_fields(template: str) -> set[str]:
    fields: set[str] = set()
    try:
        parsed = string.Formatter().parse(template)
        for _, field_name, _, _ in parsed:
            if field_name:
                # Format fields are intentionally simple names; attribute and
                # item access would make config review and safe rendering less
                # obvious.
                if not field_name.isidentifier():
                    raise ConfigError(
                        f"template contains unsupported placeholder {{{field_name}}}"
                    )
                fields.add(field_name)
    except ValueError as exc:
        raise ConfigError(f"invalid template syntax: {exc}") from exc
    return fields


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    evidence_root: Path
    candidate_root: Path
    manifest_root: Path
    git_download_root: Path
    skills_root: Path
    results_root: Path
    clean_after_repository: bool = True
    keep_failed_workspace: bool = False


@dataclass(frozen=True)
class BatchConfig:
    inventory_csv: Path
    batch_id_prefix: str = "skill-review"
    included_statuses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = tuple(
            _text(item, "batch.included_statuses").upper()
            for item in self.included_statuses
        )
        if not values:
            raise ConfigError(
                "batch.included_statuses must explicitly list the lifecycle statuses to review"
            )
        if len(set(values)) != len(values):
            raise ConfigError("batch.included_statuses must not contain duplicates")
        object.__setattr__(self, "included_statuses", values)


@dataclass(frozen=True)
class GerritConfig:
    ssh_url_template: str
    user: str = "git"
    host: str = "gerrit.example.com"
    port: int = 29418
    allowed_repositories: tuple[str, ...] = ()
    ssh_identity_file: Path | None = None

    _allowed_fields = frozenset({"repo_name", "branch", "user", "host", "port"})

    def __post_init__(self) -> None:
        template = _text(self.ssh_url_template, "gerrit.ssh_url_template")
        fields = _format_fields(template)
        if "repo_name" not in fields:
            raise ConfigError("gerrit.ssh_url_template must contain {repo_name}")
        unknown = fields - self._allowed_fields
        if unknown:
            raise ConfigError(
                "gerrit.ssh_url_template has unsupported placeholders: "
                + ", ".join(sorted(unknown))
            )
        if any(char in template for char in "\r\n\x00"):
            raise ConfigError("gerrit.ssh_url_template contains control characters")
        if not 1 <= self.port <= 65535:
            raise ConfigError("gerrit.port must be between 1 and 65535")
        object.__setattr__(self, "ssh_url_template", template)
        object.__setattr__(self, "user", _text(self.user, "gerrit.user"))
        object.__setattr__(self, "host", _text(self.host, "gerrit.host"))
        repositories = tuple(_text(item, "gerrit.allowed_repositories") for item in self.allowed_repositories)
        object.__setattr__(self, "allowed_repositories", repositories)
        if self.ssh_identity_file is not None:
            identity = Path(self.ssh_identity_file).expanduser().resolve()
            object.__setattr__(self, "ssh_identity_file", identity)

    def repository_url(self, repo_name: str, *, branch: str = "") -> str:
        repo = _text(repo_name, "repo_name")
        if (
            repo.startswith("/")
            or repo.endswith("/")
            or "\\" in repo
            or "//" in repo
            or any(part in {"", ".", ".."} for part in repo.split("/"))
            or "://" in repo
        ):
            raise ConfigError(f"repo_name must be a normalized Gerrit project path: {repo}")
        if self.allowed_repositories and repo not in self.allowed_repositories:
            raise ConfigError(f"repository is not in gerrit.allowed_repositories: {repo}")
        values = {
            "repo_name": repo,
            "branch": branch,
            "user": self.user,
            "host": self.host,
            "port": self.port,
        }
        try:
            return self.ssh_url_template.format_map(values)
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"cannot render Gerrit URL template: {exc}") from exc


@dataclass(frozen=True)
class StatusMapping:
    """Mapping from legacy CSV status values to internal status names."""

    aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, str] = {}
        for source, target in self.aliases.items():
            source_text = _text(source, "status_mapping source")
            target_text = _text(target, f"status_mapping[{source_text}]").upper()
            normalized[source_text] = target_text
        object.__setattr__(self, "aliases", normalized)

    def normalize(self, value: str) -> str:
        source = _text(value, "status")
        try:
            return self.aliases[source]
        except KeyError as exc:
            raise ConfigError(f"unknown inventory status: {source}") from exc


@dataclass(frozen=True)
class QualityConfig:
    candidate_threshold: int = 70
    max_score: int = 100

    def __post_init__(self) -> None:
        if not 0 <= self.candidate_threshold <= 100:
            raise ConfigError("quality.candidate_threshold must be between 0 and 100")
        if not 0 < self.max_score <= 100:
            raise ConfigError("quality.max_score must be between 1 and 100")
        if self.candidate_threshold > self.max_score:
            raise ConfigError("quality.candidate_threshold must not exceed max_score")


@dataclass(frozen=True)
class AIConfig:
    skill_path: Path
    result_schema_path: Path
    policy_version: str
    reviewer_model: str = "ai-agent-session"


def _canonical_ai_resources(
    skill_path: Path, result_schema_path: Path
) -> tuple[Path, Path]:
    """Redirect repository-owned legacy client adapters to the canonical policy.

    Existing local configurations may still point at the former Claude-only
    directory.  Keep those configurations runnable without rewriting operator
    files, while ensuring both Claude Code and Codex CLI hash and validate the
    same policy and result Schema.
    """

    parts = skill_path.parts
    if len(parts) < 3 or skill_path.name != "skill-security-review":
        return skill_path, result_schema_path
    client_root = skill_path.parent.parent
    if client_root.name not in {".claude", ".agents"}:
        return skill_path, result_schema_path
    repository_root = client_root.parent
    canonical = repository_root / "batch-review" / "skills" / "skill-security-review"
    if not canonical.is_dir():
        return skill_path, result_schema_path
    legacy_schema = skill_path / "references" / "review-result.schema.json"
    if result_schema_path == legacy_schema:
        result_schema_path = canonical / "references" / "review-result.schema.json"
    return canonical.resolve(), result_schema_path.resolve()


def derive_ai_policy_version(skill_path: Path) -> str:
    """Hash maintained review rules without timestamps or evaluation files."""

    root = skill_path.expanduser().resolve()
    candidates: list[Path] = []
    anchor = root / "SKILL.md"
    if anchor.is_file() and not anchor.is_symlink():
        candidates.append(anchor)
    references = root / "references"
    if references.is_dir() and not references.is_symlink():
        candidates.extend(
            path
            for path in references.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    if not candidates:
        return "skill-policy-unavailable"
    digest = hashlib.sha256()
    for path in sorted(candidates, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"skill-policy-sha256:{digest.hexdigest()}"


@dataclass(frozen=True)
class ScannerConfig:
    """A scanner command template represented as a shell-free argv tuple."""

    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 600
    enabled: bool = True
    version: str = "configured"

    allowed_placeholders = frozenset(
        {
            "skill_root",
            "output_file",
            "skill_digest",
            "source_revision",
            "workspace",
            "repo_name",
            "skill_name",
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "scanner.name"))
        if not self.command:
            raise ConfigError(f"scanner {self.name} command must not be empty")
        for token in self.command:
            if not isinstance(token, str) or not token:
                raise ConfigError(f"scanner {self.name} command contains an empty token")
        fields = set().union(*(_format_fields(token) for token in self.command))
        unknown = fields - self.allowed_placeholders
        if unknown:
            raise ConfigError(
                f"scanner {self.name} has unsupported placeholders: "
                + ", ".join(sorted(unknown))
            )
        if self.timeout_seconds <= 0:
            raise ConfigError(f"scanner {self.name}.timeout_seconds must be positive")
        object.__setattr__(self, "version", _text(self.version, f"scanner {self.name}.version"))

    @classmethod
    def from_mapping(cls, name: str, values: Mapping[str, Any]) -> "ScannerConfig":
        return cls(
            name=name,
            command=_string_tuple(values.get("command"), f"scanners.{name}.command", allow_string=True),
            timeout_seconds=_integer(
                values.get("timeout_seconds"),
                f"scanners.{name}.timeout_seconds",
                default=600,
                minimum=1,
            ),
            enabled=_bool(values.get("enabled"), f"scanners.{name}.enabled", default=True),
            version=_text(values.get("version"), f"scanners.{name}.version", default="configured"),
        )

    def render(self, **context: Any) -> tuple[str, ...]:
        missing = _format_fields(" ".join(self.command)) - set(context)
        if missing:
            raise ConfigError(
                f"scanner {self.name} render context is missing: " + ", ".join(sorted(missing))
            )
        try:
            return tuple(token.format_map(context) for token in self.command)
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"cannot render scanner {self.name} command: {exc}") from exc

    @property
    def executable(self) -> str:
        return self.command[0]


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    backoff_seconds: int = 5
    max_backoff_seconds: int = 60

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("retry.max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ConfigError("retry.backoff_seconds must be >= 0")
        if self.max_backoff_seconds < self.backoff_seconds:
            raise ConfigError("retry.max_backoff_seconds must be >= backoff_seconds")


@dataclass(frozen=True)
class ConcurrencyConfig:
    repositories: int = 1
    skills_per_repository: int = 1
    ai_reviews: int = 1

    def __post_init__(self) -> None:
        for name in ("repositories", "skills_per_repository", "ai_reviews"):
            value = getattr(self, name)
            if value < 1:
                raise ConfigError(f"concurrency.{name} must be >= 1")


@dataclass(frozen=True)
class ReviewConfig:
    """Complete validated configuration for one batch-review workspace."""

    path: Path
    batch: BatchConfig
    workspace: WorkspaceConfig
    gerrit: GerritConfig
    status_mapping: StatusMapping
    quality: QualityConfig
    ai: AIConfig
    scanners: Mapping[str, ScannerConfig]
    retry: RetryConfig
    concurrency: ConcurrencyConfig

    def scanner(self, name: str) -> ScannerConfig:
        try:
            return self.scanners[name]
        except KeyError as exc:
            raise ConfigError(f"scanner is not configured: {name}") from exc

    def summary(self) -> dict[str, Any]:
        return {
            "config": str(self.path),
            "inventory_csv": str(self.batch.inventory_csv),
            "included_statuses": list(self.batch.included_statuses),
            "workspace_root": str(self.workspace.root),
            "evidence_root": str(self.workspace.evidence_root),
            "candidate_root": str(self.workspace.candidate_root),
            "manifest_root": str(self.workspace.manifest_root),
            "git_download_root": str(self.workspace.git_download_root),
            "skills_root": str(self.workspace.skills_root),
            "results_root": str(self.workspace.results_root),
            "gerrit_url_template": self.gerrit.ssh_url_template,
            "scanners": {
                name: {"enabled": item.enabled, "version": item.version}
                for name, item in sorted(self.scanners.items())
            },
            "quality_candidate_threshold": self.quality.candidate_threshold,
            "ai_policy_version": self.ai.policy_version,
            "ai_reviewer_model": self.ai.reviewer_model,
            "retry_max_attempts": self.retry.max_attempts,
            "concurrency": {
                "repositories": self.concurrency.repositories,
                "skills_per_repository": self.concurrency.skills_per_repository,
                "ai_reviews": self.concurrency.ai_reviews,
            },
        }


def _load_toml(path: Path) -> Mapping[str, Any]:
    if tomllib is None:  # pragma: no cover
        raise ConfigError("Python 3.11+ or the tomli package is required to read TOML")
    if not path.is_file():
        raise ConfigError(f"configuration file does not exist: {path}")
    try:
        with path.open("rb") as handle:
            return _mapping(tomllib.load(handle), "root")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    except Exception as exc:
        # tomllib.TOMLDecodeError is intentionally not imported for the tomli
        # fallback; both are user-facing parse errors.
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def load_config(path: "str | Path") -> ReviewConfig:
    """Load and validate a TOML review configuration.

    Relative paths are resolved relative to the configuration file, making a
    batch reproducible when launched from a different current directory.
    """

    config_path = Path(path).expanduser().resolve()
    root = _load_toml(config_path)
    allowed_top_level = {
        "batch",
        "workspace",
        "gerrit",
        "status_mapping",
        "quality",
        "ai",
        "scanners",
        "retry",
        "concurrency",
    }
    unknown = set(root) - allowed_top_level
    if unknown:
        raise ConfigError("unknown top-level configuration sections: " + ", ".join(sorted(unknown)))
    base_dir = config_path.parent

    batch_data = _section(root, "batch")
    included_statuses_value = batch_data.get("included_statuses")
    if not isinstance(included_statuses_value, Sequence) or isinstance(
        included_statuses_value, (str, bytes, bytearray)
    ):
        raise ConfigError("batch.included_statuses must be a non-empty string array")
    batch = BatchConfig(
        inventory_csv=_path(
            batch_data.get("inventory_csv"),
            "batch.inventory_csv",
            base_dir=base_dir,
            default="inventory.csv",
        ),
        batch_id_prefix=_text(
            batch_data.get("batch_id_prefix"), "batch.batch_id_prefix", default="skill-review"
        ),
        included_statuses=tuple(included_statuses_value),
    )

    workspace_data = _section(root, "workspace")
    workspace = WorkspaceConfig(
        root=_path(workspace_data.get("root"), "workspace.root", base_dir=base_dir, default=".batch-review/work"),
        evidence_root=_path(
            workspace_data.get("evidence_root"),
            "workspace.evidence_root",
            base_dir=base_dir,
            default=".batch-review/evidence",
        ),
        candidate_root=_path(
            workspace_data.get("candidate_root"),
            "workspace.candidate_root",
            base_dir=base_dir,
            default=".batch-review/candidates",
        ),
        manifest_root=_path(
            workspace_data.get("manifest_root"),
            "workspace.manifest_root",
            base_dir=base_dir,
            default=".batch-review/manifests",
        ),
        git_download_root=_path(
            workspace_data.get("git_download_root"),
            "workspace.git_download_root",
            base_dir=base_dir,
            default=".batch-review/git_download",
        ),
        skills_root=_path(
            workspace_data.get("skills_root"),
            "workspace.skills_root",
            base_dir=base_dir,
            default=".batch-review/skills",
        ),
        results_root=_path(
            workspace_data.get("results_root"),
            "workspace.results_root",
            base_dir=base_dir,
            default=".batch-review/results",
        ),
        clean_after_repository=_bool(
            workspace_data.get("clean_after_repository"),
            "workspace.clean_after_repository",
            default=True,
        ),
        keep_failed_workspace=_bool(
            workspace_data.get("keep_failed_workspace"),
            "workspace.keep_failed_workspace",
            default=False,
        ),
    )
    protected_roots = {
        "evidence_root": workspace.evidence_root,
        "candidate_root": workspace.candidate_root,
        "manifest_root": workspace.manifest_root,
        "skills_root": workspace.skills_root,
        "results_root": workspace.results_root,
    }
    for name, protected in protected_roots.items():
        if protected == workspace.root or protected.is_relative_to(workspace.root):
            raise ConfigError(
                f"workspace.{name} must be outside workspace.root so cleanup cannot remove it"
            )
    protected_items = list(protected_roots.items())
    for index, (left_name, left_path) in enumerate(protected_items):
        for right_name, right_path in protected_items[index + 1 :]:
            if (
                left_path == right_path
                or left_path.is_relative_to(right_path)
                or right_path.is_relative_to(left_path)
            ):
                raise ConfigError(
                    f"workspace.{left_name} and workspace.{right_name} must be separate directories"
                )

    for name, protected in protected_roots.items():
        if (
            protected == workspace.git_download_root
            or protected.is_relative_to(workspace.git_download_root)
            or workspace.git_download_root.is_relative_to(protected)
        ):
            raise ConfigError(
                f"workspace.{name} and workspace.git_download_root must be separate directories"
            )

    gerrit_data = _section(root, "gerrit")
    allowed_repositories_value = gerrit_data.get("allowed_repositories", [])
    if not isinstance(allowed_repositories_value, Sequence) or isinstance(
        allowed_repositories_value, (str, bytes, bytearray)
    ):
        raise ConfigError("gerrit.allowed_repositories must be a string array")
    gerrit = GerritConfig(
        ssh_url_template=_text(
            gerrit_data.get("ssh_url_template"),
            "gerrit.ssh_url_template",
            default="ssh://{user}@{host}:{port}/{repo_name}.git",
        ),
        user=_text(gerrit_data.get("user"), "gerrit.user", default="git"),
        host=_text(gerrit_data.get("host"), "gerrit.host", default="gerrit.example.com"),
        port=_integer(gerrit_data.get("port"), "gerrit.port", default=29418, minimum=1),
        allowed_repositories=tuple(allowed_repositories_value),
        ssh_identity_file=(
            _path(
                gerrit_data.get("ssh_identity_file"),
                "gerrit.ssh_identity_file",
                base_dir=base_dir,
                default="unused",
            )
            if gerrit_data.get("ssh_identity_file") is not None
            else None
        ),
    )

    status_data = _section(root, "status_mapping")
    aliases_value: Any = status_data.get("aliases", status_data)
    aliases = _mapping(aliases_value, "status_mapping.aliases")
    if not aliases:
        raise ConfigError("status_mapping must contain at least one status mapping")
    status_mapping = StatusMapping(aliases=aliases)  # type: ignore[arg-type]
    mapped_statuses = {value.upper() for value in status_mapping.aliases.values()}
    unknown_included = set(batch.included_statuses) - mapped_statuses
    if unknown_included:
        raise ConfigError(
            "batch.included_statuses are not produced by status_mapping: "
            + ", ".join(sorted(unknown_included))
        )

    quality_data = _section(root, "quality")
    quality = QualityConfig(
        candidate_threshold=_integer(
            quality_data.get("candidate_threshold"),
            "quality.candidate_threshold",
            default=70,
            minimum=0,
        ),
        max_score=_integer(quality_data.get("max_score"), "quality.max_score", default=100, minimum=1),
    )

    ai_data = _section(root, "ai")
    ai_skill_path = _path(
        ai_data.get("skill_path"),
        "ai.skill_path",
        base_dir=base_dir,
        default="../skills/skill-security-review",
    )
    ai_result_schema_path = _path(
        ai_data.get("result_schema_path"),
        "ai.result_schema_path",
        base_dir=base_dir,
        default="../skills/skill-security-review/references/review-result.schema.json",
    )
    ai_skill_path, ai_result_schema_path = _canonical_ai_resources(
        ai_skill_path, ai_result_schema_path
    )
    configured_policy = ai_data.get("policy_version")
    policy_version = (
        _text(configured_policy, "ai.policy_version")
        if configured_policy not in (None, "", "auto")
        else derive_ai_policy_version(ai_skill_path)
    )
    ai = AIConfig(
        skill_path=ai_skill_path,
        result_schema_path=ai_result_schema_path,
        policy_version=policy_version,
        reviewer_model=_text(
            ai_data.get("reviewer_model"), "ai.reviewer_model", default="ai-agent-session"
        ),
    )

    scanners_data = _section(root, "scanners")
    scanners: dict[str, ScannerConfig] = {}
    for name, raw_values in scanners_data.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("scanner names must be non-empty strings")
        scanners[name] = ScannerConfig.from_mapping(name, _mapping(raw_values, f"scanners.{name}"))
    if not scanners:
        raise ConfigError("scanners must contain at least one scanner")
    required_scanners = {"cisco", "skillspector"}
    missing_scanners = required_scanners - set(scanners)
    if missing_scanners:
        raise ConfigError(
            "scanners must configure both cisco and skillspector: "
            + ", ".join(sorted(missing_scanners))
        )
    approved_suffixes = {
        "cisco": (
            "scan", "{skill_root}", "--format", "json", "--compact", "--output", "{output_file}"
        ),
        "skillspector": (
            "scan", "{skill_root}", "--no-llm", "--format", "json", "--output", "{output_file}"
        ),
    }
    for name, expected_suffix in approved_suffixes.items():
        if scanners[name].command[1:] != expected_suffix:
            raise ConfigError(
                f"scanners.{name}.command must use the approved local static arguments"
            )

    retry_data = _section(root, "retry")
    retry = RetryConfig(
        max_attempts=_integer(retry_data.get("max_attempts"), "retry.max_attempts", default=3, minimum=1),
        backoff_seconds=_integer(
            retry_data.get("backoff_seconds"), "retry.backoff_seconds", default=5, minimum=0
        ),
        max_backoff_seconds=_integer(
            retry_data.get("max_backoff_seconds"),
            "retry.max_backoff_seconds",
            default=60,
            minimum=0,
        ),
    )

    concurrency_data = _section(root, "concurrency")
    concurrency = ConcurrencyConfig(
        repositories=_integer(
            concurrency_data.get("repositories"), "concurrency.repositories", default=1, minimum=1
        ),
        skills_per_repository=_integer(
            concurrency_data.get("skills_per_repository"),
            "concurrency.skills_per_repository",
            default=1,
            minimum=1,
        ),
        ai_reviews=_integer(
            concurrency_data.get("ai_reviews"), "concurrency.ai_reviews", default=1, minimum=1
        ),
    )

    return ReviewConfig(
        path=config_path,
        batch=batch,
        workspace=workspace,
        gerrit=gerrit,
        status_mapping=status_mapping,
        quality=quality,
        ai=ai,
        scanners=scanners,
        retry=retry,
        concurrency=concurrency,
    )


__all__ = [
    "AIConfig",
    "BatchConfig",
    "ConcurrencyConfig",
    "ConfigError",
    "GerritConfig",
    "QualityConfig",
    "ReviewConfig",
    "RetryConfig",
    "ScannerConfig",
    "StatusMapping",
    "WorkspaceConfig",
    "derive_ai_policy_version",
    "load_config",
]
