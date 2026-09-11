# Crystal — evolution assessment

Written against Build 016 (`57fb317`), before any change. Section 23 of the
architectural brief asks for this before implementation, and the repository is
large enough that rewriting it blindly would destroy working subsystems.

Everything numbered here was measured on this checkout or on a real target —
`lidofinance/stonks`, 58 sources, 47 contracts — not recalled.

---

## A. Current architecture

129 modules, 29,177 lines, 54 test files, 492 collected tests. Sixteen builds.

```
discovery ──► parsers ──► language-neutral IR ──┬──► symbolic engine
  .sol .rs      tree-sitter                     │      canonical state deltas
  .go .move     regex fallback                  │      path constraints
  .vy           solc AST                        │
                                                └──► graphs
                                                       CFG · call · storage
                                                       causal state graph
                                                       (shared storage AND
                                                        cross-contract calls,
                                                        resolved through
                                                        declared types)
                                                          │
        ┌─────────────────────────────────────────────────┤
        ▼                     ▼                           ▼
   detectors (11)      research engine              campaign system
   LANGUAGES-gated     differential                 15 campaigns from
                       composition                  7 packs, scoped,
                       novelty                      order-sensitive
                       delta anomalies
        └─────────────────────┴───────────────────────────┘
                              ▼
                   finding gate — 8 proof gates
                   2 need protocol interpretation
                   and are NEVER set by machine
                              ▼
                    evidence records ──► writers
                                          json · sarif · markdown · arcadia
                              │
                              └──► crystal validate
                                     pre-flight refusal
                                     Foundry · Medusa · Halmos · Echidna
                                     verdict requires an execution witness
```

Package sizes, for a sense of where the mass sits:

| package | files | lines |
| --- | ---: | ---: |
| `parsers` | 10 | 6,537 |
| `detectors` | 11 | 3,504 |
| `backends` | 10 | 3,262 |
| `properties` | 6 | 2,784 |
| `research` | 18 | 2,439 |
| `symbolic` | 5 | 1,348 |
| `graphs` | 7 | 1,246 |
| `composition` | 6 | 915 |
| `protocol` | 10 | 903 |
| `packs` | 8 | 887 |
| `campaigns` | 6 | 813 |
| `semantics` | 8 | 764 |
| top level | 17 | 3,532 |

## B. Current responsibilities, against the brief's loop

| brief §4 stage | where it lives today | state |
| --- | --- | --- |
| understand contract | `parsers/`, `semantics/`, `ir.py` | strong — three front-ends, five languages, declared-type binding |
| model security surface | `graphs/state.py`, `symbolic/engine.py` | strong — contract-qualified state, causal edges across calls |
| extract properties | `properties/`, `protocol/`, `backends.base.derive_properties` | strong since Build 016; categories incomplete (see F) |
| generate hypotheses | `research/engine.py`, `hypotheses.py` | **two generations coexist** (see C.5) |
| select strategy | `campaigns/runner.py` | present, **not adaptive** (see C.1) |
| analyze | detectors + research engine | strong |
| collect evidence | `research/evidence.py` | flat records, **no graph** (see C.4) |
| validate | `backends/`, `verdict.py`, `preflight.py` | strong — witness-gated, refuses unprovable runs |
| correlate | `campaigns/runner.py` dedup, `quality/normalize.py` | **identity-level only** (see C.4) |
| return artifacts | `report.arcadia()`, schema `crystal-arcadia/2.0` | strong on output, **absent on input** (see C.2) |

## C. Gaps, in the order their evidence is strongest

### 1. No adaptive scheduling

> **Corrected by Build 018.** The mechanism described below was wrong, and
> finding out how was the build. The campaign runner has **no budget to
> allocate**: all 15 campaigns re-read the *same* 150 state deltas and prune.
> The "150 sequences explored" reported per campaign is the size of that shared
> list, not a slice any campaign consumed. Every number in the table was
> therefore a count of re-examinations, not of exploration.
>
> The budget is one step earlier and it is real: `derive_state_deltas` executes
> `sequence_hypotheses[:150]` — 150 of 198 on stonks, the rest discarded
> silently, and 175 of the 198 tied on score so the cut was decided by
> lexicographic order. Build 018 orders that cut by downstream demand and
> reports what it did not reach. Sequences carrying evidence that the budget
> skipped: 22/48 → 4/48 on stonks, 37/100 → 2/100 on the Flyover bridge.

The brief's §8 forbids executing every strategy equally, and the campaign layer
still does: every enabled campaign runs on every target regardless of whether
its premise has any surface there. On stonks, eleven of fifteen returned no
candidate, and a campaign with no surface is indistinguishable at the output
from one that looked and found nothing. That distinction is worth making, and
it is what remains of this gap.

### 2. Arcadia can read Crystal but cannot direct it

The output contract is in good shape — `report.arcadia()` emits eleven
sections under `crystal-arcadia/2.0`, declaring `role: evidence-only`,
`decides_severity: false`, `decides_submission: false`.

