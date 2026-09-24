"""How prior evidence changes where a question's budget goes — and how it cannot.

Evidence handed in with a question changes *what is worth doing*. It never
becomes true, and it never removes anything from the analysis. Those two limits
decide the whole design:

* **Evidence reorders, it does not skip.** A surface item somebody already has
  supporting evidence for still gets analysed — later in each round, not never.
  Skipping it would be trusting a claim Crystal did not establish, and a gap
  opened on the strength of someone else's conclusion is a gap nobody audits.
* **Uncertainty is where the budget goes first.** An item under refuting or
  inconclusive evidence is where analysis can still change a conclusion; an
  item under contradicting evidence — some attributable source says it holds,
  another says it does not — is where it matters most, and the contradiction is
  reported as one rather than resolved by picking a side.

Only evidence Crystal can attribute and place steers. Unattributable evidence,
and evidence that does not say which part of the target it concerns, is
recorded with the reason it did not steer.

Polarity is read relative to the question: `supports` is consistent with the
question's hypothesis holding, `refutes` against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .surface import SurfaceResolution, resolve

CONTRADICTED = "contradicted"
UNCERTAIN = "uncertain"
UNEXAMINED = "no prior evidence"
SUPPORTED = "supported"

# Order in which items take their turn in each round of the budget.
TIER_ORDER = (CONTRADICTED, UNCERTAIN, UNEXAMINED, SUPPORTED)


@dataclass
class Steering:
    order: list[tuple[str, frozenset]] = field(default_factory=list)
    tiers: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    not_steering: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tiers": list(self.tiers),
            "contradictions": list(self.contradictions),
            "not_steering": list(self.not_steering),
            "rule": (
                "evidence reorders the surface, it never removes an item: "
                "contradicted, then uncertain, then unexamined, then supported"
            ),
        }


def steer(question, resolution: SurfaceResolution, contracts) -> Steering:
    """The surface items in the order the budget should reach them."""
    steering = Steering()
    groups = resolution.groups()
    bearing: dict[str, dict[str, list[str]]] = {
        label: {"supports": [], "refutes": [], "inconclusive": []}
        for label, _ in groups
    }

    for item in question.prior_evidence:
        if not item.sufficiently_provenanced:
            steering.not_steering.append({
                "evidence_id": item.evidence_id,
                "reason": "no producer or source: it cannot be attributed, so it "
                          "may not move the budget",
            })
            continue
        if not item.about:
            steering.not_steering.append({
                "evidence_id": item.evidence_id,
                "reason": "it does not say which part of the target it is about; "
                          "Crystal does not infer that from the claim's wording",
            })
            continue
        placed = resolve(item.about, contracts).functions
        touched = [label for label, members in groups if members & placed]
        if not touched:
            steering.not_steering.append({
                "evidence_id": item.evidence_id,
                "reason": "what it is about does not overlap this question's "
                          "surface in the analysed code",
            })
            continue
        for label in touched:
            bearing[label][item.polarity].append(item.evidence_id)

    def tier(label):
        found = bearing[label]
        if found["supports"] and found["refutes"]:
            return CONTRADICTED
        if found["refutes"] or found["inconclusive"]:
            return UNCERTAIN
        if found["supports"]:
            return SUPPORTED
        return UNEXAMINED

    ranked = sorted(
        enumerate(groups),
        key=lambda pair: (TIER_ORDER.index(tier(pair[1][0])), pair[0]),
    )
    steering.order = [group for _, group in ranked]
    for _, (label, members) in ranked:
        steering.tiers.append({
            "item": label,
            "tier": tier(label),
            "evidence": {k: list(v) for k, v in bearing[label].items() if v},
        })
        if tier(label) == CONTRADICTED:
            steering.contradictions.append({
                "item": label,
                "supports": list(bearing[label]["supports"]),
                "refutes": list(bearing[label]["refutes"]),
                "resolution": "required — Crystal records the disagreement and "
                              "does not pick a side",
            })
    return steering
