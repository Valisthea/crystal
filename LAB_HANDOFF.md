# Crystal — Laboratory Handoff

## What Crystal is

A standalone smart-contract security research engine used as a component by
Arcadia and Claude.

## What Crystal is not

Crystal is not an autonomous auditor, not an LLM, not an orchestrator, and not
a report-submission system.

## Research focus

The central research novelty is the discovery of unknown behavior:

- unusual state interactions;
- novel transaction sequences;
- state-delta asymmetries;
- cross-component interactions;
- micro-anomalies;
- causal compositions of individually benign operations.

Known vulnerabilities are used as a baseline and regression corpus, not as the
ceiling of the search space. That baseline now lives in
`crystal/corpus/known_patterns.json` and is matched structurally — against the
shape of symbolic state deltas — rather than against function names.

## Evidence discipline

Crystal deliberately separates:

`signal → hypothesis → validation → evidence`

from:

`protocol interpretation → exploit chain → severity → final finding`

The second chain belongs to Arcadia + Claude.

## Zero-false-positive philosophy

Crystal must never fabricate missing execution inputs. It must prefer
`UNSUPPORTED` to a guessed result.

Concrete execution is evidence generation. It does not automatically constitute
a vulnerability.

Two of the eight proof gates — `economic_impact` and `minimal_trace` — are
never set automatically, which makes `CONFIRMED` unreachable by machine. This
is structural, not a policy.

## Reading a result in the lab

1. `summary.confirmed_findings` is always `0`. Do not look for findings.
2. Start at `detectors[]`: each entry has an ordered, line-anchored trace and a
   `falsification` list. Try to falsify before you investigate.
3. `delta_anomalies[]` shows where accounting stops being symmetric.
4. `finding_gate.report.poc_requests[]` lists the candidates where only a
   reproducible trace is missing — the best use of reviewer time.
5. `state_deltas[].unsupported` and `parsers` tell you where Crystal's model is
   thin. Treat those areas as unanalyzed, not as clean.

## Running it

```bash
crystal doctor                                  # environment readiness
crystal scan ./target --format markdown         # human review
crystal scan ./target --format arcadia -o handoff.json
crystal watch ./target                          # during an audit
crystal validate ./target --backend foundry --out-dir ./artifacts
```

Degraded modes are explicit: without tree-sitter the regex parsers are used and
reported; without `solc` the AST pass is skipped; without `forge` the harness is
generated but not executed.

## Integration recommendation

Arcadia should consume Crystal's structured JSON/JSONL evidence and combine it
with results from Slither, Medusa, Echidna and other research tools. Claude can
then correlate independent evidence, reason about exploitability and determine
whether a reportable vulnerability actually exists.

## Versioning

This package is a laboratory handoff build. Every evidence record includes a
schema version and provenance so downstream systems can reject incompatible
formats rather than silently misinterpreting results. The Arcadia hand-off is
versioned separately as `crystal-arcadia/2.0`.

Known limits are recorded in [CRYSTAL_V2.0.md](CRYSTAL_V2.0.md) §10 rather than
left implicit in the code.
