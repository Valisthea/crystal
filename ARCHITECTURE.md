# Crystal — Architecture Contract

## Mission

Crystal is a standalone smart-contract security research tool.

It produces structured research evidence for a higher-level system such as
Arcadia + Claude. Crystal does not replace Arcadia, does not act as an LLM
agent, and does not make submission/severity decisions.

## Explicit non-goals

Crystal must NOT:

- orchestrate other security tools;
- decide a final bug-bounty severity;
- decide whether a report should be submitted;
- write or submit a final vulnerability report;
- replace Arcadia's cross-tool correlation;
- replace Claude's protocol-level reasoning;
- claim that a local heuristic is a vulnerability;
- silently fabricate ABI arguments, roles, balances, constructor inputs, or
  protocol assumptions.

## Input → research → evidence

```text
Target source
    ↓
language detection (Solidity / Rust / Move / Vyper)
    ↓
parser front-end  →  language-neutral statement IR
    ↓
symbolic engine   →  state deltas, path constraints
    ↓
Crystal research engines + structural detectors
    ↓
candidate behavior / state deltas / novelty
    ↓
optional concrete validation
    ↓
structured evidence
    ↓
Arcadia + Claude
    ↓
global correlation / exploit reasoning / final finding
```

## Output contract

Crystal returns evidence, not conclusions.

Primary statuses:

- `RESEARCH` — exploratory signal
- `VALIDATION` — candidate requiring additional proof
- `EVIDENCE` — concrete reproducible evidence
- `UNSUPPORTED` — Crystal intentionally refused to guess
- `NO_COUNTEREXAMPLE` — validation did not reproduce the hypothesis
- `COUNTEREXAMPLE` — an execution contradicted an expected property or model
- `HARNESS_ONLY` — a runnable proof-of-concept was generated but not executed

`CONFIRMED` is intentionally not a Crystal responsibility. A concrete trace can
be strong evidence, but Arcadia + Claude must decide whether it constitutes a
real vulnerability in protocol context.

### The eight proof gates

`reachability`, `preconditions`, `state_transition`, `invariant_violation`,
`attacker_authority`, `economic_impact`, `reproducible_trace`, `minimal_trace`.

Two of them are **never set automatically**: `economic_impact` and
`minimal_trace` require protocol interpretation. `CONFIRMED` requires all
eight, and is therefore unreachable by machine. This is a structural guarantee,
not a policy that a future change might quietly relax.

### Severity mapping

SARIF output is pinned to `level: note` / `kind: review` for every result.
Crystal will not escalate a research signal into a CI failure.

## Backend boundary

Foundry/Anvil is an execution backend only. It is not an orchestration layer.
The same boundary applies to Medusa, Echidna and Halmos: Crystal generates the
harness and the properties itself, then asks the tool to execute them. Crystal
never ingests another tool's findings — cross-tool correlation is Arcadia's
responsibility, and mixing it in here would break the non-goals above.

Backends other than Foundry are opt-in through `crystal validate`, so that
`crystal scan` stays a pure analyzer.

## Parser boundary

Three front-ends, in decreasing fidelity:

1. **tree-sitter** — full syntax tree; typed signatures, statement ordering,
   modifier chains, inline assembly awareness.
2. **solc AST** — used when `solc` is available, for compiler-authoritative
   contract and function metadata.
3. **regex** — the v1 parser, retained as a guaranteed fallback.

The parser in use is always reported in the output (`parsers`,
`parser_backends`). A degraded parse is labelled as such; it is never presented
as an authoritative one.

## Research philosophy

Known weakness detectors are a baseline. The novel behavior engine searches
for interactions, state transitions, and sequences that are not represented by
the known baseline.

Novelty is prioritization evidence, never vulnerability confidence. It is
computed structurally — from the shape of symbolic state deltas, their rarity
within the target, and whether ordering changes the outcome — not from
function names.

## Zero-fabrication rule

When Crystal lacks enough information to execute a hypothesis safely, it must
return `UNSUPPORTED` rather than inventing missing information.

This extends to the symbolic model: inline assembly, unbounded loops,
unresolved storage mutations and opaque external return values are recorded in
`unsupported` and surfaced in the report. An unmodelled effect is reported as
unmodelled, never approximated into a number that looks authoritative.
