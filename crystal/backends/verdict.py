"""Verdicts that carry their witness, or do not exist.

The dominant failure of property tooling is not the false positive; it is the
silent success. Measured on one protocol on 2026-09-05: 35 tests green over
1,049,043 calls with zero successful deposits, because the actors had a zero
balance the whole run. Every one of those greens was true of a system in which
nothing ever happened.

So a property here has four verdicts, and one of them is guarded:

* `HELD`        — the tool evaluated the property, and at least one transition
                  that mutates the state the property is about actually
                  succeeded. Cannot be constructed otherwise.
* `VIOLATED`    — the tool produced a counterexample; the trace is the witness.
* `VACUOUS`     — the tool reported green but nothing it did can substantiate
                  it. The reason names what never succeeded.
* `UNSUPPORTED` — Crystal or the backend cannot decide the property, with the
                  reason (a struct argument it cannot fabricate, a block delay
                  the backend cannot advance past).

`HELD` is made structurally unreachable without a witness the same way
`confirmed_findings` is structurally zero in `crystal/research/finding_gate.py`:
the constructor refuses it. `dataclasses.replace` goes through the same
constructor, so a VACUOUS verdict cannot be promoted after the fact either.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

HELD = "HELD"
VIOLATED = "VIOLATED"
VACUOUS = "VACUOUS"
UNSUPPORTED = "UNSUPPORTED"
VERDICTS = (HELD, VIOLATED, VACUOUS, UNSUPPORTED)


class WitnessRequired(ValueError):
    """HELD was asked for without a witness that substantiates it."""


@dataclass(frozen=True)
class ActionOutcome:
    """What happened to one entry point over the run.

    `successes` are calls that did not revert. A call that succeeds without
    changing anything (`deposit(0)`) counts as a success: the witness proves
    the write was reached, not that the delta was non-zero. When the tool
    reports coverage rather than counts, `executed_mutation` carries the
    evidence instead and the counts stay None rather than being estimated.
    """

    action: str
    calls: int | None = None
    successes: int | None = None
    reverts: int | None = None
    revert_reasons: tuple[tuple[str, int], ...] = ()
    mutates_subject: bool = False
    handler: str = ""
    executed_mutation: bool | None = None

    @property
    def success_ratio(self) -> float | None:
        if not self.calls:
            return None
        return (self.successes or 0) / self.calls

    @property
    def succeeded(self) -> bool:
        return bool(self.successes) or self.executed_mutation is True

    def describe(self) -> str:
        if self.calls is not None:
            head = f"{self.action} {self.successes or 0}/{self.calls} succeeded"
        elif self.executed_mutation is not None:
            head = f"{self.action} " + (
                "executed without reverting" if self.executed_mutation
                else "never executed without reverting"
            )
        else:
            head = f"{self.action}: no execution metrics"
        if self.revert_reasons:
            reasons = ", ".join(f"{reason} x{count}" for reason, count in self.revert_reasons[:4])
            if len(self.revert_reasons) > 4:
                reasons += ", ..."
            head += f" — {reasons}"
        return head


@dataclass(frozen=True)
class ExecutionWitness:
    """The execution evidence behind a verdict.

    `subject` is the state the property is about; `actions` are the entry
    points the tool exercised, flagged with whether they write that state.
    `sequences` is exact when the tool reports it and None otherwise — it is
    never estimated from a call count.
    """

    backend: str
    source: str
    subject: tuple[str, ...]
    checked: bool = False
    calls: int | None = None
    sequences: int | None = None
    actions: tuple[ActionOutcome, ...] = ()
    revert_selectors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def mutating(self) -> tuple[ActionOutcome, ...]:
        return tuple(action for action in self.actions if action.mutates_subject)

    @property
    def substantiating(self) -> tuple[ActionOutcome, ...]:
        return tuple(action for action in self.mutating if action.succeeded)

    @property
    def total_calls(self) -> int | None:
        if self.calls is not None:
            return self.calls
        counts = [action.calls for action in self.actions if action.calls is not None]
        return sum(counts) if counts else None

    def gap(self) -> str:
        """Why this witness cannot carry HELD; empty when it can."""
        if not self.checked:
            return f"{self.backend} never reported evaluating the property"
        if not self.subject:
            return (
                "the property is not tied to any state variable of the contract, "
                "so no transition can be shown to mutate what it is about"
            )
        subject = ", ".join(self.subject)
        if not self.actions:
            return f"no per-action execution metrics were decoded from {self.source}"
        if self.total_calls == 0:
            return f"no call was executed ({self.source} reports 0 calls)"
        mutating = self.mutating
        if not mutating:
            executed = ", ".join(action.action for action in self.actions) or "nothing"
            return f"no executed entry point writes {subject}; executed: {executed}"
        if not self.substantiating:
            return "no state-mutating transition succeeded: " + "; ".join(
                action.describe() for action in mutating
            )
        return ""

    @property
    def sufficient(self) -> bool:
        return not self.gap()

    def summary(self) -> str:
        parts = []
        if self.total_calls is not None:
            parts.append(f"{self.total_calls} calls")
        parts.append(
            f"{self.sequences} sequences" if self.sequences is not None
            else "sequences not reported"
        )
        for action in self.substantiating:
            parts.append(action.describe())
        if self.revert_selectors:
            parts.append("reverts: " + ", ".join(self.revert_selectors[:6]))
        return "; ".join(parts)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["mutating_successes"] = [action.action for action in self.substantiating]
        data["gap"] = self.gap()
        return data


@dataclass(frozen=True)
class PropertyVerdict:
    """A verdict on one property. HELD cannot exist without a sufficient witness."""

    property: str
    verdict: str
    reason: str
    witness: ExecutionWitness | None = None
    counterexamples: tuple[str, ...] = ()

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise ValueError(
                f"unknown verdict {self.verdict!r}; one of {', '.join(VERDICTS)}"
            )
        if self.verdict == HELD:
            if self.witness is None:
                raise WitnessRequired(
                    f"{self.property}: HELD requires an execution witness; none attached"
                )
            gap = self.witness.gap()
            if gap:
                raise WitnessRequired(f"{self.property}: HELD refused — {gap}")
        if self.verdict == VIOLATED and not self.counterexamples:
            raise ValueError(f"{self.property}: VIOLATED requires a counterexample")
        if self.verdict in {VACUOUS, UNSUPPORTED} and not self.reason:
            raise ValueError(f"{self.property}: {self.verdict} requires a reason")

    def to_dict(self) -> dict:
        return {
            "property": self.property,
            "verdict": self.verdict,
            "reason": self.reason,
            "witness": self.witness.to_dict() if self.witness else None,
            "counterexamples": list(self.counterexamples),
        }


def decide(name: str, witness: ExecutionWitness | None = None,
           counterexamples=(), unsupported_reason: str = "") -> PropertyVerdict:
    """The one way verdicts are produced from evidence.

    Order matters: an unsupported property is not judged; a counterexample is
    conclusive whatever else happened; a green without a witness is VACUOUS,
    and the reason says what never succeeded.
    """
    if unsupported_reason:
        return PropertyVerdict(name, UNSUPPORTED, unsupported_reason, witness)
    traces = tuple(counterexamples)
    if traces:
        return PropertyVerdict(
            name, VIOLATED, f"{len(traces)} counterexample(s) reported", witness, traces
        )
    if witness is None:
        return PropertyVerdict(name, VACUOUS, "no execution witness was produced")
    gap = witness.gap()
    if gap:
        return PropertyVerdict(name, VACUOUS, gap, witness)
    return PropertyVerdict(name, HELD, "witnessed: " + witness.summary(), witness)


def tally(verdicts) -> dict[str, int]:
    counts = {verdict: 0 for verdict in VERDICTS}
    for item in verdicts:
        counts[item.verdict] += 1
    return counts


def summarise(verdicts) -> str:
    """One line for a backend's `reason` field: counts, then the vacuous reasons."""
    items = list(verdicts)
    if not items:
        return "no property verdict"
    counts = tally(items)
    head = ", ".join(f"{verdict} {count}" for verdict, count in counts.items() if count)
    details = [
        f"{item.property}: {item.reason}" for item in items
        if item.verdict in {VACUOUS, UNSUPPORTED}
    ]
    return head + (" — " + " | ".join(details) if details else "")
