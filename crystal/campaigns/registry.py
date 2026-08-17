"""Campaign registry: discovers and manages protocol packs and their campaigns."""

from __future__ import annotations

import importlib
from pathlib import Path

from .definition import CampaignDefinition


class CampaignRegistry:
    """Central registry of all known campaigns."""

    def __init__(self) -> None:
        self._campaigns: dict[str, CampaignDefinition] = {}
        self._packs: dict[str, list[str]] = {}

    def register(self, campaign: CampaignDefinition) -> None:
        self._campaigns[campaign.campaign_id] = campaign
        self._packs.setdefault(campaign.pack, []).append(campaign.campaign_id)

    def get(self, campaign_id: str) -> CampaignDefinition | None:
        return self._campaigns.get(campaign_id)

    def list_campaigns(self) -> list[CampaignDefinition]:
        return sorted(self._campaigns.values(),
                       key=lambda c: (-c.priority, c.campaign_id))

    def list_packs(self) -> list[str]:
        return sorted(self._packs.keys())

    def campaigns_for_pack(self, pack: str) -> list[CampaignDefinition]:
        ids = self._packs.get(pack, [])
        return [self._campaigns[i] for i in ids if i in self._campaigns]

    def enabled(self) -> list[CampaignDefinition]:
        return [c for c in self.list_campaigns() if c.enabled]

    def load_pack(self, pack_module: str) -> int:
        """Load campaigns from a pack module that exports ``CAMPAIGNS``."""
        try:
            mod = importlib.import_module(pack_module)
        except ImportError:
            return 0
        campaigns = getattr(mod, "CAMPAIGNS", [])
        for campaign in campaigns:
            self.register(campaign)
        return len(campaigns)


def discover_packs(extra_dirs: list[Path] | None = None) -> CampaignRegistry:
    """Build a registry from built-in packs and optional extra directories."""
    registry = CampaignRegistry()

    builtin = [
        "crystal.packs.generic",
        "crystal.packs.defi",
        "crystal.packs.registry",
        "crystal.packs.authorization",
        "crystal.packs.migration",
        "crystal.packs.economic",
    ]
    for module in builtin:
        registry.load_pack(module)

    for directory in extra_dirs or []:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"crystal.packs.{path.stem}"
            registry.load_pack(module_name)

    return registry
