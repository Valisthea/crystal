from dataclasses import dataclass

@dataclass
class SequenceHypothesis:
    sequence: list[str]
    reason: str
    score: float

def generate_sequences(state_sequences, invariant_candidates):
    out = []
    invariant_bonus = min(len(invariant_candidates) * 0.02, 0.10)

    for seq in state_sequences:
        if len(seq) == 2:
            reason = "writer-to-reader state dependency"
            score = 0.62
        else:
            reason = "multi-step state dependency chain"
            score = 0.72

        out.append(SequenceHypothesis(
            list(seq),
            reason,
            round(min(score + invariant_bonus, 0.95), 3)
        ))

    return sorted(out, key=lambda x: (-x.score, len(x.sequence), x.sequence))
