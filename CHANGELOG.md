# Changelog

## Crystal V1.00 Build 011 — the chain that started where nobody could enter

Build 010's cross-contract call edges anchored on whichever function contained
the call. That is wrong whenever the call sits in an internal helper, which in
Solidity is most of the time:

```
contract PegOut {
    function refundPegOut(...) external { _transfer(who, amount); }
    function _transfer(...) internal { _collateralManagement.slash(...); }
}
```

Build 010 reported the chain `PegOut._transfer -> Collateral.slash`. `_transfer`
is internal: no caller can invoke it, so the chain is not actionable. And the
chain that *is* actionable — through `refundPegOut` — was never emitted at all,
because `refundPegOut` makes no external call of its own. The one shape the
feature was built for produced an unreachable candidate and hid the reachable
one behind it.

External calls are now attributed to the entry points that reach them, walking
internal calls within the contract to a bounded depth. The helper is named in
the edge, so the trace stays honest about where the call actually is:

```
PegOutContract.refundPegOut -> CollateralManagement.slashPegOutCollateral
  via _transfer -> _collateralManagement.slashPegOutCollateral
```

Found by exercising the Build 010 artifact rather than the working tree.

Tests: 256 passing (+3).

## Crystal V1.00 Build 010 — the pack that loaded, and was never asked

Five defects from live use. Four of them shared a failure mode: a mechanism
that worked, wired to nothing, reporting a clean result.

**A campaign pack could not reach the analysis path.** `discover_packs()`
imported six hardcoded `crystal.packs.*` modules and nothing else. An operator's
pack lives beside their engagement, so its dotted name resolves nowhere, and the
`extra_dirs` branch built `crystal.packs.{stem}` — reachable only for files
already inside Crystal's own package. `scan` never passed `extra_dirs` and had
no flag to. So a pack that loaded fine under `campaigns_for_pack()` changed scan
output by not one byte. Packs now load from a filesystem path as well as a
module, `scan --pack` is repeatable, and what each requested pack contributed is
reported — a pack that loaded zero campaigns is no longer indistinguishable from
one that loaded and stayed quiet.

**Two thirds of the scope API was dead.** `accepts_function` and
`accepts_category` were defined, tested, and never called; only
`accepts_contract` was. Categories now fall back to classifying the delta's own
changed state when the causal graph supplies none, so `allowed_categories`
applies to every candidate rather than only to sequences the graph happened to
connect. `allowed_state` is enforced too. Every rejection is attributed:
`pruned_by` says which rule rejected how many sequences, and it is rendered.

**Campaign questions went nowhere.** `CampaignDefinition.questions` was read by
no code at all. Questions now reach the candidate and the evidence, next to the
invariant associations that were already there.

**Campaigns were invisible in Markdown.** They were serialised into JSON and the
Arcadia handoff and omitted from the Markdown writer entirely. There is now a
Campaigns section listing every campaign that ran — including the quiet ones and
why they pruned.

**Composition reported zero on a protocol that is only composition.** The causal
graph linked functions that share *storage*. A protocol split across contracts
shares none: it composes by calling through an interface-typed handle, and the
callee is declared in an interface with no body. Five contracts calling each
other constantly produced an empty graph, and every engine that walks it —
sequence generation, composition candidates, order sensitivity, campaigns —
correctly reported nothing about it. Crystal now binds a declared type to the
contracts implementing it and emits `call-flow` edges across the boundary.
Binding is by declared type, never by bare function name: two contracts can both
define `settle` without being the same `settle`. `compose()` also no longer
requires an accounting/oracle/fee invariant to exist — a chain that crosses a
contract boundary and moves the callee's state is a composition candidate on
structural grounds.

**`asymmetric-side-effect` could not report the minimal asymmetry.** A companion
needed two witnesses, so two sibling entry points — one guarded, one not — could
never be reported: the guarded one is a single witness. Worse, guards were not
companions at all; only calls were. The detector now treats an authorization
guard as the side-effect it is, admits the two-site case, and names the guard
the other path has rather than only counting it.

**`rglob("*.sol")` matched Foundry directories in six more places.** Build 008
guarded `compiler/solc.py`. The same shape was live in `semantics/solc_ast.py`,
`research/foundry.py`, `discovery.py` (twice), `composition/runtime_wiring.py`
and `campaigns/registry.py`, so a whole-repository scan of any Foundry project
with deployment history still died. All seven now go through `crystal/paths.py`;
the convention is a function.

**`crystal campaign list` crashed before printing.** The `list` subparser never
declared `--pack` while the handler read `args.pack` unconditionally. `list`
takes `--pack` now, and the read is defensive.

Tests: 253 passing (+21).

## Crystal V1.00 Build 009 — two contracts, one variable, no way to tell

