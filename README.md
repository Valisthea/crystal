<p align="center">
  <img src="assets/crystal-cover.png" alt="Project Crystal — static analyzer for smart contracts" width="100%">
</p>

<h1 align="center">Crystal V1.00 Build 001</h1>

<p align="center">
  <em>A protocol-oriented security research engine for smart contracts.</em><br>
  <strong>It produces evidence. It never produces a confirmed finding.</strong>
</p>

<p align="center">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3572A5">
  <img alt="languages" src="https://img.shields.io/badge/targets-Solidity%20%7C%20Rust%20%7C%20Move%20%7C%20Vyper-1f6feb">
  <img alt="dependencies" src="https://img.shields.io/badge/core%20dependencies-0-brightgreen">
  <img alt="tests" src="https://img.shields.io/badge/tests-138%20passing-brightgreen">
</p>

---

## What Crystal is

Crystal is a standalone CLI analyzer, in the same family as Slither, Medusa or
Halmos. One command, one target, one structured output:

```bash
crystal scan ./target --format sarif -o crystal.sarif
```

Where it differs from its neighbours:

| Tool | What you give it | What it gives back |
| --- | --- | --- |
| Slither | source | pattern-matched detectors |
| Medusa / Echidna | source **+ properties you wrote** | counterexamples |
| Halmos | source **+ tests you wrote** | proofs or counterexamples |
| **Crystal** | source | symbolic state deltas, candidate invariants and **research evidence it derived itself** |

Crystal answers a different question. Not *"does this match a known bug
pattern?"* but *"what does this protocol's state actually do, and where does it
behave asymmetrically?"*

## What Crystal is not

It is not an auditor, not an LLM, not an orchestrator, and not a report
submission system. It never decides severity and never decides whether to
submit. That judgement belongs to a human reviewer — or to Arcadia + Claude
downstream. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Install

### Windows 11 (native, no WSL)

```powershell
git clone https://github.com/Valisthea/crystal
cd crystal
.\install.ps1
crystal doctor
crystal scan .\examples --no-solc
```

### Linux / macOS / WSL

```bash
git clone https://github.com/Valisthea/crystal
cd crystal
./install.sh
crystal doctor
```

The installer verifies Python ≥ 3.10, creates a dedicated `.venv`, installs
Crystal with the tree-sitter extra, checks `PATH`, and reports which optional
external tools are present.

Options: `-Mode user|system`, `-NoTreeSitter`, `-WithSolc` (PowerShell) or
`--mode user`, `--no-treesitter`, `--with-solc` (bash).

### Manual

```bash
pip install -e ".[treesitter]"    # full fidelity
pip install -e .                  # core only, regex fallback parsers
```

**Crystal's core has zero dependencies.** tree-sitter is optional: without it
Crystal degrades to regex parsers and says so, rather than failing.

---

## Commands

| Command | Purpose |
| --- | --- |
| `crystal scan <target>` | analyze a project |
| `crystal doctor` | report environment readiness |
| `crystal watch <target>` | re-scan on file change, for use during an audit |
| `crystal validate <target> --backend medusa` | run an execution backend on generated harnesses |
| `crystal capabilities` | list engine capabilities |
| `crystal update` | self-update a git checkout |

### `scan`

```bash
crystal scan ./target                          # JSON to stdout
crystal scan ./target --format markdown -o report.md
crystal scan ./target --format sarif -o crystal.sarif      # IDE / CI
crystal scan ./target --format arcadia -o handoff.json     # Arcadia / OMEGA
crystal scan ./target --lang rust --no-solc
crystal scan ./target --detectors reentrancy,access-control
crystal scan ./target --no-treesitter          # force the fallback parsers
crystal scan ./target --no-foundry             # skip real-EVM execution
```

Output formats: `json`, `markdown`, `sarif`, `arcadia`.

---

## What a scan produces

```
$ crystal scan ./examples --no-solc --format markdown
```

```markdown
### External call precedes state update in Vault.withdraw

- Detector: `reentrancy-ordering` — confidence 0.94 — status `RESEARCH`
- Location: `examples/Vault.sol:15`
- Mechanism: an external call transfers control before the state update,
  so a re-entrant call observes stale state

Evidence:
- external call `msg.sender.call{value: amount}("")` at line 15
- state written after the call: balances, totalAssets
- state read before the call and written after: balances
- call forwards value to the callee

Ordered trace:
    L15 external_call: msg.sender.call{value: amount}("")
    L17 state_write[balances]: balances[msg.sender] -= amount
    L18 state_write[totalAssets]: totalAssets -= amount

Falsify this before believing it:
- Is the external callee trusted and immutable?
- Does a guard elsewhere on the path already prevent re-entry?
- Is the post-call write idempotent, so a nested call cannot benefit?
```

Every signal carries an **ordered trace anchored to line numbers** and a
**falsification list**. Crystal tells you how to prove it wrong.

### Symbolic state deltas

```
| Sequence                        | Delta                                          |
| ------------------------------- | ---------------------------------------------- |
| Vault.deposit -> Vault.donate   | totalAssets = ARG:msg.value#1 + ARG:msg.value#2 |
|                                 | totalSupply = ARG:msg.value#1                   |
```

`totalAssets` moved twice while `totalSupply` moved once. That asymmetry is a
donation path — the ingredient of a share-inflation attack — and Crystal found
it without knowing what `donate` means.

