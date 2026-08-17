# Crystal ↔ Arcadia

Crystal is a standalone tool, not an Arcadia module.

## Recommended integration

```bash
crystal scan /path/to/repository --format arcadia -o crystal-handoff.json
```

Arcadia should treat Crystal as an external security engine and ingest its
structured result alongside Slither / Medusa / Echidna / Halmos.

The separation is intentional:

- Arcadia = orchestration, correlation, workflow
- Crystal = independent vulnerability research

## Formats

| Format | Flag | Consumer |
| --- | --- | --- |
| Arcadia JSON | `--format arcadia` | Arcadia / OMEGA correlation |
| SARIF 2.1.0 | `--format sarif` | IDEs, GitHub code scanning, CI |
| JSON | `--format json` | generic tooling; v1 schema preserved |
| Markdown | `--format markdown` | human review during an audit |

## Arcadia hand-off schema

`schema_version: "crystal-arcadia/2.0"`

```json
{
  "schema_version": "crystal-arcadia/2.0",
  "producer": {
    "tool": "crystal",
    "version": "1.0.0",
    "build": "001",
    "role": "evidence-only",
    "decides_severity": false,
    "decides_submission": false
  },
  "target":      { "project", "languages", "frameworks", "contracts",
                   "parsers", "parse_diagnostics" },
  "signals":     { "detectors", "delta_anomalies", "composition_candidates",
                   "unknown_behaviors", "differential_candidates" },
  "state_model": { "state_deltas", "invariant_candidates",
                   "protocol_invariants" },
  "validation":  { "concrete", "foundry", "constraints" },
  "evidence_records": [ ... ],
  "gate":        { "policy", "anti_finding_checks", "decisions", "report" },
  "quality":     { "passed", "checks", "violations", "rules" },
  "counts":      { ... }
}
```

Reject any payload whose `schema_version` you do not recognise, rather than
parsing it optimistically.

## Contract guarantees a consumer can rely on

1. `counts.confirmed_findings` is always `0`. Crystal has no code path that
   emits a confirmed finding.
2. `producer.decides_severity` and `producer.decides_submission` are always
   `false`.
3. Every SARIF result is `level: note`, `kind: review`.
4. Every detector signal has `status: "RESEARCH"` and a non-empty
   `falsification` list.
5. `UNSUPPORTED` means Crystal refused to guess; the reason is always attached.
6. `parsers` and `parse_diagnostics` state which front-end produced the result,
   so a degraded parse is never mistaken for an authoritative one.

## Reading the evidence

Each `evidence_records[]` entry carries `schema_version`, `tool`,
`tool_version`, `status`, `hypothesis`, `observations`, `state_delta`,
`constraints`, `novelty`, `known_similarity`, `limitations` and `provenance`.

`provenance.source` identifies the subsystem: `crystal-novel-behavior-engine`
or `crystal-detector:<name>`.

## Proof-of-concept requests

`gate.report.poc_requests[]` lists sequences where every machine-checkable gate
is satisfied and only a reproducible trace is missing. These are the highest
value candidates for a reviewer's time.

Generate a runnable harness for them:

```bash
crystal validate ./target --backend foundry --out-dir ./artifacts
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | scan completed |
| 1 | `doctor` found a blocking issue |
| 2 | bad invocation (missing target, unknown detector) |

A non-empty signal list never changes the exit code. Crystal reports; it does
not gate your pipeline.
