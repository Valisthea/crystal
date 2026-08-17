# Crystal v0.8.2 — State Delta Engine

Crystal now models symbolic state deltas across candidate sequences.

## Core model

`S0 -> call A -> S1 -> call B -> S2`

For parser-proven arithmetic effects, Crystal computes:

- before state
- after state
- per-variable delta
- changed variables
- sequence confidence

It then derives candidate anomalies from measurable state differences.

## Safety

This is deliberately conservative:

- unknown arithmetic is not fabricated
- a delta anomaly is a research candidate, not a confirmed finding
- concrete execution remains the validation gate

## Why this matters

The previous engine could establish that two functions were causally related.
The delta engine begins to answer the more useful question:

**"What changed because of this sequence?"**
