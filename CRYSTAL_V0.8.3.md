# Crystal v0.8.3 — Zero-False-Positive Finding Gate

Crystal is intentionally aggressive internally and conservative externally.

## Hard rule

A heuristic, state delta, composition, or invariant anomaly can never be
reported as a confirmed vulnerability by itself.

A `CONFIRMED` finding requires all eight proof gates:

1. reachability
2. satisfiable preconditions
3. demonstrated state transition
4. demonstrated invariant violation
5. attacker authority
6. measurable economic/security impact
7. reproducible trace
8. minimal trace

If any gate is missing, the candidate is not `CONFIRMED`.

## Anti-finding engine

Every research candidate is accompanied by falsification questions. The engine
must actively try to disprove the hypothesis before promotion.

## Design objective

Search broadly internally.

Report narrowly externally.

The target is zero false positives in the `CONFIRMED` output, even if that
means returning fewer findings.
