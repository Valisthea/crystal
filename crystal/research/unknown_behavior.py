from dataclasses import dataclass

@dataclass(frozen=True)
class UnknownBehaviorCandidate:
    sequence: list[str]
    states: list[str]
    delta: dict[str,str]
    novelty: float
    known_similarity: float
    research_priority: str

def discover_unknown_behaviors(novel_behaviors):
    out=[]
    for x in novel_behaviors:
        # Novelty only prioritizes exploration. It cannot promote a finding.
        known=x.novelty.known_pattern_similarity*.5 + x.novelty.known_finding_similarity*.5
        if known >= .60:
            continue
        if x.novelty.overall < .60:
            priority="LOW"
        elif x.novelty.overall < .78:
            priority="MEDIUM"
        else:
            priority="HIGH"
        out.append(UnknownBehaviorCandidate(
            x.sequence,x.changed_state,{},x.novelty.overall,
            round((x.novelty.known_pattern_similarity+x.novelty.known_finding_similarity)/2,3),
            priority
        ))
    return sorted(out,key=lambda x:(-x.novelty,-len(x.sequence),x.sequence))
