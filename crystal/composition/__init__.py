"""Cross-module composition for runtime-composed systems.

Everything else in Crystal reads one module at a time. This package reads how
modules are wired together — the ordered extension pipeline, the pallet
declarations, the Config bindings — because some defects exist only in the
composition and are absent from every file taken alone.

Only DECLARED composition is visible here: a tuple, a `construct_runtime!`, a
`Config` impl. Composition that emerges at runtime (dynamic dispatch, governance
proposals, hooks registered at genesis) is not, and a boundary this package does
not report is not evidence of absence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .boundary_detector import BoundaryCrossing, find_crossings
from .config_resolver import ConfigResolver, Resolution
from .pipeline_extractor import Pipeline, extract_pipelines
from .runtime_wiring import (
    RuntimeModule,
    RuntimeTopology,
    WorkspaceProfile,
    build_topology,
)
from .stage_classifier import (
    CHECKS_ONLY,
    GUARD,
    MOVES_VALUE,
    OBSERVES,
    UNRESOLVED,
    StageRole,
)

__all__ = [
    "CHECKS_ONLY", "GUARD", "MOVES_VALUE", "OBSERVES", "UNRESOLVED",
    "BoundaryCrossing", "CompositionModel", "ConfigResolver", "Pipeline",
    "Resolution", "RuntimeModule", "RuntimeTopology", "StageRole",
    "WorkspaceProfile", "build_composition", "build_topology",
    "extract_pipelines", "find_crossings",
]

DECLARED_ONLY = (
    "Crystal sees only declared composition (extension tuples, "
    "construct_runtime!/#[frame_support::runtime], Config impls). Composition "
    "that emerges at runtime is invisible to it, and an unreported boundary is "
    "not evidence of absence."
)


@dataclass
class CompositionModel:
    topology: RuntimeTopology | None = None
    pipelines: list[Pipeline] = field(default_factory=list)
    crossings: list[BoundaryCrossing] = field(default_factory=list)
    resolver: ConfigResolver | None = None
    warning: str = ""
    limits: str = DECLARED_ONLY

    @property
    def associated_types(self) -> dict[str, str]:
        return self.resolver.as_map() if self.resolver else {}

    def __bool__(self) -> bool:
        return bool(self.pipelines)


def build_composition(contracts, wirings=(), bindings=(), root=".", sources=(),
                      unbounded_by_contract=None) -> CompositionModel:
    topology = build_topology(root, sources)
    pipelines = extract_pipelines(contracts, wirings, bindings, topology)
    model = CompositionModel(
        topology=topology,
        pipelines=pipelines,
        resolver=ConfigResolver(bindings, contracts, topology),
        warning=topology.profile.warning() if topology.profile else "",
    )
    for pipeline in pipelines:
        model.crossings.extend(find_crossings(pipeline, unbounded_by_contract))
    model.crossings.sort(key=lambda x: -x.confidence)
    return model