Crystal identified every state variable by its bare name. `balances` was
`balances`, whichever contract declared it. On a single-contract target that is
harmless; on a protocol — which is the only kind of target Crystal claims to be
built for — it was wrong in three directions at once.

Two contracts, each with a `balances`, and the sequence `Alpha.credit ->
Beta.debit`:

```
deltas  = {'balances': 'ARG:a#1 - ARG:a#2'}
edges   = [('Alpha.credit','Beta.debit',('balances',)),
           ('Beta.debit','Alpha.credit',('balances',))]
nodes   = [('balances', 'Beta')]
```

**The delta was a false conservation law.** One shared `SymbolicState` keyed on
bare names, so two unrelated contracts each moving money netted to a single
expression that reads as "this sequence conserves `balances`". The one shape
Crystal exists to notice — an asymmetry in accounting — was being cancelled out
by a spelling coincidence.

**The causal edges were invented.** `build_state_graph` intersected every
function's reads and writes across the whole project, so any two contracts
sharing a variable name got an edge in both directions. Those edges feed
sequence generation, campaigns and order-sensitivity, so a name collision
manufactured research candidates downstream.

**A state node silently overwrote its namesake.** `sv_info[sv.name]` kept
whichever contract was parsed last; Alpha's `balances` node did not exist.

