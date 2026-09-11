# Changelog

## Crystal V1.00 Build 018 — the budget that was spent alphabetically

Second step of the plan in [docs/EVOLUTION_ASSESSMENT.md](docs/EVOLUTION_ASSESSMENT.md).
That assessment described the waste wrongly, and finding out how is what this
build is: it said 15 campaigns each burned a 150-sequence slice. They do not.
Every campaign re-reads the *same* 150 state deltas and prunes. The runner has
no budget to allocate.

The budget is one step earlier and it is real: `derive_state_deltas` executes
`sequence_hypotheses[:150]`. On the Lido stonks protocol that is 150 of 198, and
the other 48 were discarded with no line of output anywhere.

### The measurement that decided the design

On stonks, **175 of the 198 hypotheses carry the same score, 0.720**. The budget
boundary therefore falls deep inside a tie, and what decided between executing a
sequence and discarding it was the fallback tiebreak — sequence length, then
lexicographic order.

```
[0.720] Stonks.constructor -> Order.initialize -> Order.isValidSignature   (dropped)
```

That is the function the protocol's own Wake harness spends four hundred draws
on, never symbolically executed, discarded because `S` sorts late. **22 of the
48 discarded sequences already carried a detector signal on their path.**

### What changed

New `crystal/scheduling/`. Scores are untouched and remain the primary key: a
hypothesis the generator ranked higher is still executed first, because
re-ranking on anything else would be scoring the ranking twice. Only the
tiebreak changed — which on a real target is where the decision lives.

Three signals, each structural, all free because the pipeline has already
produced them by the time sequences are ranked:

| signal | reads |
| --- | --- |
| `signalled` | a detector already fired on a function in the chain |
| `cross_contract` | contract identity — more than one contract spanned |
| `mutating` | some function writes state; a chain of pure getters cannot produce a delta |

A fourth was built, measured and removed: whether an enabled campaign's
`allowed_categories` accepted the state written. True for 95% of hypotheses, its
accepted set covered every category `classify_state` can return, so it was
`mutating` under another name — and it reached that answer through substring
matches on state-variable names. A signal that does not discriminate is not
worth a nominal dependency, and the constraint against deciding by spelling is
the point of this build.

`discover_packs` moved ahead of `run_research`, since the registry has to exist
before the budget is spent. Loading a pack reads no research output.

### Measured, two protocols, budget unchanged at 150

| | stonks | Flyover bridge |
| --- | ---: | ---: |
| hypotheses generated | 198 | 250 |
| executed | 150 | 150 |
| deferred | 48 | 100 |
| **deferred that carried evidence — 017** | 22 (46%) | 37 (37%) |
| **deferred that carried evidence — 018** | **4 (8%)** | **2 (2%)** |
| sequences swapped in/out | 18 | 48 |
| distinct `(kind, state)` findings lost | none | none |

`Stonks.constructor -> Order.initialize -> Order.isValidSignature` is now
executed.

### What is not the result

The anomaly count on stonks went from 30 to 34. **That is not progress and is
not claimed as it.** All 34 are the same single finding —
`unresolved-effect on Stonks::RECEIVER` — reached by more paths. In distinct
`(kind, state)` findings it is 1 before and 1 after.

Three anomalies present under 017 are absent under 018, and all three are
`Stonks.constructor -> Stonks.<getter>`: single-contract two-step chains ending
in a pure getter, demand 0.4. They were displaced by cross-contract chains
carrying a detector signal, demand 1.0, reporting the same anomaly on the same
state slot. With a fixed budget something must be dropped; what changed is that
it is now dropped for a stated reason instead of by alphabetical order, and the
drop is reported.

### Nothing is discarded in silence any more

`sequence_budget` travels in the scan payload and the Arcadia hand-off: budget,
considered, executed, deferred, the boundary score, **how wide the tie at the
boundary was**, and per-hypothesis detail with the reason. A reader deciding
whether to trust the cut needs the tie width — when it is 175 of 198, the score
is not ranking and the tiebreak is.

The report also states that raising the budget is not the intended fix, where
somebody would reach for it. Throughput is the axis Crystal does not compete on,
and the budget was deliberately left at 150.

### Tests

`tests/test_v300_scheduling.py` — 15 tests. Two are invariants and the named CI
gate grows from 12 to 14:

* **the allocation does not change when everything is renamed.** Two structurally
  identical projects, names reversed alphabetically; the executed set must
  translate exactly. Under the old tiebreak it would not have.
