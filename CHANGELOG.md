# Changelog

## Crystal V1.00 Build 003 — neither module is wrong on its own

Build 002 closed Signal 1 of the F5 benchmark and explicitly did not claim
Signals 2 and 3. This build closes both, and the reason they were hard is the
same reason they matter: **the defect is not inside any one file.**

**Signal 2 — a guard one stage enforces and another never consults.**

    TxExtension = ( .., ReversibleTransactionExtension, .., ChargeTransactionPayment, .. )
                       index 7: rejects protected accounts
                                                    index 9: debits the signer

Both stages run on every transaction. The guard covers the call it inspects; the
payment stage moves value on a different path and never asks. Read either module
alone and it is correct. Crystal now extracts the runtime wiring (`pub type X =
(..)` tuples, `construct_runtime!`), resolves each stage to parsed code,
classifies it as guard / value-mover / unresolved, and reports the pair. Stages
that resolve to nothing are named as unresolved rather than assumed benign.

The index is the ordering claim, so getting it right mattered: comments are
named children of a tuple type, and counting them put `ChargeTransactionPayment`
at 13 instead of 9.

**Signal 3 — a settlement that is handed the outcome and drops it.** Visible in
the signature alone:

    fn post_dispatch_details(.., _result: &DispatchResult) -> .. {
        let actual_fee_with_tip = compute_actual_fee(len, info, &post_info, tip);
        T::OnChargeTransaction::correct_and_deposit_fee(.., actual_fee_with_tip, tip, ..)
    }

`_result` carries whether the dispatch succeeded; the underscore is Rust for
"deliberately ignored". The tip is re-added and charged on every path, including
the one where the user got nothing. The detector fires only when the outcome was
*received and discarded* — a frame that never gets the outcome is not making
that mistake and is not flagged. Restoring the parameter and refunding on
`is_err()` clears the signal, pinned by a test.

**Precision: 18 signals down to 10 on the same scope, with all three kept.**
Adding detectors without this would have buried the result. Three
name-and-shape confusions were doing the damage:

- `pallet_balances::Call::<T>::transfer_keep_alive { .. }.into()` was read as an
  external call. It **constructs** a dispatchable; it executes nothing. Every
  call-construction looked re-entrant.
- `DispatchTime::At(..)` and `Ok(..)` were read as calls. Rust says otherwise by
  convention: functions are snake_case, types and variants are CamelCase.
- `count_transfers` matched "transfer" as a substring, which turned an
  event-scanning extension into a false bypass. Value verbs now match whole name
  segments.

Also reclassified as read-only: `T::Lookup::unlookup`, `T::Hashing::hash_of`,
and the `is_`/`can_`/`saturating_` families. What survives on
`reversible-transfers` is four genuine "value operation before state write"
sites (`hold`, `release`, `schedule_named`, `bound`).

**Not claimed.** Stage classification is structural, not semantic: Crystal
reports that one stage checks a restriction and another moves value without it.
Whether the guard *should* have covered that path is a protocol question, which
is why the falsification list leads with it. Cross-module composition is
implemented for declared pipelines only — Config-trait associated types are
still unresolved, so `T::OnChargeTransaction` does not yet route to
`FungibleAdapter`.

Tests: 161 passing (+11 composition regressions).

## Crystal V1.00 Build 002 — the tip nobody bounded

Build 001 was measured against a confirmed Critical it had never seen: **F5,
uncapped transaction tip drains a high-security account past the guardian**, in
`pallet-transaction-payment` (Substrate/Rust, Quantus). It parsed the pallet
cleanly — 7 files, 20 types, 59 functions with IR — and reported nothing that
mattered. Two detector signals, both `missing-access-control` on `ExtBuilder`,
a test helper. Two state deltas, both on the same test helper.

Three defects, each fixed and each pinned by a regression test.

**The production surface was invisible because fixtures crowded it out.** Every
delta Crystal produced came from `tests.rs`. A mock runtime mutates state and
skips authority checks *by design*, so every detector fires on it. Fixtures are
now classified — by path, by `#[cfg(test)]`, by `mod tests`, and for Solidity by
`*.t.sol` and `Test`/`DSTest` inheritance — and excluded from research, not just
from detectors. They are still parsed and still reported, under
`excluded_test_contracts`, because silently dropping code is its own defect.
`--include-tests` restores the old behaviour.

The classifier was itself wrong on the first pass: matching `bench` as a
substring caught `cfg(feature = "runtime-benchmarks")` on `FungibleAdapter`, a
production adapter, and one benchmark-gated method marked the entire type a
fixture. Markers are now anchored on the whole attribute, and a type is a
fixture only when *everything* in it is.

**The tip was invisible because three parsing layers each dropped it.**
`ChargeTransactionPayment` is a tuple struct — `(#[codec(compact)] BalanceOf<T>)`
— and its single field, addressed as `self.0`, is the tip. It was extracted as
nothing: tuple structs use `ordered_field_declaration_list`, which the parser
did not read. Then `self.0` lost its field, because the expression reader only
continued a path on `.name`, never `.0`. Then the call that spends it,
`…::withdraw_fee(who, call, info, fee_with_tip, tip).map(|li| …)`, was recorded
as a call to **`map`** — the chain was classified by its last segment. And
`compute_fee(len as u32, info, tip)` lost two of its three arguments, because
the `as` cast stopped the argument loop.

All four are fixed: tuple fields, `.0` paths, Result/Option combinator
unwrapping, `?` propagation, `as` casts, and `::<T>::` turbofish paths. An
unparsed construct in an argument list now skips to the next separator instead
of truncating the rest.

**A type the runtime decodes from the transaction is attacker input.** This is
not a heuristic: `TransactionExtension` means "decoded from the transaction", so
every field of an implementor is chosen by whoever signed it. Such fields now
resolve to `ARG:` symbols rather than protocol state, which is what makes the
taint visible at all.

**New detector: `unbounded-input-in-value-op`.** A caller-chosen value reaching
the amount position of a value-bearing operation with no observed upper bound.
It reports the shape that matters and not the shape that doesn't: a plain
`transfer(to, amount)` debited from the caller's own balance is deliberately
**not** reported, or the real signal drowns. It fires when the value is a
decoded field, or when it is one term of a larger total it can dominate. It also
cites *sibling caps* — the same type explicitly capping other inputs is the
codebase saying it knows the difference. `saturating_*` is explicitly not read
as a bound: it stops the addition from overflowing, it does not cap the operand.

On F5 it now produces, at 0.82:

    ChargeTransactionPayment.withdraw_fee     lib.rs:826
      withdraw_fee(ARG:who, ARG:call, ARG:info, ARG:fee_with_tip, ARG:self.0)
      arg4 `tip` = ARG:self.0 (decoded from the transaction)
      no upper-bound guard observed on this path
      the same type caps other inputs explicitly: get_priority:881 …max(Weight…)

which is the benchmark's Signal 1. The `ExtBuilder` false positives are gone.
Adding `ensure!(tip <= T::MaxTip::get(), …)` clears the signal, so the detector
reacts to the guard and not to the function name — pinned by a test.

**Not claimed.** Signals 2 and 3 of the benchmark are still missed: cross-pallet
composition through the `TxExtension` tuple, and the refund-path analysis
showing the tip is never returned on a failed dispatch. Cross-module composition
is not implemented. This build closes the input-taint gap, nothing wider.

Tests: 150 passing (31 v1 + 107 v2 + 12 new F5 regressions).

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
