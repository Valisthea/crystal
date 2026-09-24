# Integrating Crystal into MIRA

Written against Build 019. Everything stated about the current code was checked
in the repository, not recalled.

## The short answer

You do not start here.

The roadmap's own §47 puts the MIRA adapter at step 19 of 19, and that ordering
is right for a reason this repository has already demonstrated twice: an
integration seam built over an incoherent interior encodes the incoherence.
Crystal shipped a protocol layer that decided by spelling (fixed in 016) and a
symbolic budget that decided by alphabetical order (fixed in 018). An adapter
written before either would have exported both as contract.

What you build now is the *shape* the adapter will need, inside Crystal, where
it is testable without MIRA existing.

## Where the seam already is

Half of it is built and has been for several builds. `report.arcadia()` emits a
structured hand-off under `crystal-arcadia/2.0` whose first object is a refusal
of authority:

```json
"producer": {
  "tool": "crystal",
  "role": "evidence-only",
  "decides_severity": false,
  "decides_submission": false
}
```

Alongside it: `target`, `signals`, `state_model`, `validation`,
`evidence_records`, `campaigns`, `gate`, `quality`, `counts`, and — since
Builds 017–019 — `isolation`, `sequence_budget` and `producer_provenance`.

That payload is already close to what §37 asks a MIRA adapter to expose. It is
the **evidence** half.

## The half that does not exist

There is no way to ask Crystal a question. The only programmatic entry is:

```python
research(project, use_solc=True, languages=None, run_detector_pass=True,
         use_foundry=True, detectors=None, include_tests=False, packs=())
```

No objective, no budget, no prior evidence, no validation depth, no requested
capability. MIRA can read Crystal and cannot direct it, which means Crystal is
currently a *scanner* MIRA consumes rather than an *instrument* MIRA operates.

§36's operations — `experiment(hypothesis)`, `symbolically_test(property)`,
`trace(scenario)`, `compare(baseline, candidate)` — do not exist as addressable
entry points. The work behind them does; the addressing does not.

## The decision this needs before the adapter is written

Crystal's hand-off is named for Arcadia and versioned `crystal-arcadia/2.0`.
Under the layering in this roadmap, Arcadia is no longer the top:

```
MIRA      global research operating system
Arcadia   research / reasoning engine
Crystal   deep analysis / experiment / evidence engine
```

So the adapter has to be addressed at one of two places, and they are different
designs:

**A — Crystal speaks to Arcadia; MIRA speaks to Arcadia.** Crystal keeps one
consumer and one payload. `crystal-arcadia/2.0` stays valid. Arcadia translates.
Fewest moving parts, and Crystal never learns MIRA exists.

**B — Crystal speaks to both.** A second, MIRA-addressed envelope alongside the
Arcadia one. More surface, and the risk §38 names: the adapter starts
duplicating capability definitions that already live inside Crystal.

**A is the recommendation**, and not only for simplicity: under B, Crystal ends
up with two consumers who can disagree about what a result means, and resolving
that disagreement is a global responsibility Crystal is forbidden to hold
(§39). Nothing in the current payload needs to change for A.

This is not blocking. Steps 2 through 18 are identical either way.

## What Crystal already satisfies

Checked against the roadmap, not asserted:

| roadmap | state |
| --- | --- |
| §9 evidence-first | `EvidenceRecord` carries `limitations` and `provenance`; the finding gate has eight proof obligations, two of which a machine never sets |
| §10 falsification persists | Build 019 — survives a real process restart, tested by spawning one |
| §11 provenance | Build 019 — content-addressed, clock-free, never inferred from the current tree |
| §12 evidence identity | deterministic; Build 019 deliberately left the identity material untouched so no existing id was reissued |
| §14 "no result" is meaningful | `UNSUPPORTED` / `VACUOUS` / `REFUSED` / `TIMEOUT` / `EXECUTED_PASS` are distinct and survive every backend |
| §18 fuzzing | `HELD` raises `WitnessRequired` without an execution witness, so "no failure found" cannot become "property holds" |
| §19 backends as capabilities | `BackendCapabilities` and `BackendTraits` declare version, availability, and `unmodelled_precompiles` |
| §35 standalone | no Crystal module imports anything MIRA-shaped; the whole suite runs without it |
| §46 forbidden list | the structural ones are enforced by tests in a named CI gate |

## What is missing, in the roadmap's order

Steps 1 and 2–4 are done. What remains before an adapter is worth writing:

| step | gap |
| --- | --- |
| 5 | **question model** — no `AnalysisQuestion`; Crystal answers "what did you find" and cannot be asked "can you establish X" |
| 6–7 | strategy engine is question-blind; every campaign runs on every target |
| 8 | **experiment model** — backend runs produce results, not recorded experiments with prediction and environment |
| 9 | backend capabilities are declared but not exported as a registry a parent can read |
| 13 | **contradiction handling** — if Halmos holds a property under constraints and Foundry produces a counterexample, Crystal has nowhere to record the disagreement |
| 17 | no benchmark corpus; each build measures on targets chosen for that build |
| 28 | resource metadata is partial — timeouts exist, memory and parallelism do not |

Step 5 is the one that changes Crystal's character. Everything after it is
easier once a question is a first-class object, and the adapter at step 19 is
mostly a rename of operations that already exist by then.

## What must never move into Crystal

From §39, and worth repeating because "temporary" versions of these become
permanent:

```
global queue · leases · research priorities · research debt
global coverage · saturation · hypothesis authority
attack-chain authority · human approval · submission
programme and cross-target monitoring
```

Crystal's `finding_gate` already refuses the last of these structurally:
`CONFIRMED` is unreachable by machine because two of the eight gates require
protocol interpretation. That refusal is the model for all of them.

## The boundary, stated as two questions

```
Crystal    What can I determine, test, falsify, or evidence — here, now,
           on this source, with these tools?

MIRA       What should we investigate next, why, across everything we know,
           and when is the research state actually complete?
```

A Crystal proposal is a *proposal*. `ResearchCandidate` carries `claim`,
`rationale`, `falsification`, `assumptions`, `provenance` and what it is
`missing` — and no severity, no priority, no submission state. Whether a
proposal becomes part of global research is MIRA's answer, not Crystal's, and
Crystal must never assume every candidate it emits becomes a MIRA hypothesis.

## Status

| step | build |
| --- | --- |
| 1 · architecture audit | [EVOLUTION_ASSESSMENT.md](EVOLUTION_ASSESSMENT.md) |
| 2 · hypothesis-model duplication | 019 |
| 3 · falsification persistence | 019 |
| 4 · provenance hardening | 019 |
| 5 · question model | 020 — [research-question.md](research-question.md) |
| 6–7 · question-driven strategy, evidence steering | 021 — surface and prior evidence steer the budget; `adaptive` depth still an obstruction |
| 8 · experiment model | next |

Steps 17 and 22 of the earlier architecture brief — isolation and the symbolic
budget — were taken out of order as Builds 017 and 018, because the first was a
credential exposure and the second was a measured misallocation. Both are
recorded in [CHANGELOG.md](../CHANGELOG.md).
