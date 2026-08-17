# Crystal v0.8.1 — Causal Optimization

The research engine now ranks behavior by **causal state dependency**, not merely
by shared state or global mechanism presence.

## Key changes

- explicit producer → consumer causal edges
- order-aware sequence generation
- impact relevance tied to state actually touched by the sequence
- composition requires a valid causal edge for every adjacent transition
- irrelevant oracle/fee/accounting categories no longer inflate a chain
- three-step sequences are retained only when the intermediate state dependency
  is real

This is still hypothesis generation. Concrete execution remains required.
