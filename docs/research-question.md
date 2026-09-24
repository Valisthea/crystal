# `ResearchQuestion` — Crystal's input contract

Until Build 020 there was one way into Crystal:

```python
research(project, use_solc=True, packs=())
```

That describes *how to run the tool*. It never describes what security question
the run should try to settle. `ResearchQuestion` is that missing half.

**Architecture note.** This is a Crystal-native contract, introduced before the
Arcadia seam exists and with no dependency on Arcadia or MIRA. Connecting those
layers later is expected to add fields, adapters or a translation layer. Such
changes must preserve the standalone contract and must not move global authority
into Crystal. That is deliberate sequencing, not a defect.

What this establishes is narrow and worth stating plainly: **Crystal can be
asked something through a formal contract.** It does not mean Crystal
understands the question semantically, nor that anything can yet schedule it.

---

## A question, end to end

```python
from crystal.question import (
    ResearchQuestion, SourceSnapshot, Surface, Target, execute, validate,
)

asked = ResearchQuestion(
    question=(
        "Can a quote produced under a stale Chainlink price still satisfy "
        "isValidSignature without the margin relation being preserved?"
    ),
    hypothesis=(
        "If the feed's updatedAt exceeds maxStalenessSeconds, the cached "
        "quote can settle outside the tolerance band."
    ),
    target=Target(target_id="lidofinance/stonks@main",
                  repository="lidofinance/stonks"),
    source_snapshot=SourceSnapshot(revision="main", tree_digest="bed526ee…"),
    affected_surface=(
        Surface("function", "Order.isValidSignature"),
        Surface("state_variable", "Stonks::MARGIN_DIFFERENCE_IN_BASIS_POINTS"),
    ),
    required_capabilities=("static", "symbolic", "composition"),
    constraints={"symbolic_budget": 60, "excluded_paths": ["stubs"]},
    depth="standard",
    expected_output=("evidence", "candidate"),
)

assert validate(asked).ok
run = execute(asked, "/path/to/checkout")
```

A good question is **specific, testable and bounded**. `"check zero value"`
describes an action; the question above describes what must be determined.

---

## Identity

`question_id` is **content-derived** — a digest over the semantics and nothing
else, so the same question about the same world is recognisably the same
question, and a result can be attributed without a session holding the mapping.

It covers the schema **MAJOR**, not the full version. A MINOR is backward
compatible by definition, so it cannot change what a question is. Build 020
hashed the full string — which would have reissued every question's identity on
each MINOR bump. Documents written under 1.0 still load and still verify against
the id they were written with; that id is kept as `legacy_question_id` in the
loaded question's provenance.

Provenance is deliberately excluded from it. *Who* asked does not change *what*
was asked, and including it would make two identical questions from two callers
look like two questions.

```python
asked.with_provenance(asked_by="arcadia").question_id == asked.question_id  # True
```

## `schema_version`

`MAJOR.MINOR`, currently `1.0`.

* **MAJOR** — a breaking semantic change. A reader refuses a MAJOR it does not
  know, before interpreting any field. Reading unknown fields under the old
  meaning is how a question quietly becomes a different question.
* **MINOR** — a backward-compatible extension. Unknown fields are ignored, and
  that is safe *because* the identity check runs afterwards: if an ignored field
  carried meaning, the reconstructed id will not match and the load fails loudly.

## Question ≠ hypothesis

They are carried in separate fields and never merged.

| | |
| --- | --- |
| `question` | *Is property P violated?* |
| `hypothesis` | *If condition C holds, transition T can violate P.* |

Merging them would let a proposal arrive disguised as an open question.

## `target`

`target_id` is the caller's stable handle, and it is required. Crystal does not
derive an identity from a path basename: two unrelated engagements both
containing `contracts/` would collide.

Optional and used when the caller has them: `project`, `repository`, `path`,
`contract`, `deployment`, `chain`.

## `source_snapshot` — the world is never assumed

A question is always about a specific world. **A missing snapshot is an error,
not a default.** Crystal never substitutes the current checkout, because that
answers a question about a tree the asker never described.

A caller who genuinely wants resolution at execution says so:

```python
SourceSnapshot(revision=AT_EXECUTION)   # recorded, and warned about
```

which validates with `snapshot.deferred` — the result is reproducible only
against the snapshot recorded in its answer.

## `affected_surface` — empty never means everything

Each surface is a `kind` Crystal resolves and a stable `identifier`. Never a
list position or a path suffix: both move when unrelated code changes.

Kinds: `function`, `contract`, `contract_family`, `state_variable`,
`call_path`, `asset_flow`, `state_transition`.

