# Changelog

## Crystal V1.00 Build 001 — engine rebuild

Full notes: [CRYSTAL_V2.0.md](CRYSTAL_V2.0.md).

### Added

- **Installation.** `pyproject.toml` with extras, `install.ps1` (Windows 11
  native), `install.sh`, `crystal doctor`, `crystal update`, `crystal watch`,
  `crystal validate`.
- **tree-sitter parsers.** Solidity and Rust front-ends with typed signatures,
  statement ordering, modifier chains, events, errors, user types and inline
  assembly awareness. Move and Vyper front-ends with structural fallbacks.
- **Language-neutral statement IR** (`crystal/ir.py`) shared by every parser.
- **Symbolic execution engine** (`crystal/symbolic/`): canonical polynomial
  algebra, expression reader, branch forking, loop unrolling, internal call
  inlining, revert-path pruning, path constraints.
- **Multi-language targets.** Solidity, Rust (Substrate / Anchor / plain), Move,
  Vyper, with automatic detection and project profiling.
- **Detectors** (`crystal/detectors/`): `reentrancy-ordering`,
  `missing-access-control`, `first-depositor-inflation`,
  `oracle-manipulation-surface`. Every signal carries a line-anchored ordered
  trace and a falsification list.
- **Backends** (`crystal/backends/`): Medusa, Echidna, Halmos — opt-in via
  `crystal validate`.
- **Output formats**: SARIF 2.1.0 and Arcadia JSON (`crystal-arcadia/2.0`),
  alongside enriched Markdown with mermaid graphs.
- **Reference corpus** (`crystal/corpus/known_patterns.json`) as the novelty
  baseline.
- 107 new tests (138 total), exercised in three modes.

### Changed

- **Novelty scoring** is structural (delta shapes, rarity, order sensitivity)
  instead of function-name matching.
- **Differential analysis** identifies inverse pairs by cancelling deltas
  instead of a hardcoded name list.
- **Foundry backend** supports functions with parameters, derives constructor
  arguments, and handles multi-contract sequences. Beyond its execution budget
  it returns `HARNESS_ONLY` with a runnable harness.
- **Finding gate** derives the proof checklist from held evidence and emits
  proof-of-concept requests. `economic_impact` and `minimal_trace` are now
  structurally never machine-set.
- **Delta anomalies** require both sides of an accounting pair to be declared.
- Former stubs implemented: CFG basic blocks, IR-resolved call graph, storage
  layout, dataflow taint, inheritance linearization and base-state attribution,
  modifier guard classification, proxy/delegatecall/EIP-1967 detection, quality
  rule evaluation.
- `crystal.parser` is now a compatibility shim over
  `crystal.parsers.solidity_regex`.

### Fixed

- `tree-sitter-solidity` legacy integer ABI overflowing `unsigned long` on
  64-bit Windows.
- `subprocess.run(text=True)` returning `None` on Windows when tool output is
  not decodable in the ANSI code page.
- Parameter splitting treating the `>` of a Solidity `mapping(a => b)` as a
  generic close.
- Regex parser missing constructors, `receive`/`fallback` and modifier
  definitions — which let the Foundry backend emit a harness with the wrong
  constructor signature.
- State-variable visibility misparsed when the declared type contained `=>`.

### Compatibility

- The 31 v1 tests pass unmodified.
- The v1 JSON output contract is preserved field-for-field; v2 only adds.
- Crystal's core still has zero runtime dependencies; tree-sitter is optional.

---

## 1.0.0 — laboratory handoff

Baseline handed to the lab: regex Solidity parser, protocol model, research
engines, finding gate with the eight-gate proof checklist, anti-finding
falsification checks, Foundry execution backend, evidence-only output contract,
31 tests.

Earlier iterations are recorded in `CRYSTAL_V0.4.md` through
`CRYSTAL_V0.9.1.md`.