* what the budget did not reach is reported with a reason.

Also pinned: demand never promotes a sequence past a better-scored one, a tie
demand cannot separate stays deterministic, and repeated signals on one path are
not double counted.

523 collected. 521 passed, 2 xfailed under tree-sitter; 498 passed, 24 skipped,
1 xfailed under `CRYSTAL_NO_TREESITTER=1`.

### What this does not do

It does not make the score discriminate. 175 of 198 hypotheses sharing one value
is the real weakness; Build 018 stops that weakness being resolved by spelling,
but a ranking that actually separated them would be better than a good tiebreak.
That is the `generate_sequences` scoring model, and it is not touched here.

It also does not schedule anything else. Campaigns still all run, detectors
still all run, and the choice between static reasoning and executable validation
is still the operator's. Only the one budget that was already being rationed is
now rationed for a reason.

## Crystal V1.00 Build 017 — what the target's tooling could read

Section 22 of the architecture brief requires that analysed code run without
reach into host credentials. The first step of the evolution plan
([docs/EVOLUTION_ASSESSMENT.md](docs/EVOLUTION_ASSESSMENT.md)) put isolation
ahead of the scheduler because it is the only gap on that list that can hurt
the operator rather than the results.

Measured on this checkout, 2026-09-11, by running a real child process and
reading what it received:

```
before:  PRIVATE_KEY= 0xdeadbeef_operator_key | ETH_RPC_URL= https://rpc.example/KEYMATERIAL
after :  PRIVATE_KEY= None                    | ETH_RPC_URL= None                    | PATH set= True
```

This was not theoretical. Foundry hands a Solidity harness
`vm.envUint("PRIVATE_KEY")` and `vm.envString(...)`, and that harness is
compiled from a contract Crystal did not write. Every backend run put the
operator's deploy key, RPC tokens and cloud credentials inside reach of the
target's own code.

### What changed

New `crystal/isolation/`. `process.run` now builds a child environment by
**allowlist** and no longer inherits `os.environ`.

An allowlist, not a denylist, and the choice is not stylistic: a denylist of
`PRIVATE_KEY`, `*_TOKEN`, `AWS_*` is a list of the secrets somebody thought of,
and the one that leaks is the one nobody named. An allowlist fails the other
way — a toolchain that needs something unforeseen stops working loudly, and
`CRYSTAL_PASS_ENV=NAME` opts it back in, recorded in the isolation report so
the decision stays visible.

| | policy |
| --- | --- |
| environment | allowlist; on one developer machine, 25 names passed and 83 withheld |
| filesystem | already correct — a disposable working directory per execution |
| process | **not sandboxed**, and now says so |
| operator code | `--pack`/`--fixture` only from a command-line path, never discovered in the target |

`inherit_environment=True` exists for one call site: `pip install -e .`, which
acts on the operator's own checkout, where a proxy or certificate setting has
to survive and no target code runs.

The generated `foundry.toml` files now state `ffi = false` instead of relying
on Foundry's default. `vm.ffi` would hand the host to a harness built from the
target's code, and the file is ours, so the decision is stated.

`crystal doctor` prints all four rows, and the same report travels in the scan
payload and in the Arcadia hand-off — a consumer weighing a verdict needs to
know what the run could reach.

### Verified by running the tools, not by reading the code

* **Halmos** (Python toolchain): `EXECUTED_PASS :: Vault — HELD 2`, proving the
  `totalAssets/totalSupply` coherence and `nonce` monotonicity properties Build
  016 grounded.
* **Medusa** (Go toolchain): `EXECUTED_PASS :: Vault — HELD 2`.
* `git`, `forge 1.6.0`, `medusa 1.5.1`, `halmos 0.3.3` and `solc` all probe
  clean under the scrubbed environment.

A first medusa run failed, and the cause was attributed before anything was
concluded: the host `solc` is 0.8.17 and the fixture declared `^0.8.20`. The
same failure reproduced with the full environment, so it was not the change.

### Two defects found while testing this

1. **Windows stores environment names upper-cased.** `os.environ` looks up
   `SystemRoot` case-insensitively but *iterates* `SYSTEMROOT`, so exact
   matching against the allowlist withheld the one variable without which a
   Windows child fails before its first instruction. Matching is now
   case-insensitive on `nt` and exact on POSIX, where `path` and `PATH` are
   genuinely different variables.
2. **`halmos --generate-only` did not emit its `foundry.toml`.** An operator
   persisting the harness to run by hand got the test and not the config —
   losing precisely the file that turns `ffi` off. `generate()` now returns the
   artifacts it would have run under.

