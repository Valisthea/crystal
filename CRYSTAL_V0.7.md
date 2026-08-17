# Crystal v0.7

Crystal now begins reconstructing **protocol semantics** instead of treating
contracts as isolated source files.

## New semantic layers

### Token model
Recognizes common token operations such as transfer, transferFrom, approval,
mint and burn.

### Value-flow model
Creates conservative hypotheses for:

- user -> protocol deposits
- protocol -> user withdrawals
- token transfers
- mint
- burn

### Accounting model
Links likely:

- assets ↔ shares
- debt ↔ assets
- reserves ↔ assets

These are candidate relationships, not proofs.

### Oracle model
Identifies likely price/oracle surfaces.

### Fee model
Identifies fee-related state and functions.

### State-machine model
Creates transitions when one function writes state consumed by another.

### Protocol invariants
Combines the above into testable candidate properties.

## Important design rule

Crystal must never report a semantic guess as a confirmed vulnerability.
Everything remains a signal, invariant candidate, sequence hypothesis, or finding
until a concrete validation backend proves the behavior.
