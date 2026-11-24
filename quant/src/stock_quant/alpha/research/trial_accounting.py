"""Trial-accounting façade used by reports and integration tests."""
from __future__ import annotations

from .contracts import TrialAccounting
from .experiment_registry import ResearchExperimentRegistry


def account_trials(registry: ResearchExperimentRegistry, campaign_id: str) -> TrialAccounting:
    """Return six distinct scientific counts plus execution and exposure diagnostics."""
    return registry.trial_accounting(campaign_id)
