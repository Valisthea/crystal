from dataclasses import dataclass

@dataclass(frozen=True)
class CompositionCandidate:
    chain: list[str]
    mechanism: list[str]
    escalation: str
    score: float
    validation_required: bool = True
    causal_edges: list[dict] = None
    relevant_state: list[str] = None

def _edge_map(result):
    return {(e.source,e.target):e for e in result["state_graph"].causal_edges}

def compose(result):
    if "state_graph" not in result:
        return []
    edge_map=_edge_map(result)
    impact_by_path={}
    for x in result["impact_paths"]:
        impact_by_path.setdefault(tuple(x.path),[]).append(x)

    out=[]
    for seq_obj in result["sequence_hypotheses"][:100]:
        seq=list(seq_obj.sequence)

        edges=[]
        valid=True
        for a,b in zip(seq,seq[1:]):
            e=edge_map.get((a,b))
            if not e:
                valid=False; break
            edges.append(e)
        if not valid:
            continue

        impacts=impact_by_path.get(tuple(seq))
        if impacts:
            mechanisms=sorted(set(x.category.split("-")[0] for x in impacts))
            relevant=sorted(set().union(*(set(x.relevant_state) for x in impacts)))
        else:
            # A chain can compose without touching an economic invariant. A
            # protocol split across contracts composes by calling: the chain
            # crosses a contract boundary and the callee writes state. Gating
            # composition on accounting/oracle/fee invariants alone reports
            # zero on a protocol that is nothing but composition.
            crossing=[
                e for e in edges
                if e.edge_kind=="call-flow" and e.consumed
                and e.source_contract!=e.target_contract
            ]
            if not crossing:
                continue
            mechanisms=["cross-contract"]
            relevant=sorted(set().union(*(set(e.consumed) for e in crossing)))
        causal_score=sum(e.score for e in edges)/len(edges)

        # Longer, fully causal paths get a modest bonus; unsupported category
        # stacking is deliberately impossible.
        score=min(.97, causal_score + .05*max(0,len(seq)-2) + .03*max(0,len(mechanisms)-1))
        out.append(CompositionCandidate(
            list(seq),mechanisms,
            "causal chain requires concrete state execution and counterexample validation",
            round(score,3),True,
            [{"source":e.source,"target":e.target,"produced":list(e.produced),
              "consumed":list(e.consumed),"score":e.score} for e in edges],
            relevant
        ))

    seen=set(); unique=[]
    for x in out:
        k=(tuple(x.chain),tuple(x.mechanism),tuple(x.relevant_state))
        if k not in seen:
            seen.add(k); unique.append(x)
    return sorted(unique,key=lambda x:(-x.score,-len(x.chain),x.chain))
