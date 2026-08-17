# Crystal V1.00 Build 001 — engine rebuild

This build replaces the three bottlenecks of the v1.0.0 laboratory handoff:
the regex parser, the toy emulator, and keyword-based novelty scoring. It also
extends the tool to Rust, Move and Vyper, and makes it installable as a
first-class CLI on Windows 11.

The architecture contract is unchanged. Crystal still produces evidence and
never a confirmed finding.

---

## 1. Installation

`crystal` installs as a proper console entry point.

- `pyproject.toml` — full metadata, dynamic version, optional extras
  (`treesitter`, `solc`, `dev`, `all`), packaged corpus data.
- `install.ps1` — Windows 11 native: verifies Python ≥ 3.10, creates a `.venv`,
  installs with the tree-sitter extra, falls back to a core-only install if the
  extra fails, checks `PATH`, probes optional tools, then runs `crystal doctor`.
- `install.sh` — the same flow for Linux/macOS/WSL.
- `crystal doctor` — Python, Crystal, parser backends, `solc`, `forge`, `anvil`,
  `medusa`, `echidna`, `halmos`, `cargo`, `git`, and the detector list.
- `crystal update` — fast-forward a git checkout and reinstall.
- `crystal watch` — re-scan on change, for use during an audit.
- `crystal validate` — run an execution backend on generated harnesses.

**Windows fix worth recording:** `tree-sitter-solidity` still exposes the legacy
integer language ABI. On Windows a 64-bit pointer overflows the `unsigned long`
the modern binding expects, which makes `Language(...)` raise `OverflowError`.
Crystal rebuilds the `PyCapsule` the new API wants (`solidity_ts._capsule`), so
the stock wheels work natively without a C compiler.

## 2. Real parsers

`crystal/parsers/` — one front-end per language behind a common dispatcher.

- `solidity_ts.py` — tree-sitter Solidity. Extracts typed signatures, modifier
  chains, events, errors, enums, structs, `using for`, imports, constructors,
  `receive`/`fallback`, mapping key/value types, `constant`/`immutable`, and
  inline assembly (reported as unsupported rather than silently ignored).
- `rust_ts.py` — tree-sitter Rust with Substrate and Anchor awareness.
- `move_ts.py`, `vyper_ts.py` — tree-sitter when the grammar is installed,
  structural readers otherwise.
- `solidity_regex.py` — the v1 parser, kept as the guaranteed fallback and
  extended with typed parameters, constructors, modifiers and an approximate
  statement IR.

Everything lowers into `crystal/ir.py`, a language-neutral statement IR
(`IRStmt`, `IRCall`, `IRExpr`). Every engine downstream reads the IR, which is
why one symbolic engine serves four languages.

## 3. Symbolic engine

`crystal/symbolic/` replaces the regex delta reader.

- `algebra.py` — canonical multivariate polynomials over opaque symbols, plus a
  recursive-descent expression reader. Two semantically equal expressions
  render to the same string, so `delta(a) == delta(b)` is meaningful.
- `state.py` — symbolic state keyed by access path, deltas aggregated per state
  variable.
- `constraints.py` — observed guards with provenance, type domains, boundary
  values.
- `engine.py` — the interpreter: branch forking with a path cap, constant-bound
  loop unrolling, internal call inlining, revert-path pruning, per-step symbol
  scoping for sequences.

What it now proves rather than guesses:

```solidity
balances[msg.sender] -= a; balances[to] += a;   →  delta(balances) = 0
for (uint i = 0; i < 3; i++) totalAssets += 1;  →  delta(totalAssets) = 3
if (totalSupply == 0) …else assets*supply/assets →  two paths, one with
                                                     DIV(ARG:assets*S0:totalSupply
                                                         /S0:totalAssets)
```

Unbounded loops, inline assembly and unresolved mutations land in
`unsupported`. They are surfaced, not approximated.

## 4. Multi-language

`discovery.py` detects the language per file and profiles the project
(`Cargo.toml` markers → substrate / anchor / solana / ink / cosmwasm / near;
`foundry.toml`, `Move.toml`, `Anchor.toml`). `engine.py` routes each file to
its parser; everything after the parse layer is language agnostic.

Substrate storage calls lower into ordinary IR assignments:

```rust
TotalSupply::<T>::mutate(|t| *t += amount)   →  delta(TotalSupply) = ARG:amount
Balances::<T>::mutate(&who, |b| *b += x)     →  delta(Balances)    = ARG:x
Admin::<T>::put(new_admin)                   →  write(Admin)
```

