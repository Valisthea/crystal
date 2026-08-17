# Crystal v0.8 — Behavioral Research Engine

Crystal v0.8 changes the research model from pattern detection toward
behavioral exploration.

## New

### Behavioral relations
Finds public/external functions that interact with overlapping state and marks
them for path-dependent analysis.

### Differential analysis
Creates candidate comparisons between related operations, including inverse
operations such as:

- deposit / withdraw
- mint / burn
- borrow / repay
- stake / unstake

The engine does not assume they are equivalent. It asks validation backends to
test the relationship.

### Mutation research
Generates controlled research mutations:

- zero
- one
- near-maximum
- repeated calls
- reverse ordering
- interleaving

### Impact paths
Connects sequences to accounting, oracle and fee assumptions.

### Composition engine
Combines only explicitly connected sequence/invariant signals. Arbitrary
"bug chaining" is intentionally rejected.

## Design principle

Crystal does not claim a vulnerability from these layers. It generates
testable research candidates. A confirmed finding requires concrete validation.

The long-term objective is discovery of behaviors that signature-oriented
detectors are not designed to express.
