# Crystal v0.9.0 — Concrete Validation Engine

Crystal now has a concrete-validation stage.

## Pipeline

Discovery → Novel Behavior → State Delta → Constraints → Concrete Fuzzing →
Counterexample → Anti-Finding → Proof → Finding Gate.

## Important boundary

A concrete counterexample is **evidence**, not automatically a finding.

The existing zero-false-positive gate remains the only route to `CONFIRMED`.

## Backend

The current backend is a deterministic conservative symbolic emulator for
parser-proven direct state assignments. It intentionally does not pretend to
execute arbitrary Solidity.

The architecture is ready for a Foundry/Anvil or equivalent EVM backend without
changing the research model.

## Determinism

Each hypothesis is fuzzed with a fixed seed and bounded trial count so benchmark
results remain reproducible.
