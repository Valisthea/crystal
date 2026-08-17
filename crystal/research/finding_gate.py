"""The finding gate.

Crystal never produces a confirmed finding on its own. Two of the eight proof
gates — `economic_impact` and `minimal_trace` — require protocol interpretation
and are therefore never set automatically. The gate can report that a candidate
is one reproducible trace away from being provable, and it can ask the Foundry
backend for a proof-of-concept scaffold, but the decision stays with the human
or with Arcadia.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

HUMAN_ONLY_GATES = ("economic_impact", "minimal_trace")


@dataclass(frozen=True)
class ProofChecklist:
    reachability: bool
    preconditions: bool
    state_transition: bool
    invariant_violation: bool
    attacker_authority: bool
    economic_impact: bool
    reproducible_trace: bool
    minimal_trace: bool

    @property
    def score(self):
        return sum([
            self.reachability, self.preconditions, self.state_transition,
            self.invariant_violation, self.attacker_authority,
            self.economic_impact, self.reproducible_trace, self.minimal_trace
        ])


@dataclass(frozen=True)
class FindingDecision:
    status: str
    reason: str
    proof: ProofChecklist


def evaluate(proof: ProofChecklist) -> FindingDecision:
    # Crystal is deliberately conservative: a confirmed finding requires
    # every proof gate, not merely a high aggregate score.
    required = {
        "reachability": proof.reachability,
        "preconditions": proof.preconditions,
        "state_transition": proof.state_transition,
        "invariant_violation": proof.invariant_violation,
        "attacker_authority": proof.attacker_authority,
        "economic_impact": proof.economic_impact,
        "reproducible_trace": proof.reproducible_trace,
        "minimal_trace": proof.minimal_trace,
    }
    missing = [k for k, v in required.items() if not v]
    if not missing:
        return FindingDecision("CONFIRMED", "all proof gates satisfied", proof)
    if proof.score >= 4:
        return FindingDecision("VALIDATION", "candidate requires proof of: " + ", ".join(missing), proof)
    return FindingDecision("RESEARCH", "insufficient evidence: " + ", ".join(missing), proof)


def anti_finding_checks(candidate):
    """
    Return falsification questions. A candidate is not a finding until these
    checks are answered with concrete evidence.
    """
    return [
        "Can the required state actually be reached by the claimed attacker?",
        "Can the alleged invariant remain valid for all satisfying inputs?",
        "Can the alleged economic gain be eliminated by a valid execution?",
        "Can the sequence be reproduced from a clean initial state?",
        "Can the trace be minimized without removing the effect?",
    ]


def assess(sequence, result) -> ProofChecklist:
    """Derive a checklist from evidence Crystal actually holds."""
    functions = _entry_points(result["contracts"])
    deltas = {tuple(d.sequence): d for d in result.get("state_deltas", [])}
    anomalies = {
        tuple(a.sequence) for a in result.get("delta_anomalies", [])
        if a.kind != "unresolved-effect"
    }
    concrete = {
        tuple(c.hypothesis): c for c in result.get("concrete_validation", [])
    }
    executed = {
        tuple(f.hypothesis): f for f in result.get("foundry_validation", [])
    }

    key = tuple(sequence)
    delta = deltas.get(key)
    fuzz = concrete.get(key)
    run = executed.get(key)

    reachability = all(name in functions for name in sequence)
    preconditions = bool(delta is not None and not delta.unsupported)
    state_transition = bool(delta is not None and delta.changed)
    invariant_violation = key in anomalies or bool(
        fuzz is not None and fuzz.counterexamples
    )
    attacker_authority = all(
        functions.get(name) is not None
        and not functions[name].modifiers for name in sequence
    )
    reproducible_trace = bool(run is not None and run.status.startswith("EXECUTED"))

    return ProofChecklist(
        reachability=reachability,
        preconditions=preconditions,
        state_transition=state_transition,
        invariant_violation=invariant_violation,
        attacker_authority=attacker_authority,
        # Never derived automatically: needs protocol interpretation.
        economic_impact=False,
        reproducible_trace=reproducible_trace,
        minimal_trace=False,
    )


def gate_report(result) -> dict:
    """Per-candidate gate status plus the list of missing proofs."""
    assessments = []
    poc_requests = []

    candidates = [list(x.sequence) for x in result.get("unknown_behavior_candidates", [])[:25]]
    candidates += [list(x.chain) for x in result.get("composition_candidates", [])[:25]]

    seen = set()
    for sequence in candidates:
        key = tuple(sequence)
        if key in seen:
            continue
        seen.add(key)
        proof = assess(sequence, result)
        decision = evaluate(proof)
        assessments.append({
            "sequence": sequence,
            "status": decision.status,
            "reason": decision.reason,
            "score": proof.score,
            "proof": asdict(proof),
            "human_only_gates": list(HUMAN_ONLY_GATES),
        })
        missing = [
            name for name, satisfied in asdict(proof).items() if not satisfied
        ]
        if set(missing) <= {"reproducible_trace", *HUMAN_ONLY_GATES}:
            if not proof.reproducible_trace:
                poc_requests.append({
                    "sequence": sequence,
                    "reason": "every machine-checkable gate is satisfied; a "
                              "reproducible trace would complete the evidence",
                    "backend": "foundry",
                })

    return {
        "assessments": assessments,
        "poc_requests": poc_requests,
        "human_only_gates": list(HUMAN_ONLY_GATES),
        "note": "economic_impact and minimal_trace are never set automatically; "
                "CONFIRMED is not a Crystal responsibility.",
    }


def _entry_points(contracts):
    return {
        f"{contract.name}.{function.name}": function
        for contract in contracts for function in contract.functions
    }
