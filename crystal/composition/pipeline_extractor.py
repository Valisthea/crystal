"""Ordered pipelines, with each stage resolved and classified."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config_resolver import ConfigResolver
from .stage_classifier import StageRole, classify, inherit_routed

PIPELINE_NAMES = {"txextension", "signedextra", "transactionextension",
                  "extra", "signedextensions", "txextensions"}


@dataclass
class Pipeline:
    name: str
    kind: str
    path: str
    line: int
    stages: list[StageRole] = field(default_factory=list)
    declared: list[str] = field(default_factory=list)

    @property
    def guards(self) -> list[StageRole]:
        return [stage for stage in self.stages if stage.is_guard]

    @property
    def movers(self) -> list[StageRole]:
        return [stage for stage in self.stages if stage.is_mover]

    @property
    def unresolved(self) -> list[StageRole]:
        return [stage for stage in self.stages if not stage.resolved]

    def render(self) -> list[str]:
        return [
            f"[{stage.index}] {stage.name} {stage.role}"
            + (f" — {stage.scope()}" if stage.resolved else " (not parsed)")
            for stage in self.stages
        ]


def _short(name: str) -> str:
    return re.sub(r"<[^>]*>", "", name or "").strip().rsplit("::", 1)[-1]


def is_transaction_pipeline(name: str) -> bool:
    return (name or "").lower().replace("_", "") in PIPELINE_NAMES


def extract_pipelines(contracts, wirings=(), bindings=(), topology=None,
                      only_transaction_pipelines: bool = True) -> list[Pipeline]:
    """Build one classified pipeline per declared extension tuple."""
    by_name = {contract.name: contract for contract in contracts}
    resolver = ConfigResolver(bindings, contracts, topology)
    pipelines: list[Pipeline] = []

    for wiring in wirings or ():
        if wiring.kind != "extension-pipeline":
            continue
        if only_transaction_pipelines and not is_transaction_pipeline(wiring.name):
            continue

        pipeline = Pipeline(wiring.name, wiring.kind, wiring.path, wiring.line,
                            declared=list(wiring.members))
        for index, member in enumerate(wiring.members):
            short = _short(member)
            contract = by_name.get(short)
            if contract is None:
                stage = StageRole(short, index, False)
                stage.crate = topology.crate_of(member) if topology else ""
                pipeline.stages.append(stage)
                continue
            stage = classify(contract, resolver)
            stage = inherit_routed(stage, by_name, resolver)
            stage.index = index
            stage.crate = topology.crate_of(member) if topology else ""
            pipeline.stages.append(stage)
        pipelines.append(pipeline)

    return pipelines
