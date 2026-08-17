# Arcadia Hand-off — Crystal V1.00 Build 006

Crystal is Arcadia's microscope. It finds high-value state transition anomalies
that Arcadia can turn into security hypotheses and reproducible PoCs. Crystal
does NOT decide severity, does NOT decide submission, and does NOT produce
confirmed findings.

## Output contract

```bash
crystal scan ./target --format arcadia -o handoff.json
```

The Arcadia format (`crystal-arcadia/2.0`) is structured for machine
consumption. Every field is documented here.

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Always `"crystal-arcadia/2.0"` |
| `producer` | object | Tool identity and role declaration |
| `target` | object | Project metadata, contracts, parsers |
| `signals` | object | Detector signals, anomalies, candidates |
| `state_model` | object | State deltas, invariant candidates |
| `validation` | object | Concrete and Foundry validation results |
| `evidence_records` | array | Structured evidence for each candidate |
| `campaigns` | array | Campaign results with scored candidates |
| `gate` | object | Finding gate policy and decisions |
| `quality` | object | Quality validation report |
| `counts` | object | Summary counts for all signal types |

### Campaign results

Each entry in `campaigns` is a campaign result:

```json
{
  "campaign_id": "generic-ownership-transition",
  "campaign_name": "Ownership Transition",
  "candidates": [
    {
      "candidate_id": "...",
      "campaign_id": "generic-ownership-transition",
      "category": "ownership",
      "target_contract": "Token",
      "target_functions": ["Token.setOwner", "Token.transfer"],
      "hypothesis": "ownership transition may leave stale authority; affected state: owner, balances",
      "state_sequence": ["Token.setOwner", "Token.transfer"],
      "state_before": {"owner": "S0:owner"},
      "state_after": {"owner": "ARG:x"},
      "state_delta": {"owner": "ARG:x - S0:owner"},
      "causal_chain": ["ownership-action"],
      "confidence": 0.7,
      "novelty_score": 0.8,
      "evidence": [
        "delta(owner) = ARG:x - S0:owner",
        "invariant-candidate: previous owner must not retain authority"
      ],
      "suggested_next_action": "Arcadia: investigate exploitability",
      "score": 0.42
    }
  ],
  "deferred": [],
  "total_sequences_explored": 12,
  "total_sequences_pruned": 4,
  "warning": ""
}
```

### How Arcadia should consume this

1. **Read `campaigns` first.** Candidates are pre-scored and pre-ranked. The
   `score` field is the multi-factor composite; higher is more interesting.

2. **Use `hypothesis` and `evidence` to form the investigation prompt.** The
   hypothesis is a structured English sentence; the evidence is a list of
   observations Crystal made structurally.

3. **Use `state_delta` to understand what moved.** Deltas are symbolic
   expressions over attacker-controlled symbols (`ARG:`) and protocol state
   (`S0:`).

4. **Use `suggested_next_action` as a hint**, not a command. Crystal suggests
   what Arcadia should investigate, but Arcadia decides.

5. **Never treat Crystal output as a confirmed finding.** Every candidate is
   `RESEARCH` status. The `validation_status` field is always `SYMBOLIC_ONLY`
   unless Foundry executed the hypothesis, in which case it is `EXECUTED` — but
   even that is not confirmation.

## Campaign packs

Crystal ships with six built-in packs:

| Pack | Campaigns | Domain |
| --- | --- | --- |
| generic | 5 | ownership, role, nonce, temporal, balance |
| defi | 3 | oracle, share inflation, permit |
| registry | 2 | resolver, approval |
| authorization | 2 | stale auth, revocation |
| migration | 1 | permission preservation |
| economic | 2 | quote settlement, allowance |

The ENS preset adds 7 more (loaded explicitly, not by default).

### Loading extra packs

```bash
crystal campaign list                           # show registered campaigns
crystal campaign run <campaign-id> ./target     # run one campaign
crystal campaign run <id> ./target --pack crystal.packs.ens  # with ENS pack
```

## Scoring model

Candidates are scored by six weighted factors:

| Factor | Weight | Description |
| --- | --- | --- |
| novelty | 0.20 | How unusual is this behavior vs known patterns |
| state_delta_significance | 0.25 | How many and what kind of state variables change |
| causal_depth | 0.15 | How deep is the causal chain |
| authorization_relevance | 0.15 | Does the transition touch authorization state |
| order_sensitivity | 0.10 | Does function ordering change the outcome |
| exploitability | 0.15 | Structural exploitability indicators |

Penalties are applied for duplicate candidates, known patterns, and low
confidence scores. The final score is clamped to [0, 1].
