"""Research orchestration.

Order matters: behavioural relations and deltas first, then novelty, then the
bounded validation pass. Nothing here promotes a signal to a finding.
"""

from __future__ import annotations

import os

from ..naming import qualify
from ..symbolic import SymbolicEngine
from .behavior import derive_behavior_relations
from .boundary import propose_boundaries
from .composition import compose
from .concrete import fuzz_hypothesis
from .constraint import derive_constraints
from .delta_anomalies import detect_delta_anomalies
from .differential import generate_differential_candidates
from .evidence import make_evidence
from .finding_gate import anti_finding_checks, gate_report
from .foundry import detect_foundry, execute_hypothesis, plan_hypothesis
from .impact import infer_impact_paths
from .mutations import generate_mutations
from .novelty import score_novelty
from .order_sensitivity import detect_order_sensitivity
from .statedelta import derive_state_deltas
from .unknown_behavior import discover_unknown_behaviors

VALIDATION_BUDGET = 8
# Real-EVM runs compile the target, so they are budgeted separately. Sequences
# beyond the budget still get a generated harness they can be replayed with.
FOUNDRY_BUDGET = 3


def run_research(result):
    contracts = result["contracts"]
    engine = result.get("symbolic_engine") or SymbolicEngine(contracts)
    result["symbolic_engine"] = engine

    result["behavior_relations"] = derive_behavior_relations(contracts)
    result["state_deltas"] = derive_state_deltas(
        contracts, result["sequence_hypotheses"], engine
    )
    result["differential_candidates"] = generate_differential_candidates(
        contracts, result["state_deltas"], engine
    )
    result["mutations"] = generate_mutations(result["sequence_hypotheses"])
    result["impact_paths"] = infer_impact_paths(
        result["protocol_model"], result["protocol_invariants"],
        result["sequence_hypotheses"], contracts,
    )
    result["delta_anomalies"] = detect_delta_anomalies(
        result["state_deltas"], result.get("protocol_model"),
        # Qualified, so an accounting pair binds inside one contract instead of
        # pairing one contract's assets against another's supply.
        {qualify(contract.name, variable.name)
         for contract in contracts for variable in contract.state_vars},
    )
    result["composition_candidates"] = compose(result)

    # v3 engines.
    result["order_sensitivity"] = detect_order_sensitivity(
        contracts, result["state_graph"], engine,
    )
    result["boundary_proposals"] = propose_boundaries(contracts)

    result["novel_behaviors"] = score_novelty(
        contracts, result["state_deltas"], result["differential_candidates"],
        engine, result.get("detectors"),
    )
    result["unknown_behavior_candidates"] = discover_unknown_behaviors(
        result["novel_behaviors"]
    )

    result["constraints"] = []
    result["concrete_validation"] = []
    result["foundry_capabilities"] = detect_foundry()
    result["foundry_validation"] = []

    # Concrete validation is deliberately limited to high-priority unknown
    # behaviour candidates and composition candidates. It remains a proof
    # producer, not a finding confirmer.
    hypotheses = []
    hypotheses.extend(x.sequence for x in result["unknown_behavior_candidates"][:VALIDATION_BUDGET])
    hypotheses.extend(x.chain for x in result["composition_candidates"][:VALIDATION_BUDGET])
    project = result.get("project") or (
        result["sources"][0].parent if result["sources"] else "."
    )

    allow_foundry = result.get("use_foundry", True) and \
        os.environ.get("CRYSTAL_NO_FOUNDRY", "").strip() not in {"1", "true", "yes"}

    seen = set()
    executed = 0
    for hypothesis in hypotheses:
        key = tuple(hypothesis)
        if key in seen:
            continue
        seen.add(key)
        result["constraints"].append(
            derive_constraints(hypothesis, result["state_deltas"], engine)
        )
        result["concrete_validation"].append(
            fuzz_hypothesis(contracts, hypothesis, trials=256, seed=0, engine=engine)
        )
        # Real-EVM validation is attempted only when Foundry is available and
        # the hypothesis is executable without fabricated constructor/ABI data.
        if allow_foundry and executed < FOUNDRY_BUDGET:
            result["foundry_validation"].append(
                execute_hypothesis(project, contracts, hypothesis)
            )
            executed += 1
        else:
            result["foundry_validation"].append(
                plan_hypothesis(project, contracts, hypothesis)
            )

    result["evidence_records"] = _evidence(result)
    result["finding_gate"] = {
        "policy": "zero-false-positive-confirmed",
        "anti_finding_checks": anti_finding_checks(None),
        "decisions": [],
        "concrete_validation_is_not_confirmation": True,
        "report": gate_report(result),
    }
    return result


def _evidence(result):
    records = []
    delta_by_sequence = {
        tuple(delta.sequence): delta for delta in result["state_deltas"]
    }
    constraint_by_sequence = {}
    for hypothesis, system in zip(
        [tuple(x) for x in _validated_sequences(result)], result["constraints"]
    ):
        constraint_by_sequence[hypothesis] = system

    for candidate in result["unknown_behavior_candidates"][:50]:
        key = tuple(candidate.sequence)
        delta = delta_by_sequence.get(key)
        system = constraint_by_sequence.get(key)
        limitations = ["Not a vulnerability conclusion",
                       "Requires protocol-level analysis"]
        if delta is not None:
            limitations.extend(delta.unsupported)
        records.append(make_evidence(
            status="RESEARCH",
            hypothesis=list(candidate.sequence),
            observations=["novel behavior candidate requiring protocol-level validation"],
            state_delta=dict(delta.delta) if delta is not None else {},
            constraints=list(system.expressions) if system is not None else [],
            novelty=candidate.novelty,
            known_similarity=candidate.known_similarity,
            limitations=limitations,
            provenance={"source": "crystal-novel-behavior-engine",
                        "model": delta.model if delta is not None else "unknown"},
        ))

    for detector_signal in result.get("detectors", [])[:100]:
        records.append(make_evidence(
            status="RESEARCH",
            hypothesis=[f"{detector_signal.contract}.{detector_signal.function}"],
            observations=[detector_signal.title, detector_signal.reason],
            constraints=list(detector_signal.ordered_trace),
            limitations=["Not a vulnerability conclusion"] +
                        list(detector_signal.falsification),
            provenance={
                "source": f"crystal-detector:{detector_signal.detector}",
                "path": detector_signal.path,
                "line": detector_signal.line,
                "confidence": detector_signal.confidence,
                "references": list(detector_signal.references),
            },
        ))
    return records


def _validated_sequences(result):
    sequences = []
    seen = set()
    for candidate in result["unknown_behavior_candidates"][:VALIDATION_BUDGET]:
        key = tuple(candidate.sequence)
        if key not in seen:
            seen.add(key)
            sequences.append(list(candidate.sequence))
    for candidate in result["composition_candidates"][:VALIDATION_BUDGET]:
        key = tuple(candidate.chain)
        if key not in seen:
            seen.add(key)
            sequences.append(list(candidate.chain))
    return sequences
