"""Campaign registry: discovers and manages protocol packs and their campaigns."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from .definition import CampaignDefinition
from ..paths import glob_files


class CampaignRegistry:
    """Central registry of all known campaigns."""

    def __init__(self) -> None:
        self._campaigns: dict[str, CampaignDefinition] = {}
        self._packs: dict[str, list[str]] = {}
        # What each explicitly requested pack contributed, so a pack that
        # loaded zero campaigns is reported instead of silently ignored.
        self.load_report: dict[str, int] = {}
        # Campaigns that came from an operator-supplied pack (the `packs=`
        # argument), as opposed to a built-in. These are targeted at the
        # engagement, so when several campaigns select the same chain the
        # operator's is preferred as the one candidate that survives dedup.
        self.operator_campaign_ids: set[str] = set()

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


BUILTIN_PACKS = (
    "crystal.packs.generic",
    "crystal.packs.defi",
    "crystal.packs.registry",
    "crystal.packs.authorization",
    "crystal.packs.migration",
    "crystal.packs.economic",
)


def load_pack_file(registry: "CampaignRegistry", path) -> int:
    """Load a pack from a `.py` file anywhere on disk.

    A pack the operator wrote lives next to their engagement, not inside
    Crystal's own package, so importing it by dotted module name can never
    reach it. This loads the file directly under a private module name.
    """
    path = Path(path).resolve()
    if not path.is_file():
        return 0
    spec = importlib.util.spec_from_file_location(
        f"crystal._userpack_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        return 0
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a pack that imports itself does not recurse.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        return 0
    campaigns = getattr(module, "CAMPAIGNS", [])
    for campaign in campaigns:
        registry.register(campaign)
    return len(campaigns)


def load_packs(registry: "CampaignRegistry", specs) -> dict[str, int]:
    """Load each spec, accepting a dotted module path or a filesystem path.

    Returns what each spec contributed, so a pack that resolved to nothing is
    reported rather than silently ignored — a campaign pack that loads zero
    campaigns is indistinguishable from a working one at the output.
    """
    loaded: dict[str, int] = {}
    for spec in specs or []:
        text = str(spec)
        candidate = Path(text)
        before = set(registry._campaigns)
        if text.endswith(".py") or candidate.exists():
            loaded[text] = load_pack_file(registry, candidate)
        else:
            loaded[text] = registry.load_pack(text)
        # Whatever this operator spec newly registered is operator-supplied.
        registry.operator_campaign_ids |= set(registry._campaigns) - before
    return loaded


def discover_packs(extra_dirs: list[Path] | None = None,
                   packs=()) -> CampaignRegistry:
    """Build a registry from built-in packs, extra directories and explicit packs.

    `packs` accepts dotted module paths (`crystal.packs.ens`) and filesystem
    paths to a `.py` file, which is how an operator's own pack gets in.
    """
    registry = CampaignRegistry()

    for module in BUILTIN_PACKS:
        registry.load_pack(module)

    for directory in extra_dirs or []:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(glob_files(directory, "*.py")):
            if path.name.startswith("_"):
                continue
            # Try it as a Crystal-internal pack first, then as a loose file.
            if not registry.load_pack(f"crystal.packs.{path.stem}"):
                load_pack_file(registry, path)

    registry.load_report = load_packs(registry, packs)
    return registry
