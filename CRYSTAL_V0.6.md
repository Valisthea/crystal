# Crystal v0.6

Crystal now adds a semantic layer above the lightweight source model.

## New

- compiler AST adapter through `solc --standard-json`
- compiler-model data structures
- inheritance graph
- modifier graph
- proxy/upgrade surface signals
- explicit data-flow graph
- richer machine-readable report

The AST adapter is optional and degrades cleanly when `solc` is unavailable.

## Security value

The semantic layer gives later detectors a better foundation for reasoning about:

- inherited privileges
- modifier-protected operations
- upgrade surfaces
- cross-contract state
- precise source locations
- expression-level data flow

## Next

The next release should stop inferring protocol semantics from names alone and
begin reconstructing actual asset, share, debt, oracle, fee and state-machine
relationships.
