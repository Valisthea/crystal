"""Module-level graph over a runtime-composed system.

The analysis itself lives in `crystal.composition`; this module keeps the graph
view (edges between modules) and the import path that existed before the
composition package was split out. Having one classifier rather than two is the
point: a second copy would eventually disagree with the first about what a guard
is, and only one of them would be wired to the detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..composition import ConfigResolver, Pipeline, build_topology, extract_pipelines
from ..composition.config_resolver import associated_name


@dataclass(frozen=True)
class ModuleEdge:
    source: str
    target: str
    kind: str
    via: str = ""
    confidence: float = 0.6


@dataclass
class ModuleGraph:
    pipelines: list[Pipeline] = field(default_factory=list)
    edges: list[ModuleEdge] = field(default_factory=list)
    modules: dict[str, str] = field(default_factory=dict)
    associated_types: dict[str, str] = field(default_factory=dict)
    resolver: ConfigResolver | None = None

    def __bool__(self) -> bool:
        return bool(self.pipelines or self.edges)

    def resolve(self, reference: str) -> str:
        if self.resolver is None:
            return ""
        return self.resolver.resolve(reference).contract


def build_module_graph(contracts, wirings=(), bindings=(), root=".",
                       sources=()) -> ModuleGraph:
    topology = build_topology(root, sources)
    resolver = ConfigResolver(bindings, contracts, topology)
    by_name = {contract.name: contract for contract in contracts}

    graph = ModuleGraph(
        pipelines=extract_pipelines(contracts, wirings, bindings, topology),
        modules={
            contract.name: contract.module or contract.path for contract in contracts
        },
        associated_types=resolver.as_map(),
        resolver=resolver,
    )

    for pipeline in graph.pipelines:
        for stage in pipeline.stages:
            for other in pipeline.stages:
                if stage.index < other.index:
                    graph.edges.append(ModuleEdge(
                        stage.name, other.name, "runs-before", pipeline.name, 0.9,
                    ))

    for contract in contracts:
        for function in contract.functions:
            if function.ir is None:
                continue
            for call in function.ir.calls():
                receiver = call.receiver or ""
                direct = receiver.split("::")[-1].strip()
                if direct in by_name and direct != contract.name:
                    graph.edges.append(ModuleEdge(
                        contract.name, direct, "calls", call.callee, 0.7,
                    ))
                    continue
                resolution = resolver.resolve(receiver)
                if resolution.contract and resolution.contract != contract.name:
                    graph.edges.append(ModuleEdge(
                        contract.name, resolution.contract, "config-routed",
                        f"{associated_name(receiver)}::{call.callee}", 0.85,
                    ))

    seen = set()
    unique = []
    for edge in graph.edges:
        key = (edge.source, edge.target, edge.kind, edge.via)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    graph.edges = sorted(unique, key=lambda x: (x.source, x.kind, x.target))
    return graph