### Tests

`tests/test_v300_isolation.py` — 16 tests. Four carry `@pytest.mark.invariant`
and join the named CI gate, which grows from 8 to 12: a secret does not reach a
launched tool, the policy is an allowlist rather than a list of known secrets,
pack discovery never reads the analysed target, and the report states that the
process is not sandboxed — that last one so the honest line cannot be quietly
dropped.

The tests that assert a secret does not escape **run a real subprocess and read
what it received**. Inspecting the dictionary Crystal builds would pass even if
the dictionary were never handed to `subprocess.run`, which is the bug.

508 collected. 506 passed, 2 xfailed under tree-sitter; 483 passed, 24 skipped,
1 xfailed under `CRYSTAL_NO_TREESITTER=1`.

### What this does not do

It does not sandbox anything. Crystal confines the environment and the
filesystem; the tool still runs as the operator, on the operator's host, and a
fuzzer executing a target's bytecode has the operator's rights. Container or VM
isolation is the layer Crystal does not provide, and `isolation_report()` says
so in the payload rather than leaving a reader to assume otherwise.

`HOME` is passed because no toolchain resolves without it, so configuration
under it — `~/.foundry/foundry.toml`, and an RPC URL an operator keeps there —
remains reachable by the target's tooling. Stated, not glossed.

## Crystal V1.00 Build 016 — the invariants that were reading names

Measured on the Lido stonks protocol (`lidofinance/stonks`, 58 sources,
47 contracts), 2026-09-07. Prompted by a comparison with that protocol's own
Wake fuzzing harness: 416 lines of Python that reimplement the intended
semantics and compare them against a mainnet fork. Crystal cannot write that
oracle and is designed not to — but the comparison exposed something it *had*
written, which is worse.

**52 protocol invariants, 27 of them citing `heuristic:function-name` as their
only evidence. All 21 "fee" invariants had matched the substring `fee` inside
the word `Feed`** — `getFeed`, `setTokenFeed`, `isFeedInSync`,
`_resolveFeedAndScale`. Not one was about a fee. The oracle set included five
interface declarations with no body to check, four setters that write a
threshold and read no feed, and a `constant`.

The whole `crystal/protocol/` package answered its questions from spelling. The
constraint from Build 012 — *the discriminant is relational* — had been applied
to the detectors and never to this layer.

### What changed

New `crystal/protocol/grounding.py`: the predicates each generator now uses,
read out of the statement IR, each carrying the line it was observed on.

| generator | before | after |
| --- | --- | --- |
| `oracles` | `"price" in name.lower()` | an external call to a declared price method whose result reaches state or a return, plus whether a freshness guard was seen |
| `fees` → value-scaling | `"fee" in name.lower()` | a configurable numeric state variable multiplying or dividing a value that flows through the function, result reaching state, a transfer or the return |
| `accounting` | state-name families (`totalAssets`/`totalSupply`) | two non-mapping quantity variables written in the same direction by the same function |
| `tokens` | `f.name in ERC20_PATTERNS` | the published ABI signature; `mint`/`burn` kept as *conventions*, scored below standards and required to have a body that writes |
| `flows` | functions *named* `transfer`/`mint`/`deposit` | declared entry points, plus every outbound value transfer observed in a body |
| `derive_properties` monotonicity | `"nonce" in name` | every observed write to the variable increments it |

Names still appear and the distinction is the point: `latestRoundData` is a
promise published in an ABI, `getPriceThing` is a spelling. `constant` versus
`immutable` is read the same way — `MAX_BASIS_POINTS` is 10000 in every
deployment that will exist, so it is a denominator, not a protocol parameter.
That is the same line Lido's harness draws when it varies the margin, the
tolerance and the improvement cap and never varies the basis-point divisor.

### Measured, on stonks

|  | 015 | 016 | 016 · regex |
| --- | ---: | ---: | ---: |
| protocol invariants | 52 | 16 | 11 |
| resting on a name | 27 | **0** | **0** |
| citing a line or a signature | 0 | **16** | **11** |
| oracle signals | 27 | 5 | 3 |
| fee / value-scaling signals | 21 | 12 | 8 |
| detector signals | 58 | 58 | 52 |
| confirmed findings | 0 | 0 | 0 |

**Fewer is not the claim.** What survived has to be right. Two results say more
than the count:

