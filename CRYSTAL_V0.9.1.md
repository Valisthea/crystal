
# Crystal v0.9.1 — Real EVM Concrete Backend

Crystal 0.9.1 adds an optional Foundry/Anvil execution backend.

## Rule

Crystal never fabricates constructor arguments or ABI parameters merely to get
a test to run.

Unsupported hypotheses remain `UNSUPPORTED`.

## Backend flow

Novel hypothesis → constraints → bounded emulator → optional Foundry execution
→ trace/evidence → anti-finding → proof gates.

## Safety boundary

A passing Foundry test is evidence of execution, not a confirmed vulnerability.
`CONFIRMED` remains impossible without every proof gate.

The adapter is deliberately conservative in this first EVM backend. It currently
supports a simple single-contract/no-argument deployment shape and refuses
ambiguous cases. This is intentional; the next adapter can add ABI-aware
calldata generation once the parser exposes typed parameters reliably.