The input contract does not exist. The only programmatic entry is:

```python
research(project, use_solc=True, languages=None, run_detector_pass=True,
         use_foundry=True, detectors=None, include_tests=False, packs=())
```

No objective, no budget, no prior evidence, no validation depth, no requested
vulnerability classes. Everything the brief's §19 lists as an input is missing.
Arcadia can invoke Crystal and read the result; it cannot say *what to
investigate* or *how much to spend*.

### 3. The vulnerability knowledge model is flat

Eleven detectors, each independent, each with its own `FALSIFICATION` tuple and
`REFERENCES`. Campaign packs group campaigns by protocol family. Nothing
represents family → failure mode → pattern → conditions → verification, and
nothing represents the **combinations** §12 asks for. `composition/` composes
*call pipelines*, which is a different axis: it finds multi-contract call
chains, not interacting vulnerability classes.

### 4. Evidence is a list, not a graph; correlation is identity-level

`EvidenceRecord` is a flat frozen dataclass written to JSONL. Deduplication is
`stable_id` equality plus chain-equality within and across campaigns. Two
observations that share a root cause but differ in kind — a delta anomaly and a
detector signal on the same storage slot — are reported as two things. §14 and
§15 both need a link structure Crystal does not have.

### 5. Two hypothesis generations coexist

`crystal/hypotheses.py` is 60 lines of v1-era generation and `crystal/ranking.py`
is six lines sorting on a priority integer. Both are still imported. The real
work happens in `research/engine.py` and `campaigns/scoring.py`, which scores
novelty × state significance × causal depth × exploitability. The v1 pair
should be retired or absorbed, not left as a second answer to the same
question. Neither carries the fields §9 requires — prerequisites, actors,
assets, expected impact, verification method.

### 6. Resource isolation is not implemented — this is a security gap

§22 requires arbitrary repository code, generated tests, fuzzing, compilation
and dependency installation to run in disposable isolated environments.

Today: `process.run` shells out to Foundry, Medusa, Halmos and Echidna **on the
host**, in the operator's environment, with the operator's filesystem. Generated
harnesses are written into the target tree. `--pack` and `--fixture` load and
execute operator-supplied Python **in-process** — deliberate and documented in
[SECURITY.md](../SECURITY.md), but it is in-process nonetheless.

This is the only gap on the list that can hurt the operator rather than the
results. It should be ranked accordingly.

### 7. Self-evaluation is ad hoc

One benchmark file (`benchmarks/equal_parameter_benchmark.py`). No labelled
corpus, no precision/recall/F1 harness, no per-strategy yield tracking across
runs. Crystal's builds *are* measured — every changelog entry since 009 carries
before/after numbers on a real target — but each measurement is hand-built for
that build and not repeatable by anyone else. §18 asks for a framework; what
exists is a discipline.

### 8. Property categories are partial

`properties/` (2,784 lines) is the strongest single subsystem, and Build 016
grounded the protocol-invariant layer that feeds it. Of §5's fourteen
categories, Crystal covers conservation, accounting, authorization,
monotonicity, oracle consistency and — since Build 016 — precision/rounding in
the value-scaling form. Absent: solvency, collateralization, lifecycle
correctness, temporal correctness as a first-class category, cross-contract
consistency as a property rather than a graph edge. The property record also
lacks `actors`, `assets`, `preconditions` and `impact_if_violated`.

---

## D. The Arcadia / Crystal boundary

Already written, and already correct. [ARCHITECTURE.md](../ARCHITECTURE.md)
states the mission and eight explicit non-goals, every one of which matches the
brief's §20 and §21. `report.arcadia()` encodes the same boundary in the
payload itself.

**Nothing in this assessment proposes moving that line.** The scheduler in C.1
is local to Crystal's own smart-contract strategies; the knowledge model in C.3
is Crystal's security knowledge, not Arcadia's global graph; the evidence graph
in C.4 is a returned subgraph, not a persistent research memory.

The one place the boundary is under-served is the *input* side (C.2): Arcadia
is supposed to be able to configure a specialist, and currently cannot.

---

## E. Files to modify

| file | change |
| --- | --- |
| `crystal/campaigns/runner.py` | fixed slice → budget drawn from a scheduler |
| `crystal/campaigns/scoring.py` | expose per-campaign yield so the scheduler has a prior |
| `crystal/engine.py` | `research()` grows a structured request object; keyword args kept as the thin path |
| `crystal/report.py` | add `coverage`, `resource_usage`, `unresolved_hypotheses`, `recommended_next` to the Arcadia payload |
| `crystal/research/evidence.py` | records gain typed links; graph assembly beside it |
| `crystal/hypotheses.py`, `crystal/ranking.py` | retire into `research/`, or delete if nothing survives |
| `crystal/properties/model.py` | add actors, assets, preconditions, impact |
| `crystal/process.py` | isolation boundary for every external execution |

## F. New modules

