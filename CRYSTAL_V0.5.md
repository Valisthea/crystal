# Crystal v0.5

Crystal now has the beginning of a real program-analysis stack.

## Added

- optional `solc --standard-json` backend
- CFG approximation per function
- local call graph
- explicit storage read/write graph
- richer program graph
- unified research pipeline
- expanded machine-readable report

The solc backend is optional. Crystal remains usable without it.

## Research significance

The goal is no longer to identify isolated patterns. Crystal is constructing a
model from which later versions can reason about:

- reachability
- state transitions
- data dependencies
- cross-function effects
- protocol invariants
- multi-call attack paths

## Next

The next major milestone is compiler-grade AST ingestion and precise CFG/dataflow,
followed by protocol-level asset/share/debt/oracle models.
