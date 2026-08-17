# Crystal v0.4

This release moves Crystal from a source scanner toward a genuine research engine.

## New

### Program Graph
Crystal now builds relationships between:

- contracts
- functions
- state variables
- reads
- writes
- locally-resolvable calls
- state dependencies

### State Graph
Crystal can identify simple writer -> reader chains and produce candidate
multi-call sequences.

### Invariant Discovery
Crystal derives conservative invariant candidates from protocol state naming
and relationships. These are hypotheses, not proofs.

### Sequence Hypotheses
The engine combines state dependencies and invariant candidates to rank
multi-step attack research paths.

## Important limitation

The Solidity parser is still intentionally lightweight. V0.4 is the architecture
for the semantic engine, not a claim of complete Solidity grammar coverage.
The next step is a production-quality AST/CFG layer.