`Stonks.estimateTradeOutput` now yields
`` `MARGIN_DIFFERENCE_IN_BASIS_POINTS` scales `expectedBuyAmount` `` with the
expression `(expectedBuyAmount * MARGIN_DIFFERENCE_IN_BASIS_POINTS) / MAX_BASIS_POINTS`
— the exact formula the Wake harness reimplements by hand as
`_estimate_trade_output`. Build 015 did not have it: the margin is applied to a
local holding an earlier call's result, and an earlier draft of this build that
demanded a parameter missed it too.

`OracleRouter._readNormalizedPrice` produces a signal and **no invariant**. It
reads Chainlink through the feed registry and guards it with `answeredInRound`,
`updatedAt` and `maxStalenessSeconds`. The source already asserts what the
invariant would say, so it is not raised. Nothing is silenced — the signal is
still there, scored below an unguarded read.

### Three things this build got wrong first

Recorded because each was caught by a measurement rather than by review, and
the second was caught by the suite the project keeps for exactly this reason.

1. **A rule that hid the finding.** The first co-movement rule required every
   function writing one variable to write the other. That drops the pair as soon
   as some path moves one alone — the donation that inflates a share price, the
   case the relation exists to expose. Two tests failed; the rule was wrong, not
   the tests. The discriminant is instead that a counter stepped by a literal is
   not a quantity.
2. **A predicate that worked on one parser.** That quantity test keyed off
   `value.kind == "number_literal"`, which is the tree-sitter spelling. The
   regex front-end says `expression` for `nonce += 1`, so every counter passed
   on the fallback path. It now keys off identifiers and the source slice, which
   both front-ends agree on, and is pinned by a test that parses with
   `solidity_regex` directly.
3. **Scaling that was addition.** Accepting any binary expression reported
   `_cumulativeRevenueUSD + amountUSD_`, an accumulator where nothing is scaled.

### Fixed on the way

* **The regex parser dropped every `constant` and `immutable` state variable.**
  `VAR_RE` allowed one modifier between the type and the name, so
  `uint16 private constant MAX_BASIS_POINTS = 1e4;` did not match at all. A
  pre-existing defect that only surfaced because this build started reading the
  `constant` flag. Modifiers are now matched in any number and order, and
  `constant`, `immutable` and the initialiser are recorded.

### Tests

`tests/test_v300_grounding.py` — 21 tests, each pinning a reason rather than a
count: a getter over local storage is not an oracle read, an interface
declaration yields nothing, an observed guard suppresses the invariant but not
the signal, a mapping is not paired with a scalar total, an asymmetric path does
not dissolve the relation, a `constant` denominator is not the scalar, and the
quantity test survives the regex front-end.

492 collected. 490 passed, 2 xfailed under tree-sitter; 467 passed, 24 skipped,
1 xfailed under `CRYSTAL_NO_TREESITTER=1`.

### What this does not do

The regex path derives 11 invariants where tree-sitter derives 16. The gap is
IR fidelity, not this layer: the regex front-end does not attach a call to a
`return` statement, so some scalings are only visible as expressions. Build 016
reads both shapes and still finds fewer on the fallback. The number is reported
rather than smoothed over.

Nothing here makes Crystal a substitute for a hand-written model like the Wake
harness. That harness knows what the protocol *intends*; Crystal is not allowed
to. What it can now do is name the relations the model would have to encode,
and say where each one lives.

## Crystal V1.00 Build 015 — three axes the execution engines leave open

Measured against Foundry 1.6.0, Medusa 1.5.1 and Halmos 0.3.3 on one bridge,
17 contracts, five properties. Not on throughput: Medusa does 1,317 calls a
second and Halmos carries an SMT solver, and chasing either produces a bad
clone of both. On the three axes nobody holds.

### Deriving the properties

The engines verified five properties a human wrote: ~50 minutes and 1,688
harness lines for Foundry alone. Crystal verified zero, refusing with "no
property could be derived without fabricating protocol assumptions". That
refusal was right when nothing carried a property. Since Build 010 campaign
packs carry `CampaignInvariant`s, and those are properties.

`crystal validate --pack <pack> --fixture <spec>` compiles them into Foundry,
Medusa and Halmos harnesses. On the reference pack, four of five invariants
compile to all three backends; the fifth refuses, precisely:

    term `the sum of unsettled quote totals` names no state variable of the
    scoped contracts and is not a sum, a scalar or a balance; Crystal will not
    infer the arithmetic that defines it

