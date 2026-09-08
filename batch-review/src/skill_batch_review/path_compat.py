"""Repository-owned path compatibility for the standalone batch-review project.

The model-facing canonical security policy lives under
``.agents/skills/skill-security-review``.  Older generated local configurations
may still point at the removed top-level ``skills/`` directory or at a client
adapter.  This compatibility layer redirects only those known repository-owned
paths, only when the canonical policy exists, and never rewrites the user's TOML.
Explicit external/custom policy paths remain untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Callable


BATCH_REVIEW_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_AI_SKILL = (
    BATCH_REVIEW_ROOT / ".agents" / "skills" / "skill-security-review"
).resolve()
CANONICAL_AI_SCHEMA = (
    CANONICAL_AI_SKILL / "references" / "review-result.schema.json"
).resolve()


def _known_skill_paths() -> set[Path]:
    root = BATCH_REVIEW_ROOT
    return {
        CANONICAL_AI_SKILL,
        (root / "skills" / "skill-security-review").resolve(),
        (root / ".claude" / "skills" / "skill-security-review").resolve(),
        (root.parent / "skills" / "skill-security-review").resolve(),
        (root.parent / "batch-review" / "skills" / "skill-security-review").resolve(),
    }


def canonical_ai_resources(
    skill_path: Path,
    result_schema_path: Path,
) -> tuple[Path, Path]:
    """Resolve only known legacy repository paths to the canonical policy."""

    skill = skill_path.expanduser().resolve()
    schema = result_schema_path.expanduser().resolve()
    known = _known_skill_paths()
    if skill not in known:
        return skill, schema
    if not CANONICAL_AI_SKILL.is_dir() or not CANONICAL_AI_SCHEMA.is_file():
        return skill, schema

    known_schemas = {
        (candidate / "references" / "review-result.schema.json").resolve()
        for candidate in known
    }
    if schema in known_schemas:
        schema = CANONICAL_AI_SCHEMA
    return CANONICAL_AI_SKILL, schema


def install_config_path_compat(config_module: ModuleType) -> None:
    """Install the compatibility resolver into the already-loaded config module."""

    original = getattr(config_module, "_canonical_ai_resources", None)
    if original is None:
        return
    if getattr(original, "__module__", "") == __name__:
        return
    setattr(config_module, "_canonical_ai_resources", canonical_ai_resources)


__all__ = [
    "BATCH_REVIEW_ROOT",
    "CANONICAL_AI_SCHEMA",
    "CANONICAL_AI_SKILL",
    "canonical_ai_resources",
    "install_config_path_compat",
]
