"""Safe building blocks for repository-at-a-time batch Skill review.

Network, scanner, AI-import, candidate-export and cleanup boundaries remain
separate and operator-visible.  No module executes target Skill content or
automatically commits, pushes, or publishes candidates.
"""

# Install the persistent evidence boundary before orchestration modules bind
# ``EvidenceStore`` / ``write_html_report`` locally.  The public import surface
# remains unchanged for existing callers.
from . import artifacts as _artifact_module
from .evidence_index import IndexedEvidenceStore

_artifact_module.EvidenceStore = IndexedEvidenceStore
_artifact_module.RestrictedEvidenceStore = IndexedEvidenceStore

from . import html_reporting as _html_reporting
from .indexed_html_reporting import write_html_report as _indexed_write_html_report

_html_reporting.write_html_report = _indexed_write_html_report

from .config import (
    BatchConfig,
    ConcurrencyConfig,
    GerritConfig,
    QualityConfig,
    ReviewConfig,
    RetryConfig,
    ScannerConfig,
    StatusMapping,
    WorkspaceConfig,
    load_config,
)
from .inventory import InventoryDocument, InventoryLoader, InventoryRow, load_inventory_csv
from .models import (
    AIReviewStatus,
    FinalReviewStatus,
    QualityDecision,
    ReviewTargetKey,
    ScanStatus,
    SecurityDecision,
    SourceKey,
    SourceSelectionStatus,
    StaticReviewStatus,
    TaskStatus,
    normalize_branch,
    normalize_skill_path,
)
from .orchestrator import (
    OrchestrationError,
    cleanup_repository_workspace,
    finalize_repository,
    plan_repositories,
    prepare_repository,
)

__all__ = [
    "AIReviewStatus",
    "BatchConfig",
    "ConcurrencyConfig",
    "FinalReviewStatus",
    "GerritConfig",
    "InventoryDocument",
    "InventoryLoader",
    "InventoryRow",
    "QualityConfig",
    "QualityDecision",
    "ReviewConfig",
    "ReviewTargetKey",
    "RetryConfig",
    "ScanStatus",
    "ScannerConfig",
    "SecurityDecision",
    "SourceKey",
    "SourceSelectionStatus",
    "StaticReviewStatus",
    "StatusMapping",
    "TaskStatus",
    "WorkspaceConfig",
    "load_config",
    "load_inventory_csv",
    "normalize_branch",
    "normalize_skill_path",
    "OrchestrationError",
    "cleanup_repository_workspace",
    "finalize_repository",
    "plan_repositories",
    "prepare_repository",
]

__version__ = "0.2.0"