Verified by running it, not by reading it: `forge test` on the generated
harnesses gives 3 passed, 1 failed, with

    [FAIL: I5 VIOLATED: a settlement call reverted on
           CollateralManagementContract::_pegOutCollateral]
        [Sequence] (original: 442, shrunk: 6)

The contract-qualified state name in that message is Build 009's namespacing
surfacing at the far end of the chain.

**The honest number is 293 lines, not zero.** A deployment fixture stays the
operator's to write: how an upgradeable stack is wired, who the actors are, and
how to build a call whose argument is an EIP-712 signature or a Bitcoin
transaction that must parse cannot be derived from sources without inventing
them. Crystal refuses to invent and says so per property — without a fixture
all fifteen combinations are UNSUPPORTED. 293 against 1,688 is 5.8×, and the
part that remains is the part no engine can take.

### Refusing the unproven green

The dominant failure mode of these tools is not the false positive, it is the
silent success. Measured: Medusa returned 35 green tests over 1,049,043 calls
with zero successful deposits, because the actors had a zero balance.

`HELD` now requires an execution witness — sequences run, per-action success
ratio, decoded revert selectors, and proof that at least one transition
mutating the property's own state succeeded. Without it the verdict is
`VACUOUS`. `PropertyVerdict.__post_init__` raises `WitnessRequired`, and
`dataclasses.replace` re-runs it, so a VACUOUS cannot be promoted after the
fact — the same structural discipline that keeps `confirmed_findings` at 0.

Three traps are detected before a backend is launched. On the raw repository:

    [REFUSE] homonym: `Quotes` (library) is declared at 2 paths:
      src/legacy/Quotes.sol, src/libraries/Quotes.sol — the definitions differ.
      The artifact tree resolved the name to src/legacy/Quotes.sol.
      PegOutContract imports src/libraries/Quotes.sol, not the one linked.
        affects 70 contracts

    [REFUSE] zero-balance-actors: register, depositPegout are payable but the
      harness never forwards msg.value nor funds an actor; every property can
      only be VACUOUS

It names the copy actually bound, by reading the build cache. `SignatureValidator`
is the same trap a second time. And the binding is **not stable**: observed
`src/legacy/Quotes.sol` on one build and `src/libraries/Quotes.sol` on the next
with nothing changed between them — which is why the refusal cannot be replaced
by a warning about which one wins.

Halmos pins `block.number` to 1, so a property whose reachability depends on a
block delta is reported UNSUPPORTED on that backend rather than PASS.

Two supporting fixes fell out of building this. Preflight compared whole-file
digests to decide whether two declarations diverge — so a helper repeated
verbatim across generated harnesses read as divergent; it now digests the
declaration. And a refusal about two test fixtures colliding blocked nothing
real while teaching an operator to pass `--ignore-preflight` by reflex; blocking
is scoped to contracts a property runs against, and one ambiguous name prints
once with the contracts it affects.

### The Go front-end

A bridge's off-chain servers hold the signing keys and build the fields the
contracts consume. `rsksmart/liquidity-provider-server` is 548 Go files in the
same program's scope. Crystal had nothing to say about it.

Go has no contracts, no sender, no storage. The mapping is the work, and it is
argued in the front-end's own docstring: a struct with a method set is a
contract and its fields are state; a package is a contract and its `var`s are
state; interface satisfaction is decided **by signature over the whole
project** — the Go compiler's rule, not name matching — which is what lets a
call through an interface-typed field resolve to the implementation that runs,
and therefore what lets the coupling grade exist at all.

Signal trajectory: 164 with the first model, 58 after fixing the model, **2**
once detectors only see the languages their premise holds for. Both are honest
and in the low band; the coupling grade is what stops Go's ubiquitous
`if err != nil` from producing hundreds of fake asymmetries.

A detector now declares `LANGUAGES` when its reasoning is tied to an execution
model. `reentrancy-ordering` produced 51 signals on that service — every one
"external call precedes state update" on an ordinary method, none of them about
anything, because Go has no re-entrant dispatch to exploit. Omitting the
declaration means the shape is structural and travels, which is the case for
guard asymmetry.

The panic class — index, nil dereference, type assertion — is carried faithfully
in the IR and consumed by no detector. Stated rather than faked.

### One more thing a backend cannot do

Halmos 0.3.3 cannot execute the hashing precompiles. Measured: a settlement
path reaching `sha256` ran 26 minutes and produced no verdict — the log never
left compilation output, the process reached 940 MB. That is neither a failure
nor a pass, and unnamed it is worse than either: an operator waits, kills it,
and concludes the tool is broken rather than that the property is undecidable
there. `BackendTraits.unmodelled_precompiles` declares it, and such a property
is UNSUPPORTED before anything launches — the same discipline as
`advances_block`. Medusa, which executes the precompile, stays silent.

