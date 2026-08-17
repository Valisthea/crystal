# Crystal v0.7.1 — Hardening

This is a hardening release, not a new detector dump.

## Quality guarantees

- deterministic candidate IDs
- candidate deduplication
- provenance for every research candidate
- confidence bounded to `[0,1]`
- explicit `validation_required`
- heuristic protocol signals are clearly marked
- duplicate oracle keyword matches collapse to one signal
- no heuristic is presented as a confirmed vulnerability

## Why this matters

Crystal's job is to compress a large protocol into a smaller set of high-value
research paths. The output must be stable and auditable so Arcadia can correlate
Crystal with other engines without treating duplicate heuristics as independent
findings.

The correct flow is:

source → semantic signal → candidate → concrete validation → finding