| module | responsibility |
| --- | --- |
| `crystal/scheduling/` | local strategy scheduler: priority, budget draw, yield feedback |
| `crystal/knowledge/` | vulnerability family → failure mode → pattern → conditions, with composition edges |
| `crystal/evidence/graph.py` | the local evidence graph and root-cause merge |
| `crystal/api/request.py` | the structured investigation request Arcadia sends |
| `crystal/isolation/` | disposable execution environment for backends and packs |
| `benchmarks/corpus/` | labelled targets with known findings, for §18 |

## G. Interfaces to expose

An `InvestigationRequest` carrying target, scope, vulnerability classes,
objective, prior evidence, compute budget and validation depth; and an
`InvestigationResult` that is the current Arcadia payload plus coverage,
resource usage, unresolved hypotheses and a recommended next investigation.
Both versioned alongside `crystal-arcadia/2.0`, which stays valid.

## H. Migration strategy

One build per gap, each with a before/after measurement on a real target, in
this order — evidence strength first, except that C.6 jumps the queue because
it is a security gap:

1. ~~**017 — isolation** (C.6)~~ — shipped.
2. ~~**018 — scheduler** (C.1)~~ — shipped, having first corrected what the
   budget actually was.
3. **019 — request contract** (C.2). Measured by Arcadia being able to ask a
   scoped question and get a scoped answer.
4. **020 — evidence graph and root-cause merge** (C.4, C.5).
5. **021 — knowledge model and composition** (C.3).
6. **022 — property categories** (C.8).
7. **benchmark corpus** (C.7), built alongside from 018 onward rather than as
   its own build, since each build needs it.

## I. Risks

* **Scheduling can hide findings.** A campaign starved because it yielded
  nothing on one target may be the one that matters on the next. The scheduler
  must report what it did not explore, and a zero-yield campaign must be
  distinguishable from an unexplored one. This is the same failure Build 016
  hit twice: a rule that improves a number by hiding a case.
* **Isolation costs latency and can mask environment bugs.** Two of Crystal's
  shipped defects were Windows-only path behaviour; a container that always
  runs Linux would have hidden both.
* **A knowledge model invites the flat list it replaces.** If the model becomes
  a taxonomy that detectors look themselves up in, it is a lookup table with
  extra steps.
* **The request contract can leak orchestration inward.** An "objective" field
  that grows free-text planning is Arcadia's job arriving through the back door.
* **Retiring `hypotheses.py`/`ranking.py` touches live imports.** Six call sites
  across `crystal/` and `tests/`.

## J. Testing strategy

Every build in this sequence ships with:

* both parser paths green — `pytest -q` and `CRYSTAL_NO_TREESITTER=1 pytest -q`;
* the named `invariants` gate green, unchanged;
* new tests that pin a *reason*, not a count, in the style of
  `tests/test_v300_grounding.py`;
* a before/after measurement on a real target, in the changelog, with the
  README updated in the same commit;
* for the scheduler specifically: a test that a starved campaign is reported as
  starved, and a test that total explored sequences fall while candidates do
  not.

---

## Three conflicts with the brief that need a decision

These are not gaps. They are places where the brief and Crystal's existing
invariants disagree, and resolving them silently in either direction would be
wrong.

### 1. `VERIFIED` versus zero confirmed findings

§13 asks for statuses `SPECULATION / CANDIDATE / SUPPORTED / VERIFIED`, with
"a vulnerability must not be marked VERIFIED without sufficient evidence".

Crystal's central invariant is stronger: `CONFIRMED` is **structurally
unreachable by machine**, because two of the eight proof gates need protocol
interpretation and are never set automatically. `confirmed_findings == 0` is
asserted by eight tests running as their own named CI gate, and the README
states it in the first two hundred words.

If `VERIFIED` means *machine-verified*, it contradicts that invariant. If it
means *backed by an execution witness*, Crystal already has it: `HELD`, which
`PropertyVerdict` refuses to construct without a witness.

**Recommendation:** map the brief's ladder onto what exists —
`SPECULATION → CANDIDATE → SUPPORTED(=HELD, witness-backed) → VERIFIED(human or
Arcadia)` — and keep `VERIFIED` outside Crystal's reach. Not implemented
pending a decision.

### 2. "Confirmed findings per unit of compute" as the optimisation target

§2 and §17 both name confirmed findings as the metric. By §1 Crystal cannot
produce one. The measurable local proxy is *evidence accepted downstream*, which
Crystal cannot observe without Arcadia telling it.

**Recommendation:** Crystal optimises **candidates per sequence explored** and
**unique root causes per run** locally, and accepts an acceptance signal from
Arcadia as scheduler feedback. That keeps the real metric where the decision
lives.

### 3. Documentation-derived properties

§5 permits extracting properties from documentation while §5's last line
requires they not be automatically trusted. Crystal's zero-fabrication rule
currently forbids deriving *any* claim from prose.

**Recommendation:** documentation-derived properties enter at a distinct
provenance (`source: doc`) and a confidence ceiling, and may never satisfy a
proof gate on their own. Not implemented pending a decision.
