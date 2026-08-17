from dataclasses import dataclass

@dataclass(frozen=True)
class ImpactPath:
    category: str
    path: list[str]
    impact_signal: str
    confidence: float
    relevant_state: list[str]

def _fnmap(contracts):
    return {f"{c.name}.{f.name}":f for c in contracts for f in c.functions}

def infer_impact_paths(protocol_model, protocol_invariants, sequence_hypotheses, contracts=None):
    out=[]
    fmap=_fnmap(contracts or [])
    for seq in sequence_hypotheses[:100]:
        seq_path=list(seq.sequence)
        funcs=[fmap.get(x) for x in seq_path]
        funcs=[x for x in funcs if x]
        writes=set().union(*(f.writes for f in funcs)) if funcs else set()
        reads=set().union(*(f.reads for f in funcs)) if funcs else set()
        touched=writes|reads

        for inv in protocol_invariants:
            # Only attach an invariant when its evidence/state names intersect
            # the actual sequence state, avoiding global category pollution.
            expr=inv.expression.lower()
            relevant=[s for s in touched if s.lower() in expr]
            if inv.category=="accounting":
                category="economic-accounting"
                signal="sequence changes state involved in an accounting relation"
            elif inv.category=="oracle":
                category="price-dependent"
                signal="sequence changes/consumes state in an oracle-related relation"
            elif inv.category=="fees":
                category="fee-dependent"
                signal="sequence changes/consumes fee-related state"
            else:
                continue
            if relevant or any(k in expr for k in ("totalassets","totalsupply","debt","reserve","price","fee")) and relevant:
                score=min(.92, inv.confidence+.08*(len(relevant)>0))
                out.append(ImpactPath(category,seq_path,signal,round(score,3),sorted(relevant)))
    # deterministic dedupe
    seen=set(); unique=[]
    for x in out:
        k=(x.category,tuple(x.path),tuple(x.relevant_state))
        if k not in seen:
            seen.add(k); unique.append(x)
    return sorted(unique,key=lambda x:(-x.confidence,x.category,x.path))