Finding it exposed a parser gap worth naming: `paid[_identify(x)] += y` records
no call at all. A call inside an index expression on the left-hand side is
dropped by both front-ends, so nothing built on `ir.calls()` can follow it. That
is pinned as a strict xfail rather than worked around, because it silently
narrows every traversal in the engine, not only this one.

Tests: 470 passing (+95). Go has a regex fallback with its own declared
limitations, as Solidity does.

## Crystal V1.00 Build 014 — the fallback nobody was watching

Three residues on the coupling grade, five parallel improvements, and one
regression that had been shipping silently for four builds.

**The regex fallback had been broken since Build 010.** The README promises
that without tree-sitter Crystal "degrades to regex parsers and says so, rather
than failing". It was failing, quietly: 16 of its own tests were red on
`CRYSTAL_NO_TREESITTER=1`. Everything built since Build 010 — declared-type
binding, cross-contract `call-flow` edges, the guard-asymmetry detector, the
whole coupling grade — produced nothing at all on the fallback path. The suite
was last run that way at Build 009 and not since. Three parser defects, each
one enough on its own:

* `interface X {}` was recorded with `kind="contract"`, so an interface became
  its own implementation and every declared-type binding's confidence halved.
* State writes were matched with the assignment operator required *immediately*
  after the name, so `balances[msg.sender] -= x` and `pool.total = y` were not
  writes. Whole contracts reported empty `writes`. The same pattern read
  `x == y` as an assignment.
* A local declaration was emitted as `assign`, not `var_decl`, and carried
  `uint256 stored` as its target instead of `stored`, so nothing could learn
  that a local came *from* a particular call.

**Two shapes stopped sharing one scale.** `asymmetric-side-effect` emitted a
guard asymmetry and a missing-companion asymmetry on one ranking, where an
unevaluated companion at 0.833 outranked the only known true defect at 0.770.
The natural analogue of coupling for the companion shape — does the absent
companion touch the state the primary operation writes? — turns out to be
unanswerable in practice: measured across two protocols, the primary operation
is an inherited `_burn`, an interface method with no body, or an ERC-20 outside
the scanned tree. It could not be calibrated, so it was separated instead, into
`asymmetric-companion`, and now says what its number means: a convention ratio,
not a defect confidence. `unresolved` and `uncoupled` are reported as different
claims, because they are.

**Every demotion names what failed to couple**, not only what coupled:

```
coupling [partially-coupled]: guard coupled only to the argument(s) cToken;
its subject borrowGuardianPaused is neither read nor written by
distributeBorrowerComp (compAccrued, compBorrowerIndex) — a precondition of
the calling function, not a guard on the callee's effect
```

**Casts and struct literals are no longer calls.** `Exp({mantissa: ...})` was
recorded as a callee, and two entry points were reported as sharing a "state
transition" that was a struct construction. Worse, the extractor kept only the
outermost expression, so the real external call inside — `CToken(cToken)
.borrowIndex()` — was lost entirely. Excluding conversions by what the project
declares recovers 8 real external calls on one target while removing 108
non-calls. A name the project declares as both a type and a function stays a
call; a name declared nowhere stays a call. No evidence, no guess.

**`reentrancy-ordering` resolves what actually transfers control.** A call
reaches attacker code only if its receiver resolves to an address, an
interface, or a library whose body makes such a call. Flyover 5 → 2, Strata
22 → 18, and a `Math.min` no longer reads as re-entrancy.

**Scaffolding leaves research once, by path, before anything is built.** It was
being filtered afterwards, and by contract *name* — so `Quotes` and
`SignatureValidator`, which exist in both `libraries/` and `legacy/`, had their
live copies dropped along with the dead ones. And because the 250-sequence cap
was spent on the unfiltered graph, only 94 live candidates reached research;
now all 250 do.

**The graph agrees with the report.** State transitions, nodes, causal edges
and the call graph no longer describe contracts the same report says were
excluded: 460 → 226 transitions, 319 → 88 edges, 161 → 52 nodes, all excluded
counts now zero. `--include-tests` restores them element for element.

**Exclusions are visible.** All 59 excluded contracts carry a per-contract
reason in JSON, Markdown and the Arcadia hand-off; Markdown had been listing 40
and dropping 19 in silence. The published JSON schema was missing six top-level
keys, `composition` among them, present since Build 005.

