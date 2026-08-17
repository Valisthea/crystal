
# Crystal 0.8.4 — Hardening

## Zero false positives
`CONFIRMED` remains proof-gated. Novelty and heuristics cannot bypass it.

## Novelty calibration
- sequence length alone does not create novelty
- known pattern similarity suppresses novelty
- known finding similarity suppresses novelty
- unknown-behavior queue excludes candidates too close to known classes

## Equal-parameter benchmark
Crystal, Slither, Medusa and Echidna are compared by the same corpus and
constraints. Capabilities are compared honestly; no unavailable engine output
is fabricated.
