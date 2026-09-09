"""Safe building blocks for repository-at-a-time batch Skill review.

Network, scanner, AI-import, candidate-export and cleanup boundaries remain
separate and operator-visible. No module executes target Skill content or
automatically commits, pushes, or publishes candidates.
"""

# Install repository-owned compatibility boundaries before orchestration modules
# bind their local imports. Existing callers keep the same public API while new
# configurations use the actual canonical .agents policy location.
from . import artifacts as _artifact_module
from .platform_artifacts import install_artifact_platform_compat

install_artifact_platform_compat(_artifact_module)

from . import evidence_index as _evidence_index_module
from .evidence_index_concurrency import install_evidence_index_concurrency

install_evidence_index_concurrency(_evidence_index_module)

from .evidence_index import IndexedEvidenceStore

_artifact_module.EvidenceStore = IndexedEvidenceStore
_artifact_module.RestrictedEvidenceStore = IndexedEvidenceStore

from . import html_reporting as _html_reporting
from .indexed_html_reporting import write_html_report as _indexed_write_html_report

_html_reporting.write_html_report = _indexed_write_html_report

from .bilingual_html_reporting import install_bilingual_html_reporting

install_bilingual_html_reporting(_html_reporting)

from . import config as _config_module
from .path_compat import install_config_path_compat

install_config_path_compat(_config_module)

# Public/durable security decisions use one machine code. Historical policy code
# emitted BLOCK while models/reporting already used BLOCKED.
from . import review_policy as _review_policy_module

_review_policy_module.SECURITY_BLOCK = "BLOCKED"

# New reviews use deterministic score-based automatic approval. Capture/install
# this before orchestration/per-skill modules bind evaluate_policy locally.
from .approval_policy import evaluate_policy as _automatic_evaluate_policy

_review_policy_module.evaluate_policy = _automatic_evaluate_policy

from . import live_report as _live_report_module
from . import reporting as _reporting_module
from .overall_reporting import install_reporting_compat

install_reporting_compat(_reporting_module, _live_report_module)

# Localization is a report-only projection. Install it after deterministic
# overall-decision enrichment and before orchestration/completion modules bind
# the live-report function. Canonical review/evidence state is never rewritten.
from .localization_reporting import install_localization_reporting

install_localization_reporting(_live_report_module)

# The job/import API also needs package-level path validation and must preserve
# each unit's locale when it is merged into Translation Memory.
from . import localization_job as _localization_job_module
from .localization_job_safety import install_localization_job_safety

install_localization_job_safety(_localization_job_module)

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

__version__ = "0.3.0"