**`crystal doctor` reports build drift.** The editable install has been observed
pointing at extracted ZIP snapshots frozen several builds behind the repo —
five of them accumulated in one day, and two live bugs were re-reported after
being fixed because the scan ran an old copy. Doctor now names the package
location, whether it sits in a git checkout, HEAD, whether the build matches,
and whether pip's editable target is somewhere else entirely. Drift is a
warning, never a failure: the exit code is unchanged.

| on the three reference protocols | Build 013 | Build 014 |
| --- | ---: | ---: |
| Flyover, the true defect | 0.770 | **0.770** |
| CapyFi, deliberate asymmetry | 0.440 | **0.440** |
| Strata, uncoupled guards | 0.440 | **0.440** |
| asymmetry signals, all three | 1 / 8 / 30 | **1 / 8 / 31** |
| reentrancy signals, all three | 5 / 2 / 22 | **2 / 2 / 18** |
| regex-fallback test failures | 16 | **0** |

Ranking unchanged and still founded; recall unchanged and one signal gained.
`confirmed_findings` is still 0, still unreachable by machine.

The fallback is green again, but not at parity, and the difference is now
stated rather than implied. Eleven of the sixteen failures were the four parser
defects above. The remaining five, plus four of the new reentrancy tests, need
language features the regex front-end does not model at all — `import`
resolution, `using X for Y`, user-defined value types, modifier bodies — so a
receiver's type cannot be identified there. Those nine tests are skipped on
that path with that reason, and `parser_report()` now carries a
`reduced_fidelity` flag and the list of limitations, so a report produced on
the fallback says which conclusions it is not entitled to draw. Naming the
parser was never the same as naming what using it costs: a detector that stays
quiet because it could not resolve a type looks exactly like a clean result.

Tests: 375 passing (+85). On `CRYSTAL_NO_TREESITTER=1`: 365 passing, 10 skipped,
0 failing — the first green fallback run since Build 009.

## Crystal V1.00 Build 013 — the score that disagreed with the evidence beside it

Build 012's `asymmetric-side-effect` was measured against three unrelated public
protocols, and its confidence was anti-correlated with the truth on all three:

```
0.833  Strata    guards foreign to the callee            (dubious)
0.770  Flyover   the real Critical                       (true)
0.650  CapyFi    a deliberate, correct asymmetry         (false positive)
```

The only real defect ranked second, behind the least coupled case.

The cause was arithmetic. Confidence was a base plus a boost per corroborating
fact, and coupling — the thing that actually separates the three — was printed
as a note *beside* the score instead of setting it. So a case with three
unrelated preconditions outscored a guard reading the exact slot the callee
clamps, because it had more guards.

Coupling is now the dominant term, in three bands that do not overlap. Nothing
inside a band can climb into the one above it.

**What counts as coupling changed too.** The old test intersected the guard's
operands with the callee's `reads | writes`. A callee reads a great deal of
state, so almost any guard coupled — which is how a pair of `deposit`
preconditions came to be graded stronger than the real case. Only state the
callee **writes** counts now, and a receiver rule fires only when the call
actually mutates that receiver: a getter on a handle is not an effect to guard.

**The sharp discriminant is relational, and needs both halves.** The strong
shape relates state the callee *writes* to an argument it *consumes* — a guard
re-deciding what the callee already decides about its own input. That is
exactly the real defect: it compares the collateral slot the callee clamps
against the penalty argument the callee clamps it with, and the callee uses
`Math.min`, so the guard decides nothing and only blocks a settlement for a
payment already made.

Either half alone is much weaker, and both weak halves were among the measured
cases. State alone is ordinary control flow — a caller may legitimately read a
flag its callee later sets, which is the Strata shape. Argument alone is nearly
free, since a guard naturally names the values it is about to pass on, which is
the CapyFi shape: the paused-borrowing check is a precondition of borrowing,
and repayment must stay possible while borrowing is suspended.

**Guards are no longer summed.** Only the single best-coupled guard is evidence.
Counting differentiating conditions was what put the least coupled case first.

```
0.770  Flyover   effect-coupled
0.440  Strata    partially-coupled   reads a flag the callee sets, constrains no argument
0.440  CapyFi    partially-coupled   names an argument it passes on, no written state
```

