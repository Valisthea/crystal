"""Downstream demand for a sequence, computed before it is executed.

Every signal here asks the same question in a different way: *does anything
downstream consume the result?* None of them asks what a function is called.
That distinction is the one Build 012 set and Build 016 applied to the protocol
layer; the sequence budget was the last place still deciding by spelling.

The signals are free. All three read data the pipeline has already produced by
the time sequences are ranked — detector signals and the parsed contracts — so
ordering by demand costs nothing that executing the sequences would not have
cost anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# What each signal is worth. Deliberately flat rather than tuned: these break
# ties inside one score band, and a precise weighting would be a claim about
# relative importance that nothing here measures.
WEIGHTS = {
    "signalled": 0.50,
    "cross_contract": 0.30,
    "mutating": 0.20,
}


@dataclass(frozen=True)
class Demand:
    """Why a sequence is worth one of the budget's slots."""

    signalled: int = 0          # functions already carrying a detector signal
    cross_contract: bool = False
    mutating: int = 0           # functions that write state
    length: int = 0

    @property
    def score(self) -> float:
        """In [0, 1]. Counts are flattened to presence: two signals on a path
        are not twice the evidence, they are the same path."""
        total = (
            WEIGHTS["signalled"] * bool(self.signalled)
            + WEIGHTS["cross_contract"] * bool(self.cross_contract)
            + WEIGHTS["mutating"] * bool(self.mutating)
        )
        return round(total, 4)

    def reasons(self) -> tuple[str, ...]:
        out = []
        if self.signalled:
            out.append(f"{self.signalled} function(s) already carry a detector signal")
        if self.cross_contract:
            out.append("spans more than one contract")
        if self.mutating:
            out.append(f"{self.mutating} function(s) write state")
        if not out:
            out.append("no detector signal, single contract, writes no state")
        return tuple(out)


@dataclass(frozen=True)
class Deferred:
    """A hypothesis the budget did not reach, and what it would have been worth."""

    sequence: tuple[str, ...]
    score: float
    demand: float
    reasons: tuple[str, ...]


@dataclass
class Allocation:
    """The split, with enough detail that a reader can audit the boundary."""

    executed: list = field(default_factory=list)
    deferred: list[Deferred] = field(default_factory=list)
    budget: int = 0
    considered: int = 0
    # Hypotheses sharing the score at which the budget ran out. When this is
    # large the score is not doing the ranking, and the tiebreak is.
    tied_at_boundary: int = 0
    boundary_score: float | None = None

    def report(self) -> dict:
        return {
            "budget": self.budget,
            "considered": self.considered,
            "executed": len(self.executed),
            "deferred": len(self.deferred),
            "boundary_score": self.boundary_score,
            "tied_at_boundary": self.tied_at_boundary,
            "ordering": "score, then downstream demand, then length and name",
            "note": (
                "deferred sequences were not executed because the symbolic "
                "budget ran out, not because they were judged uninteresting. "
                "Raising the budget is not the intended fix; the ordering is."
            ),
            "deferred_detail": [
                {
                    "sequence": list(item.sequence),
                    "score": item.score,
                    "demand": item.demand,
                    "reasons": list(item.reasons),
                }
                for item in self.deferred[:40]
            ],
        }


def _signalled_functions(detectors) -> set[str]:
    out = set()
    for signal in detectors or ():
        contract = getattr(signal, "contract", None)
        function = getattr(signal, "function", None)
        if contract and function:
            out.add(f"{contract}.{function}")
    return out


def _writes_by_function(contracts) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for contract in contracts or ():
        for function in contract.functions:
            out[f"{contract.name}.{function.name}"] = tuple(sorted(function.writes))
    return out


def demand_for(sequence, *, signalled, writes) -> Demand:
    """The demand signals for one sequence. No execution, and no names.

    A fourth signal was measured and removed: whether an enabled campaign's
    `allowed_categories` accepted the state the sequence writes. On a real
    target it was true for 95% of hypotheses — the accepted set covered every
    category `classify_state` can return, so it was `mutating` wearing a
    different label, and it reached that answer through substring matches on
    state-variable names. A signal that does not discriminate is not worth a
    nominal dependency.
    """
    sequence = tuple(sequence)
    contracts = {name.split(".", 1)[0] for name in sequence if "." in name}
    mutating = sum(1 for name in sequence if writes.get(name))

    return Demand(
        signalled=sum(1 for name in sequence if name in signalled),
        cross_contract=len(contracts) > 1,
        mutating=mutating,
        length=len(sequence),
    )


def schedule_sequences(hypotheses, *, budget, detectors=(), contracts=()) -> Allocation:
    """Order hypotheses for a bounded symbolic budget, and say what was left.

    The score stays the primary key. Nothing here promotes a sequence past one
    the generator ranked above it — which would be scoring the ranking twice.
    Demand replaces the *tiebreak*, and on a real target the tie is where the
    decision actually lives: 175 of 198 hypotheses shared one score, so the
    lexicographic fallback was choosing 32 of the 48 discards by spelling.
    """
    hypotheses = list(hypotheses or ())
    signalled = _signalled_functions(detectors)
    writes = _writes_by_function(contracts)

    scored = [
        (hypothesis, demand_for(hypothesis.sequence, signalled=signalled, writes=writes))
        for hypothesis in hypotheses
    ]

    # Primary key untouched. Length and name stay as the final tiebreak so the
    # allocation is deterministic when demand cannot separate two either.
    scored.sort(key=lambda pair: (
        -float(pair[0].score),
        -pair[1].score,
        len(pair[0].sequence),
        tuple(pair[0].sequence),
    ))

    executed = [hypothesis for hypothesis, _ in scored[:budget]]
    deferred = [
        Deferred(tuple(hypothesis.sequence), float(hypothesis.score),
                 demand.score, demand.reasons())
        for hypothesis, demand in scored[budget:]
    ]

    boundary_score = None
    tied = 0
    if scored and budget < len(scored):
        boundary_score = float(scored[budget - 1][0].score) if budget else None
        if boundary_score is not None:
            tied = sum(
                1 for hypothesis, _ in scored
                if abs(float(hypothesis.score) - boundary_score) < 1e-9
            )

    return Allocation(
        executed=executed, deferred=deferred, budget=budget,
        considered=len(scored), tied_at_boundary=tied,
        boundary_score=boundary_score,
    )