---

## How it works

```
target sources
      │
      ▼
discovery ──────────► language detection (.sol / .rs / .move / .vy)
      │
      ▼
parsers ────────────► tree-sitter AST  ──┐
                      regex fallback  ───┤──► language-neutral statement IR
                      solc AST         ──┘
      │
      ▼
symbolic engine ────► state deltas as canonical polynomials
      │               path constraints, branch forking, loop unrolling
      ▼
graphs ─────────────► CFG (basic blocks) · call graph · storage layout · dataflow
      │
      ▼
detectors ──────────► reentrancy · access control · first depositor · oracle
research engine ────► differential · composition · structural novelty
      │
      ▼
finding gate ───────► 8-gate proof checklist  ►  RESEARCH / VALIDATION
      │                                          (never CONFIRMED)
      ▼
evidence records ───► JSON · SARIF · Markdown · Arcadia
```

### The symbolic engine

State is tracked as a canonical polynomial over attacker-controlled symbols:

```solidity
function transfer(address to, uint256 a) external {
    balances[msg.sender] -= a;
    balances[to] += a;
}
```
→ `delta(balances) = 0` — the engine proves conservation, it does not guess it.

```solidity
if (totalSupply == 0) { shares = assets; }
else { shares = assets * totalSupply / totalAssets; }
```
→ two paths, one of which yields
`DIV(ARG:assets*S0:totalSupply/S0:totalAssets)` — the exact expression an
inflation attack manipulates.

Anything the engine cannot model exactly — inline assembly, unbounded loops,
unresolved storage mutations — is recorded in `unsupported`, never
approximated.

### Multi-language

| Language | Backend | Maps to |
| --- | --- | --- |
| Solidity | tree-sitter, regex fallback | contracts, functions, modifiers, events |
| Rust (Substrate) | tree-sitter | `#[pallet::storage]` → state, `#[pallet::call]` → extrinsics |
| Rust (Anchor) | tree-sitter | `#[program]` → instructions, `#[account]` → state |
| Rust (plain) | tree-sitter | `struct` + `impl` → contract + functions |
| Move | tree-sitter or regex | modules, resources with abilities, entry functions |
| Vyper | tree-sitter or regex | storage declarations, `@external` functions |

Substrate storage calls are lowered into ordinary IR assignments, so
`TotalSupply::<T>::mutate(|t| *t += amount)` yields
`delta(TotalSupply) = ARG:amount` exactly like Solidity's `+=`.

---

## The evidence discipline

Crystal separates:

```
signal → hypothesis → validation → evidence
```

from:

```
protocol interpretation → exploit chain → severity → final finding
```

The second chain is not Crystal's job.

**Three rules that are enforced by tests, not by convention:**

1. **Zero fabrication.** Missing an ABI type, a constructor argument or a
   deployment context means `UNSUPPORTED` with the precise reason — never a
   guess.
2. **Zero confirmed findings.** The proof checklist has eight gates. Two of
   them — `economic_impact` and `minimal_trace` — are *never* set
   automatically, because they require protocol interpretation. `CONFIRMED` is
   therefore unreachable by machine.
3. **SARIF severity never escalates.** Every result is `level: note`,
   `kind: review`. Crystal will not tell your CI that something is broken.

---

## Validation backends

Backends are **execution backends, not an orchestration layer**: Crystal
generates the harness and the properties itself, then asks the tool to run
them. It never reads another tool's findings — cross-tool correlation belongs
to Arcadia.

```bash
crystal validate ./target --backend medusa  --out-dir ./artifacts
crystal validate ./target --backend echidna --generate-only --out-dir ./artifacts
crystal validate ./target --backend halmos  --out-dir ./artifacts
crystal validate ./target --backend foundry --out-dir ./artifacts
```

Properties are derived only when they are mechanically expressible from public
getters. Aggregate conservation (sum of balances vs totals) needs an enumerable
holder set, so Crystal declines it and says why.

---

## Integration

```bash
crystal scan ./target --format arcadia -o handoff.json
```

```json
{
  "schema_version": "crystal-arcadia/2.0",
  "producer": { "role": "evidence-only",
                "decides_severity": false,
                "decides_submission": false },
  "signals": { "detectors": [...], "delta_anomalies": [...] },
  "state_model": { "state_deltas": [...], "invariant_candidates": [...] },
  "gate": { "policy": "zero-false-positive-confirmed" }
}
```

The v1 JSON schema is preserved field-for-field; v2 only adds. A regression
test pins every v1 key.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q                                  # 138 tests
CRYSTAL_NO_TREESITTER=1 pytest -q          # regex fallback path
CRYSTAL_NO_FOUNDRY=1 pytest -q             # skip real-EVM execution
```

Environment variables: `CRYSTAL_NO_TREESITTER`, `CRYSTAL_NO_FOUNDRY`.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — the architecture contract and non-goals
- [CRYSTAL_V2.0.md](CRYSTAL_V2.0.md) — what changed in this build
- [CHANGELOG.md](CHANGELOG.md) — version history
- [INTEGRATION.md](INTEGRATION.md) — consuming Crystal from another system
- [LAB_HANDOFF.md](LAB_HANDOFF.md) — laboratory handoff notes

## Licence

MIT — Kairos Lab.