An empty surface is valid **only** with `discover_surface=True`. Without it, the
question is refused with `surface.empty`.

### What a surface does (Build 021)

Before anything expensive runs, each item is checked against the parsed code
(0.2–0.3 s on the reference protocols) and resolved to the entry points it
covers:

| kind | resolves to |
| --- | --- |
| `function` | that function |
| `contract` | every function of the contract |
| `state_variable` | the functions that read or write it — `Stonks::X` for one contract, bare `X` for any |
| `call_path` | `A.f -> B.g`: its members, if every one exists |
| `asset_flow`, `state_transition`, `contract_family` | **unsupported** — reported, not approximated |

An item that exists nowhere in the analysed code is **unresolved** — a typo, a
rename, a question written against another revision. If *nothing* resolves, the
run is refused with `REFUSED_UNRESOLVED_SURFACE` before analysis starts: running
anyway would answer a question about the rest of the target and file it under
this one. Resolution uses the same contract set research does, so a surface that
only exists in a mock is not a surface of the target.

The resolved surface then **decides where the symbolic budget goes**. Sequences
touching it are executed first, shared round-robin across items so one busy
function cannot take every slot; score still ranks within each item. Measured,
same budget of 30:

| | Build 020 | Build 021 |
| --- | ---: | ---: |
| stonks — surface sequences executed | 13 / 30 | **30 / 30** |
| stonks — `Order.isValidSignature` | **0** | 5 (all that exist) |
| Flyover — surface sequences executed | 5 / 30 | **30 / 30** |
| Flyover — `isCollateralSufficient` | **0** | 15 |

The surface **does not filter outputs**. Every run reports candidates, detector
signals and anomalies split `on_surface` / `elsewhere` — a finding one call away
from the surface is still a finding.

Without a question, nothing changes: the legacy `research()` path produces the
Build 020 ordering exactly (476 candidates / 150 deltas / 58 signals on stonks,
before and after).

## `constraints` — declared means enforced

| constraint | effect |
| --- | --- |
| `allowed_paths` | *not enforced* — discovery filters exclusions only, and the run says so |
| `excluded_paths` | drops these path prefixes from discovery, matched on **segments** (`test` drops `test/Foo.sol`, never `latest/Foo.sol`) |
| `max_sequence_length` | longest call sequence a campaign may consider |
| `symbolic_budget` | how many sequence hypotheses may be executed |
| `timeout_seconds` | wall-clock limit handed to an external backend |

Anything else is an **error**. A constraint accepted and then ignored produces a
result that looks bounded when it was not, and nothing downstream can tell.

## `prior_evidence`

```python
PriorEvidence(evidence_id="E1", claim="…", polarity="supports",
              provenance={"producer": "crystal", "revision": "57fb317"})
```

`polarity` is `supports`, `refutes` or `inconclusive`. Evidence **changes what
is worth doing; it does not become true.**

Evidence without a producer and a world stays usable and stays *marked*
(`evidence.unattributable`): a step skipped on the strength of an
unattributable claim is a gap nobody can audit later.

### How evidence steers (Build 021, schema 1.1)

Schema 1.1 adds `PriorEvidence.about` — the surface the evidence concerns.
Polarity is read relative to the question: `supports` is consistent with its
hypothesis holding, `refutes` against it.

Evidence **reorders the surface, it never removes an item.** Each item takes a
tier, and the budget reaches tiers in this order:

1. **contradicted** — attributable evidence both supports and refutes it. Reported
   as a contradiction with both sides named; Crystal does not pick one.
2. **uncertain** — refuting or inconclusive evidence.
3. **no prior evidence**.
4. **supported** — still analysed, later in each round. Skipping it would be
   trusting a claim Crystal did not establish.

Evidence steers nothing when it cannot be attributed (no producer or source),
when it has no `about` — Crystal does not infer from a claim's wording which
part of the target it concerns — or when what it is about does not overlap the
question's surface. Each such item is listed under `not_steering` with the reason.

## `required_capabilities`

A capability is a *kind of analysis*, never a tool name. A question asks for
`symbolic`; it does not ask for Halmos.

| capability | providers |
| --- | --- |
| `static` | `crystal.detectors` |
| `dataflow` | `crystal.semantics.dataflow` |
| `symbolic` | `crystal.symbolic.engine`, `halmos` |
| `differential` | `crystal.research.differential` |
| `composition` | `crystal.composition` |
| `fuzzing` | `medusa`, `echidna` |
| `runtime` | `foundry` |

An unknown capability is refused. Accepting one silently produces an answer to
a different question from the one asked.

`crystal.question.export()` hands the registry to a parent system, deliberately
*without* availability: what Crystal can be asked is a property of Crystal;
what is installed is a property of a machine.

