from dataclasses import dataclass

@dataclass(frozen=True)
class Mutation:
    target: str
    kind: str
    rationale: str
    priority: float

MUTATIONS = (
    ("zero", "Boundary: zero"),
    ("one", "Boundary: one"),
    ("near-max", "Boundary: near uint256 maximum"),
    ("repeated", "Repeated invocation"),
    ("reverse-order", "Reverse candidate sequence"),
    ("interleave", "Interleave two state-changing paths"),
)

def generate_mutations(sequence_hypotheses):
    out = []
    for seq in sequence_hypotheses[:100]:
        target = " -> ".join(seq.sequence)
        for kind, rationale in MUTATIONS:
            # Mutations are research instructions, not exploit claims.
            base = seq.score
            bonus = .06 if kind in {"reverse-order", "interleave"} else .03
            out.append(Mutation(
                target, kind,
                f"{rationale}; test whether the sequence changes an expected invariant",
                min(.95, base + bonus)
            ))
    return sorted(out, key=lambda x: (-x.priority, x.target, x.kind))