State identity is now the qualified `Alpha::balances`, applied where state is
actually shared: the symbolic engine namespaces `execute_sequence` (a single
function's effect never spans contracts, so it keeps bare names), and the state
graph namespaces transitions, nodes and edges. Inheritance resolves to the
declaring contract, so `Child.balances` and `Parent.balances` stay one slot and
no edge crossing the inheritance boundary is lost. A name declared nowhere in
the scanned set stays with its accessor rather than merging two contracts that
happen to share an unscanned base.

Identity is qualified; meaning is bare. Everything asking what a variable *is*
rather than which one it is — category classification in the state graph, the
differential engine, the invariant engine and novelty scoring, plus campaign
invariant association — reads through `bare_name` first, so no classifier
changed its answer.

Accounting pairs now bind inside one contract. `Vault::totalAssets` against
`Registry::totalSupply` was never a relation, only two protocols sharing a
vocabulary; each side is compared against its own contract's counterpart.

Reports gain the information as a side effect: `delta(Vault::totalAssets)`
names the slot, where `delta(balances)` on a twelve-contract protocol named
nothing.

Tests: 232 passing (+19, of which 18 are the new namespace suite; each pins one
of the failure modes above).

## Crystal V1.00 Build 008 — the directory that pretended to be Solidity

Two regressions found in live use, both invisible to the existing suite.

**`compile_standard` crashed on Foundry broadcast directories.**
`rglob("*.sol")` matches directories whose name ends in `.sol` — Foundry's
`broadcast/Deploy.s.sol/` is a directory containing `run-latest.json`, not a
Solidity file. `read_text()` on a directory raises `IsADirectoryError` on
Unix and `PermissionError` on Windows, crashing the entire solc pass. Fixed
with a `p.is_file()` guard before the read. Covered by a test that creates
the Foundry broadcast layout.

**`asymmetric-side-effect` was blind to Solidity.**  The callee extraction
used `rsplit("::", 1)` (Rust module paths) but Solidity uses `.` as a member
separator, so `token.transfer` never reduced to `transfer`. Additionally,
`VALUE_OPERATIONS` was missing Solidity's underscore-prefixed internal
functions (`_mint`, `_burn`, `_transfer`, `_safeMint`, `_safeTransfer`) and
common ERC-20 variants (`transferFrom`, `safeTransfer`, `safeTransferFrom`).
A new `_leaf_name()` helper now handles both `::` and `.` separators.
Covered by a Solidity fixture with a manifest asymmetry: three functions
call `_mint`, two of which also call `_updateCheckpoint`, one does not.

Tests: 213 passing (+2 regression tests).

## Crystal V1.00 Build 007 — two detectors that could only ever report nothing

Both of Build 002–004's composition detectors were found unable to report, on
the very target they were calibrated against. Neither failure was visible from
the test suite, because both lived past the point the suite stopped looking.

**`asymmetric-side-effect` raised on its own emit path.** `signal()` anchors on
a function-like object and reads `.contract` / `.name` / `.path` / `.language`
off it; the detector passed those as four separate string keyword arguments. So
the first asymmetry it ever found raised `TypeError` instead of reporting it,
and the detector had therefore never emitted a signal on any target in its life.
The suite asserted the detector's NAME in the registry list and nothing else,
which is exactly how a detector that cannot emit ships green. Fixed with the
`_Anchor` adapter the sibling detector already used, and covered by a fixture in
the shape the detector exists to find: four settlement paths, three of which
record a commitment leaf beside the credit.

**Composition reported zero when it had not read the stages.** A stage's role is
classified from its own body, so a scan root holding the extension tuple but not
the crate that defines a stage leaves that stage `UNRESOLVED`, and no crossing
can ever be built from it. The model said nothing about this. Scanning
`pallets/` — the natural choice on a Substrate workspace — returned
`detector_signals=0` for a pipeline that does contain a live crossing, and a
zero there is indistinguishable from a clean result. `build_composition` now
names the unread stages, and only for pipelines that produced no crossing: once
one is out the operator already has the signal and the note would be noise.

Both fixes are held by regression tests in the direction that matters and in the
counter-direction: the incomplete scan must speak, the complete scan must stay
quiet. 211 tests.

## Crystal V1.00 Build 006 — Crystal becomes Arcadia's microscope

Build 005 gave Crystal a composition engine and eight structural detectors. This
build transforms Crystal from a detection/analysis engine into a **structural
research engine** oriented around state transitions, campaign-scoped analysis,
and evidence generation for Arcadia.

**Campaign system.** Crystal now runs scoped, gated analyses instead of "scan
everything". A campaign defines what to look at (scope), what transitions are
dangerous (transitions), what should hold (invariants), and what to ask
(questions). Everything outside the campaign is deferred, not analysed. The
architecture enforces: SCOPE → CAMPAIGN → TARGET → STATE TRANSITIONS → TOP
CANDIDATES → VALIDATION → ARCADIA.

**Protocol invariant packs.** Six built-in packs (generic, defi, registry,
authorization, migration, economic) provide 15 campaign definitions covering
ownership transitions, role revocation, nonce replay, temporal boundaries,
balance accounting, oracle settlement, share inflation, permit funding, resolver
transitions, approval authority, stale authorization, revocation, permission
preservation, quote settlement, and allowance mismatch. Packs are loaded via
`importlib` — true plugins, not hardcoded.

**ENS preset.** Seven campaigns (A1–A3, B1–B4) covering the ENS competition
surface: migration × fuse × roles, transfer × resolver × roles, HCA × session ×
nonce, registration × payment × oracle, commit × reveal × price, permit ×
funding × settlement, expiry × premium. The campaigns use generic concepts
(owner, resolver, nonce, expiry) — they do not hardcode ENS contract names.

**Enriched causal state graph.** Edges now carry `edge_kind` (ownership-action,
role-action, nonce-auth, temporal-action, balance-transfer, etc.), semantic
`categories`, source/target contract paths and lines, `key_relation`, and
`condition`. State nodes have semantic category classification (ownership, role,
nonce, temporal, balance, registry, proxy, storage). The graph drives
campaign-aware sequence scoring.

**Order-sensitivity engine.** Detects when function ordering changes the final
security state. Tests permutations of sequences sharing causal state, producing
`OrderSensitiveResult` with the two sequences, differing state, affected
storage, confidence, and replay instructions.

**Boundary engine.** Auto-proposes x−1, x, x+1 test cases for numeric and
temporal comparisons found in the source. Useful for detecting off-by-one errors
at expiry boundaries and premium calculations.

**Asymmetric side-effect detector (8th detector).** Finds value operations where
companion side-effects are missing in some code paths — the F3 benchmark
pattern. Fires when a value operation (deposit, mint, transfer, slash, etc.) has
a companion function present in ≥2 call sites but absent in at least one.

**Top-K multi-factor scoring.** Candidates are scored by novelty (0.20) ×
state_delta_significance (0.25) × causal_depth (0.15) ×
authorization_relevance (0.15) × order_sensitivity (0.10) ×
exploitability (0.15), with penalties for duplicate, known, and low-confidence
candidates. Global top-K across campaigns with deduplication by invariant +
delta shape.

**CLI additions.** `crystal campaign list` shows registered packs and campaigns.
`crystal campaign run <id> <target>` runs a single campaign against a project
and prints candidates with scores, hypotheses, and evidence.

**Arcadia output enriched.** The `crystal-arcadia/2.0` hand-off now includes a
`campaigns` field carrying per-campaign results with candidates, scores,
evidence, and state deltas.

**Not changed.** The seven existing detectors, symbolic engine, composition
system, parsers, and backends are untouched. The v1 JSON contract is preserved.
Crystal still never produces a confirmed finding.

Tests: 208 passing (+20 covering campaigns, packs, scoring, boundary engine,
order sensitivity, CLI parsing, and report enrichment).

## Crystal V1.00 Build 005 — CheckNonce rejects all day and guards nothing

Composition landed in Build 003 and worked on the F5 shape. This build makes it
survive a real workspace, where the thing that breaks a composition detector is
not missing the true pair — it is reporting eleven false ones.

**`crystal/composition/`** now holds the analysis: `runtime_wiring` (workspace
profile, pallet topology), `pipeline_extractor`, `stage_classifier`,
`boundary_detector`, `config_resolver`. `semantics/modules.py` became an
adapter over it, because two classifiers eventually disagree about what a guard
is and only one of them is wired to the detector.

**The discriminator is authority, not rejection.** `CheckNonce` rejects
constantly and guards nothing: it compares a counter. `CheckWeight` rejects on
resource limits. Classifying either as a guard makes every pipeline report a
bypass and buries the real one. Stages are now one of GUARD / MOVES-VALUE /
CHECKS-ONLY / OBSERVES, and a stage is a GUARD only when its rejection consults
*restriction state about a principal* — not when it merely rejects. The
CHECKS-ONLY stages are listed in the evidence, so the report says why they were
not treated as guards instead of silently omitting them.

Coverage is decided by mechanism family, which is what makes the negative case
work: a guard on transfers covering a transfer is the system working and stays
silent. A guard on call dispatch does not cover a fee, and that is F5.

**Both runtime macro formats.** Quantus uses `#[frame_support::runtime]` with
`#[runtime::pallet_index(N)]`; `construct_runtime!` is still everywhere in older
trees. Supporting one gave an empty topology on half of real targets. Mock
runtimes are excluded — `mock.rs` declares its own indices, and mixing it in
gave 27 pallets for 18 real ones, with two aliases per index.

**The warning the spec asks for.** Scanning a single pallet cannot show
composition, so Crystal says so on stderr and in the JSON rather than reporting
zero crossings as if that were a result. Substrate is now detected from sources
as well as manifests, so a copied `src/` tree still warns.

On the Quantus scope, at **0.85**:

    [7] ReversibleTransactionExtension GUARD       — gates call-dispatch
    [8] WormholeProofRecorderExtension OBSERVES
    [9] ChargeTransactionPayment       MOVES-VALUE — debits the signer via fees
        -> OnChargeTransaction::withdraw_fee -> FungibleAdapter
    boundary: stage 9 debits the signer through fees, which stage 7 does not
              cover: it gates call-dispatch

`CheckNonce` and `CheckWeight`, both parsed and both rejecting, produce nothing.
The reported model and the signal now carry the same confidence — they had
diverged, 0.74 against 0.85, because only the detector counted the unbounded tip.

**Not claimed.** Coverage is decided on mechanism families, which is a
structural proxy: a guard could cover a mechanism through a path Crystal cannot
follow, and the falsification list leads with that. Only declared composition is
visible, and every signal now carries that limit in its own evidence.

Tests: 183 passing (+17 covering the seven criteria, including the two negative
cases that matter — format checks and a guard that does cover the mechanism).

## Crystal V1.00 Build 004 — where the debit actually happens

Build 003 shipped cross-module composition and named its own limit: Config-trait
associated types were unresolved, so `T::OnChargeTransaction::withdraw_fee(..)`
pointed nowhere and the debit appeared to leave the analysed code. That limit is
now closed.

A Substrate pallet declares `type OnChargeTransaction` and the runtime binds it,
in a different crate:

    impl pallet_transaction_payment::Config for Runtime {
        type OnChargeTransaction =
            FungibleAdapter<Balances, pallet_mining_rewards::TransactionFeesCollector<Runtime>>;
    }

That `impl ..::Config for Runtime` block is the only place the binding exists.
Crystal now extracts every associated type from it (248 bindings on the Quantus
runtime) and resolves `T::X` at the call site, which turns three dead-end calls
into a routed debit path:

    ChargeTransactionPayment -> FungibleAdapter   OnChargeTransaction::withdraw_fee
    ChargeTransactionPayment -> FungibleAdapter   OnChargeTransaction::can_withdraw_fee
    ChargeTransactionPayment -> FungibleAdapter   OnChargeTransaction::correct_and_deposit_fee

A pipeline stage now **inherits what its bound implementation does**. If
`FungibleAdapter` had consulted the high-security whitelist, the bypass signal
would clear; it does not, so Signal 2 gains that as evidence and rises 0.72 to
0.78. Routes that land on unparsed code are dropped rather than reported: an
unresolved target says nothing about what the operation does.

**Structural fix: a shared vocabulary module.** `semantics.modules` imported
`detectors.base`, and `detectors/__init__` imports `pipeline_bypass`, which
imports `semantics.modules` — a cycle that only stayed hidden because the test
suite happened to import the detectors package first. `crystal/vocabulary.py`
now holds the security vocabulary both layers need and imports nothing from
Crystal. It also stops the two layers from drifting into disagreeing about what
counts as a guard.

Tests: 166 passing (+5 for binding extraction, resolution, routed edges, and the
unresolved case).

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
