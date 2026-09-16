"""`ResearchQuestion` — the first way to ask Crystal something.

Until now Crystal's interface described *how to run it*:

    research(project, use_solc=..., packs=...)

and never *what security question it should try to settle*. This module is the
canonical form of the question. It is deliberately a Crystal-native contract:
nothing here knows that Arcadia or MIRA exist, and nothing here carries global
research authority — no severity, no priority, no submission state, no coverage.

Two refusals are built into the shape rather than left to a caller's discipline:

* **The world is never assumed.** A question is always about a specific snapshot.
  Omitting it does not mean "whatever is checked out right now" — that silently
  answers a different question from the one asked, on a tree the asker never saw.
  A caller who genuinely wants resolution at execution says so, and it is recorded.
* **An empty surface is never "everything".** It means "find the surface", and
  only if the caller asked for discovery.

See `docs/research-question.md`, and the architecture note there about this
being a contract introduced before the Arcadia seam exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

SCHEMA_VERSION = "1.0"
SCHEMA_MAJOR = 1

# What a caller passes as the snapshot when the world is to be resolved at
# execution time. Explicit so the choice appears in the record, and in the
# question's identity, instead of being inferred from an absence.
AT_EXECUTION = "resolve-at-execution"

# Depth is a named research posture, not a magic number. Each value names
# exactly what it permits; `crystal/question/strategy.py` is the only place
# that turns these into knobs.
DEPTHS = {
    "shallow": "parsing, detectors and dataflow. No symbolic execution of "
               "sequences, no external backend.",
    "standard": "shallow, plus in-process symbolic sequence execution under "
                "the default budget. No external backend.",
    "deep": "standard, plus external backends where the capability is "
            "available, and a raised symbolic budget.",
    "adaptive": "re-plan after each result. Declared by the schema and NOT "
                "implemented: a question asking for it is planned as an "
                "obstruction rather than quietly downgraded to standard.",
}

# Constraints Crystal can actually enforce. §13: a constraint that is declared
# and then ignored is worse than one that is refused, because the result looks
# like it honoured a bound it never applied.
ENFORCEABLE_CONSTRAINTS = {
    "allowed_paths": "restrict discovery to these path prefixes",
    "excluded_paths": "drop these path prefixes from discovery",
    "max_sequence_length": "longest call sequence a campaign may consider",
    "symbolic_budget": "how many sequence hypotheses may be executed",
    "timeout_seconds": "wall-clock limit handed to an external backend",
}

# Budget dimensions Crystal can actually hold itself to.
ENFORCEABLE_BUDGET = {
    "symbolic_sequences": "sequence hypotheses executed symbolically",
    "experiments": "external backend runs launched",
    "wall_time_seconds": "wall-clock limit per external backend run",
}

# What a caller may prefer to receive. A preference, never a demand: §19, and
# Crystal does not fabricate a result of a shape it could not establish.
EXPECTED_OUTPUTS = frozenset({
    "candidate", "evidence", "proof", "counterexample", "trace",
    "differential", "inconclusive",
})

SURFACE_KINDS = frozenset({
    "function", "contract", "contract_family", "state_variable",
    "call_path", "asset_flow", "state_transition",
})

# How a prior evidence item relates to the claim it is about.
POLARITIES = frozenset({"supports", "refutes", "inconclusive"})


@dataclass(frozen=True)
class Surface:
    """Something Crystal is being asked to look at, identified stably.

    `identifier` is a name Crystal can resolve — `Vault.withdraw`,
    `Vault::totalAssets`. Never a list position and never a path suffix: both
    move when unrelated code changes, so a question written against one stops
    meaning what it meant.
    """

    kind: str
    identifier: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "identifier": self.identifier}


@dataclass(frozen=True)
class SourceSnapshot:
    """The world a question is about."""

    revision: str = ""
    tree_digest: str = ""
    configuration_digest: str = ""
    branch: str = ""
    chain: str = ""
    deployment: str = ""

    @property
    def resolve_at_execution(self) -> bool:
        return self.revision == AT_EXECUTION

    @property
    def identifies_a_world(self) -> bool:
        return bool(self.revision or self.tree_digest)

    def as_dict(self) -> dict:
        return {
            key: value for key, value in (
                ("revision", self.revision),
                ("tree_digest", self.tree_digest),
                ("configuration_digest", self.configuration_digest),
                ("branch", self.branch),
                ("chain", self.chain),
                ("deployment", self.deployment),
            ) if value
        }


@dataclass(frozen=True)
class Target:
    """What is being analysed, identified by more than a readable name.

    `target_id` is the caller's stable handle. A basename is not one: two
    unrelated engagements both containing `contracts/` would collide, and §9
    forbids inventing an identity from one.
    """

    target_id: str
    project: str = ""
    repository: str = ""
    path: str = ""
    contract: str = ""
    deployment: str = ""
    chain: str = ""

    def as_dict(self) -> dict:
        return {
            key: value for key, value in (
                ("target_id", self.target_id),
                ("project", self.project),
                ("repository", self.repository),
                ("path", self.path),
                ("contract", self.contract),
                ("deployment", self.deployment),
                ("chain", self.chain),
            ) if value
        }


@dataclass(frozen=True)
class PriorEvidence:
    """Something already known, handed in so Crystal need not rediscover it.

    It changes what is worth doing. It does not become true: a claim arriving
    with a polarity is a report of somebody else's conclusion, and Crystal
    records whose, from where, before letting it steer anything.
    """

    evidence_id: str
    claim: str
    polarity: str = "inconclusive"
    provenance: dict = field(default_factory=dict)

    @property
    def sufficiently_provenanced(self) -> bool:
        """Enough to say who produced this and from what world.

        Evidence without it is still usable as a hint and must stay marked —
        §15 — because a strategy skipped on the strength of an unattributable
        claim is a gap nobody can later audit.
        """
        return bool(self.provenance.get("producer")) and bool(
            self.provenance.get("source_digest")
            or self.provenance.get("revision")
        )

    def as_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "claim": self.claim,
            "polarity": self.polarity,
            "provenance": dict(sorted(self.provenance.items())),
        }


@dataclass(frozen=True)
class ResearchQuestion:
    """A precise, versioned, reproducible question put to Crystal.

    `question` is interrogative — what must be determined. `hypothesis` is the
    optional conditional claim a caller already has. They are kept apart on
    purpose (§8): "is property P violated?" and "if C holds, T can violate P"
    are different inputs, and merging them would let a proposal arrive disguised
    as an open question.
    """

    question: str
    target: Target
    source_snapshot: SourceSnapshot
    schema_version: str = SCHEMA_VERSION
    hypothesis: str = ""
    affected_surface: tuple[Surface, ...] = ()
    # Empty surface means "discover it", and only when asked. It never means
    # "the whole project" by default.
    discover_surface: bool = False
    constraints: dict = field(default_factory=dict)
    prior_evidence: tuple[PriorEvidence, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    budget: dict = field(default_factory=dict)
    depth: str = "standard"
    expected_output: tuple[str, ...] = ()
    # Who asked, and from where. Never Crystal's own producer stamp: that
    # belongs to the answer.
    provenance: dict = field(default_factory=dict)

    @property
    def schema_major(self) -> int:
        try:
            return int(str(self.schema_version).split(".", 1)[0])
        except (TypeError, ValueError):
            return -1

    @property
    def question_id(self) -> str:
        """Content-derived, from the semantics and nothing else.

        The same question about the same world, surface, constraints and budget
        is the same question — so a caller re-asking it can be recognised, and a
        result can be attributed without a session to hold the mapping.

        Provenance is excluded on purpose: *who* asked does not change *what*
        was asked, and including it would make two identical questions from two
        callers look like two questions.
        """
        return hashlib.sha256(
            canonical_json(self.semantic_content()).encode("utf-8")
        ).hexdigest()[:32]

    def semantic_content(self) -> dict:
        """Everything that makes this question the question it is."""
        return {
            "schema_version": self.schema_version,
            "question": self.question,
            "hypothesis": self.hypothesis,
            "target": self.target.as_dict(),
            "source_snapshot": self.source_snapshot.as_dict(),
            "affected_surface": [s.as_dict() for s in self.affected_surface],
            "discover_surface": self.discover_surface,
            "constraints": dict(sorted(self.constraints.items())),
            "prior_evidence": [e.as_dict() for e in self.prior_evidence],
            "required_capabilities": sorted(self.required_capabilities),
            "budget": dict(sorted(self.budget.items())),
            "depth": self.depth,
            "expected_output": sorted(self.expected_output),
        }

    def as_dict(self) -> dict:
        """The full record, identity included."""
        return {
            "question_id": self.question_id,
            **self.semantic_content(),
            "provenance": dict(sorted(self.provenance.items())),
        }

    def with_provenance(self, **fields) -> "ResearchQuestion":
        """A copy carrying who asked. The identity does not move."""
        return replace(self, provenance={**self.provenance, **fields})


def canonical_json(value) -> str:
    """One representation per value, so a digest over it is stable.

    Sorted keys, no incidental whitespace, and `ensure_ascii` off so a question
    written in any language digests as the text it is rather than as its escape
    sequences.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
