# Integrating Crystal into MIRA

Written against Build 019, updated at Build 022. Everything stated about the
current code was checked in the repository, not recalled.

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

## The input half — built in Builds 020 to 022

When this document was first written there was no way to ask Crystal a
question: the only entry was `research(project, use_solc=..., packs=...)`, which
describes how to run the tool and never what to settle. That is no longer so.

| build | what a caller can now do |
| --- | --- |
| 020 | express a question as a `ResearchQuestion` — world, surface, constraints, budget, depth, prior evidence — validated, versioned, persistable |
| 021 | have that question steer the analysis: its surface takes the symbolic budget, prior evidence reorders it, contradictions are reported |
| 022 | ask **from another process**: `crystal ask question.json --project <path>`, one `crystal-question-result/1.0` envelope back, an exit code that matches it, JSON Schemas for both documents in `schemas/` |

This is what makes Crystal an instrument Arcadia can operate rather than a
scanner it consumes. A non-Python orchestrator needs nothing from this repository
but the CLI and the two schema files.

What remains: §36's finer operations — `experiment(hypothesis)`,
`symbolically_test(property)`, `compare(baseline, candidate)` — are not separate
entry points. A question with the right capabilities and surface covers the
first two in practice; there is no recorded experiment object yet (step 8), and
no local contradiction record between engines (step 13).

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

Steps 1 to 7 are done, and the out-of-process entry an adapter needs exists.
What remains:

| step | gap |
| --- | --- |
| 8 | **experiment model** — backend runs produce results, not recorded experiments with prediction and environment, attached to the question that asked for them |
| 9 | the capability registry exists (`crystal.question.export()`) but is not yet exposed on the CLI with health and cost for a parent's registry to ingest |
| 13 | **contradiction handling** — if Halmos holds a property under constraints and Foundry produces a counterexample, Crystal has nowhere to record the disagreement |
| 17 | no benchmark corpus; each build measures on targets chosen for that build |
| 28 | resource metadata is partial — timeouts exist, memory and parallelism do not |

Step 8 is the next one worth doing: it is what lets a consumer reason about
*what was tried* rather than only about what was found.

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
| out-of-process entry and result envelope | 022 — `crystal ask`, `crystal-question-result/1.0`, `schemas/` |
| 8 · experiment model | next |

Steps 17 and 22 of the earlier architecture brief — isolation and the symbolic
budget — were taken out of order as Builds 017 and 018, because the first was a
credential exposure and the second was a measured misallocation. Both are
recorded in [CHANGELOG.md](../CHANGELOG.md).