## `budget`

| dimension | |
| --- | --- |
| `symbolic_sequences` | sequence hypotheses executed symbolically |
| `experiments` | external backend runs launched |
| `wall_time_seconds` | wall-clock limit per external backend run |

A budget is a **ceiling**. The planner takes the narrowest of the depth's
allowance, the constraint and the budget — never the widest.

```
depth deep (300) → constraint symbolic_budget 60 → budget 60  ⇒  60
```

## `depth`

| value | what it permits |
| --- | --- |
| `shallow` | parsing, detectors, dataflow. No symbolic sequence execution, no external backend |
| `standard` | shallow, plus in-process symbolic sequence execution at the default budget |
| `deep` | standard, plus external backends where available, and a raised budget |
| `adaptive` | re-plan after each result — **declared and not implemented** |

`adaptive` is planned as an **obstruction**, not quietly downgraded to
`standard`. Silently answering an easier question is the failure this contract
exists to prevent.

## `expected_output`

`candidate`, `evidence`, `proof`, `counterexample`, `trace`, `differential`,
`inconclusive`. A **preference**, never a demand — it does not make Crystal
fabricate a result of a shape it could not establish.

---

## Validation

`validate(question)` checks **form and feasibility**. It does not decide whether
a question is interesting, severe or worth resources — that would be the global
research judgement Crystal is forbidden to hold, in the one place a caller would
least expect to find it.

Every error carries a code: `schema.unknown_major`, `snapshot.missing`,
`surface.empty`, `constraint.unenforceable`, `budget.unenforceable`,
`capability.unknown`, `depth.unknown`, `evidence.duplicate`, `target.missing`,
`question.empty`, and the rest.

## Persistence and replay

```python
path = dump(asked, "question.json")
# … another process, no shared memory …
same = load("question.json")
assert same.question_id == asked.question_id
```

Serialisation is canonical: the same question produces the same bytes on any
machine. `loads` verifies the identity it reconstructs against the identity
recorded in the document and raises `SemanticDrift` on a mismatch — a question
whose meaning moved in transit must not run under its old identity.

## Execution

```python
run = execute(asked, "/path/to/checkout")
run.status      # see below
run.plan        # considered, selected, obstructions, budget
run.surface     # resolved, unresolved, unsupported
run.steering    # evidence tiers, contradictions, what did not steer
run.narrowed    # what the question actually changed about the run
run.unapplied   # what it asked for and did not get
run.on_surface  # outputs split on_surface / elsewhere
```

| status | meaning |
| --- | --- |
| `EXECUTED` | the analysis ran |
| `REFUSED_INVALID` | the question is malformed |
| `REFUSED_NO_SOURCES` | nothing analysable at the path |
| `REFUSED_UNRESOLVED_SURFACE` | nothing the question names exists in this code |
| `REFUSED_UNPLANNABLE` | no strategy can answer it here |

Each refusal separates "analysis not run" from "analysis found nothing". Build
020 answered a path with no sources as `EXECUTED` with every count at zero —
indistinguishable from a clean target.

Statuses are local to a run. There is no `COMPLETE` and no `CONFIRMED` here, and
there will not be.

---

## Migrating from `research(...)`

The legacy call keeps working and is not going anywhere until something measured
replaces it. To see what an existing invocation actually asks for:

```python
from crystal.question import from_legacy_call
from_legacy_call("./target", use_foundry=False)
```

Translation makes its two implicit assumptions visible, and both are the ones
this contract refuses:

* **no snapshot** — it scans whatever is on disk. Written down: `AT_EXECUTION`;
* **no surface** — it looks at everything. Written down: `discover_surface=True`.

Neither is invented. Both are what the call already did, said out loud.

`to_legacy_kwargs()` maps the other way and is lossy; `lost` names exactly what
has no legacy equivalent, rather than letting a caller believe the round trip
was faithful.

---

## The boundary

Nothing in this contract carries global authority. There is no field for
severity, payout, submission, priority, coverage, saturation, research debt,
leases or human review, and an injected one is dropped on load.

```
Crystal    What can I determine, test, falsify, or evidence — here, now,
           on this source, with these tools?

MIRA       What should we investigate next, why, across everything we know,
           and when is the research state actually complete?
```

## Future mapping

Not implemented, and no dependency on it exists in the code:

```
Arcadia ResearchProposal
        ↓  (Arcadia translates surface, capabilities, budget, depth, prior evidence)
ResearchQuestion
        ↓
Crystal
```

MIRA may eventually decide what to investigate and pass it through Arcadia. The
Crystal contract must remain usable without either — see
[mira-integration.md](mira-integration.md).