## 5. Research engine

- `novelty.py` — structural. Delta *shapes* are normalized (`ARG:msg.value#1 -
  ARG:x#2` and `ARG:a - ARG:b` compare equal), matched against
  `corpus/known_patterns.json`, scored for rarity within the target, and tested
  for order sensitivity by re-executing the reversed sequence.
- `differential.py` — an inverse pair is now one whose *normalized deltas
  cancel*, whatever the functions are called. Name matching survives only as a
  weak corroborating signal.
- `delta_anomalies.py` — accounting pairs come from the protocol model, and both
  sides must actually be declared. A vault with no share token no longer
  reports every asset movement as an asymmetry.
- `finding_gate.py` — derives the proof checklist from evidence actually held
  and emits proof-of-concept requests when only a reproducible trace is
  missing. `economic_impact` and `minimal_trace` are never machine-set.
- `concrete.py` — substitutes typed boundary values into the symbolic
  polynomials. Every assumption is written into the trace.

### New detectors

| Detector | Mechanism |
| --- | --- |
| `reentrancy-ordering` | external call at line N, state write at M > N, weighted by whether the variable was read before the call, whether value is forwarded, and whether a guard modifier exists |
| `missing-access-control` | entry point writes authority-bearing state with no observable caller check; self-scoped writes excluded |
| `first-depositor-inflation` | zero-supply branch **and** division by supply **and** an asset-only donation path |
| `oracle-manipulation-surface` | spot vs feed price source consumed by accounting, weighted by freshness checks |

Each signal carries a line-anchored ordered trace and a falsification list.

## 6. Backends

`crystal/backends/` adds Medusa, Echidna and Halmos alongside the improved
Foundry backend. All are opt-in via `crystal validate` so `scan` stays a pure
analyzer.

Foundry now supports functions with parameters, derives constructor arguments
from the parsed signature, and handles multi-contract sequences. When a type
cannot be derived it returns `UNSUPPORTED` with the exact parameter that
blocked it. Beyond the real-EVM budget it returns `HARNESS_ONLY` with a
runnable harness attached.

## 7. Output

- Markdown: per-contract sections, detector traces, symbolic deltas, anomaly
  tables, candidate invariants, mermaid call and state-causality graphs, gate
  status.
- **SARIF 2.1.0** — pinned to `level: note` / `kind: review`.
- **Arcadia JSON** — `crystal-arcadia/2.0` hand-off.
- JSON — every v1 field preserved; v2 only adds. A regression test pins the v1
  key set.

## 8. Also rebuilt

The v1 stubs are now real: `graphs/cfg.py` (basic blocks with true/false and
loop-back edges), `graphs/callgraph.py` (IR-resolved calls, unresolved external
targets kept explicit), `graphs/storage.py` (slot/offset packing layout),
`semantics/dataflow.py` (attacker-controlled taint reaching state),
`semantics/inheritance.py` (linearization, override resolution, and attribution
of base-contract state to derived contracts), `semantics/modifiers.py` (guard
classification), `semantics/proxy.py` (delegatecall sites, EIP-1967 slots,
unguarded initializers), `quality/validation.py` (rules are now evaluated).

`crystal/process.py` centralises subprocess handling — Windows consoles return
bytes that are invalid in the ANSI code page, and `subprocess.run(text=True)`
then hands back `None` instead of a string.

## 9. Compatibility

- The 31 v1 tests pass unmodified.
- 138 tests pass in total, in three modes: default, `CRYSTAL_NO_TREESITTER=1`,
  and `CRYSTAL_NO_FOUNDRY=1`.
- The v1 JSON output contract is preserved field-for-field.
- `crystal.parser` still imports; the implementation moved to
  `crystal.parsers.solidity_regex`.
- Version stays `1.0.0` build `001`, matching the release name
  **Crystal V1.00 Build 001**.

## 10. Known limits

Recorded here rather than hidden in the code:

- Inline assembly: storage slots touched from Yul are not resolved without a
  storage layout. Reported as unsupported.
- Unbounded loops execute once and are flagged.
- Path exploration is capped at 8 paths per function; the cap is reported when
  hit.
- Internal call inlining is depth-limited to 2 and takes the first feasible
  path.
- Storage packing is per-contract; inherited layout and structs are not
  flattened.
- Move and Vyper run on structural readers unless the tree-sitter grammars are
  installed.
- Cross-contract sequences share a flat state namespace by variable name.