Every weak grade says so in its own evidence, and adds that the asymmetry is
real but nothing ties the differentiating condition to what the shared call
does. **Recall is unchanged — 1 / 8 / 30 on the three targets, before and
after.** Selection was deliberately left alone; only the grading moved. A
detector that reports just what it can prove is worth nothing to someone
looking for what nobody has proved yet: all three are still emitted, correctly
ordered.

Tests: 290 passing (+9), on synthetic shapes sharing no vocabulary with any of
the three targets, plus a test asserting the detector source contains none of
their words.

## Crystal V1.00 Build 012 — the contract does the opposite thing next door

Measured against a real multi-contract protocol rather than a fixture:
`rsksmart/liquidity-bridge-contract` (Rootstock Flyover), 5 contracts and 17
files, carrying a Critical the operator had already found by hand. Crystal
produced 9 signals on it, all false, and did not name the defect.

The defect is two entry points of one contract reaching the same callee with the
same arguments, one behind a condition that can revoke and one behind nothing —
and the callee already clamps, so the condition guards nothing and only blocks a
settlement for a payment already made. In the operator's words: *the contract
does the opposite thing next door.*

**`asymmetric-side-effect` could not see it, for two independent reasons.**
`VALUE_OPERATIONS` was a hardcoded name list and gated the grouping, so a callee
named anything else was never even considered. And the guard is an `if (..)
revert` nested inside another `if`, while the detector collected only
function-wide `require` and modifiers — both siblings carry `nonReentrant`, so
even that was symmetric. The detector now walks the IR per entry point tracking
path conditions with polarity — enclosing branches, `require`, `if (..) revert`,
`if (..) return`, modifiers, and the top-level guards of internal helpers on the
path — and groups sites by (contract, callee, receiver, arity) across two or
more entry points. The name list survives only as a +0.04 confidence boost.

A guard counts only when *coupled* to the call: it reads from the receiver the
call mutates, or from state the resolved callee touches. Two precision rules
removed six false positives: identical argument text is required, and a wrapper
that pre-checks what the callee itself requires is not reported. The real callee
clamps with `Math.min` instead of reverting, so it is not suppressed — which is
exactly the asymmetry.

It reports the shape, not the verdict. Crystal names `refundUserPegOut` as the
unguarded site; knowing that the *guard* is the defect requires knowing the
callee clamps, and that is protocol interpretation, which is not Crystal's job.

**One-shot initializers headed the ranking.** `initialize -> depositPegOut ->
refundPegOut` scored 0.550 against 0.506 for the real chain, and four of nine
slots in the operator's own campaign went to `initialize` chains. A function
carrying `initializer` / `reinitializer` — or the hand-rolled equivalent, a flag
it sets that blocks a second call — cannot join a sequence of length > 1. 71
sequences pruned on that rule alone; no `initialize` chain survives anywhere.

**`call-flow` gated the semantics it was supposed to serve.** Build 010 added
cross-contract call edges under a new `edge_kind`, and campaigns match
transitions by exact kind. So every campaign declaring a *meaning* —
`balance-transfer`, `write-read` — rejected every cross-contract chain, because
the chain's edges said `call-flow`, a *mechanism*. The operator's P5 campaign,
written for this exact defect, therefore did not contain the defect chain at
all. Transitions now also match the kinds an edge's own state categories imply.
The chain lands at rank 2 of 10 inside P5.

**Scaffolding was research input.** `src/test-contracts/` vendors the entire
Gnosis Safe codebase and `src/legacy/` holds superseded copies of the live
contracts; the fixture classifier matched `test/` and `mock/` by name and let
both through. 52 of 69 contracts are now excluded by path segment, listed with a
per-contract reason, restorable with `--include-tests`.

**Duplicates.** One (detector, contract, function) yields one signal, highest
confidence winning and the other instances' lines folded in. One chain yields
one candidate, annotated with every campaign that selected it.

| on the operator's 17-file scope | Build 011 | Build 012 |
| --- | ---: | ---: |
| detector signals | 9 | **6** |
| of which name the defect | 0 | **1** |
| campaign candidates | 19 | **12** |
| chains rendered twice | 3 | **0** |
| chains headed by `initialize` | 6 of top 10 | **0** |
| rank of the real defect chain | 11th | **2nd** |

On the full source tree: 33 signals to 6, 27 composition candidates to 8.
`confirmed_findings` is still 0, and still unreachable by machine.

Tests: 281 passing (+25). The asymmetry shape is proved on a second fixture in an
unrelated domain — membership dues — sharing no vocabulary with the target, so
the rule is structural and not a codified name.

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
