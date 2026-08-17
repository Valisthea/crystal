# Crystal Roadmap

## V0.8 — Behavioral research
- [x] behavioral relations
- [x] differential candidates
- [x] controlled mutations
- [x] impact paths
- [x] composition candidates
- [x] anti-arbitrary-composition guard
- [x] regression tests

## V0.9 — Concrete state exploration
- [x] transaction argument synthesis (typed, from the parsed signature)
- [x] Foundry execution
- [x] counterexample extraction
- [x] invariant falsification (candidate invariants only)
- [ ] actor/role synthesis
- [ ] state snapshotting against a live fork
- [ ] sequence minimization

## V1.00 Build 001 — engine rebuild
- [x] clean installation (Windows 11 native, Linux, macOS, WSL)
- [x] `crystal doctor` / `update` / `watch` / `validate`
- [x] tree-sitter Solidity front-end
- [x] tree-sitter Rust front-end (Substrate + Anchor)
- [x] Move and Vyper front-ends
- [x] language-neutral statement IR
- [x] symbolic execution engine
- [x] structural novelty scoring
- [x] reentrancy / access-control / first-depositor / oracle detectors
- [x] Foundry backend with parameters and constructors
- [x] Medusa, Echidna, Halmos backends
- [x] SARIF and Arcadia output
- [x] real CFG, call graph, storage layout, dataflow, inheritance, proxy

## Next — discovery benchmark
- [ ] historical exploit corpus with expected-signal assertions
- [ ] clean protocol corpus for false-positive measurement
- [ ] time-to-first-valid-signal metric
- [ ] signals not reproduced by baseline tools
- [ ] cross-contract state namespacing (currently flat by variable name)
- [ ] inline-assembly storage slot resolution via storage layout
- [ ] loop invariants instead of bounded unrolling
- [ ] tree-sitter Move and Vyper grammars as hard dependencies
- [ ] Anchor CPI reentrancy modelling
- [ ] incremental scan cache for `watch` on large repositories
