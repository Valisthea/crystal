"""Campaign system: scoped, gated analysis that controls what Crystal investigates.

A campaign is not "run all detectors". It is:

    scope → target functions → state transitions → top candidates → validation

Everything outside the campaign is DEFERRED, not analysed. This prevents
combinatorial explosion and keeps signal quality high.
"""

from __future__ import annotations

from .definition import (
    CampaignDefinition,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)
from .registry import CampaignRegistry, discover_packs
from .result import CampaignCandidate, CampaignResult
from .runner import run_campaign, run_campaigns

__all__ = [
    "CampaignDefinition",
    "CampaignCandidate",
    "CampaignResult",
    "CampaignRegistry",
    "CampaignScope",
    "CampaignTransition",
    "TransitionKind",
    "discover_packs",
    "run_campaign",
    "run_campaigns",
]
