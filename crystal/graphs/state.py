from dataclasses import dataclass, field

@dataclass
class StateTransition:
    function: str
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    visibility: str = "unspecified"

@dataclass(frozen=True)
class CausalEdge:
    source: str
    target: str
    produced: tuple[str, ...]
    consumed: tuple[str, ...]
    score: float

@dataclass
class StateGraph:
    transitions: list[StateTransition] = field(default_factory=list)
    causal_edges: list[CausalEdge] = field(default_factory=list)

def build_state_graph(contracts):
    sg=StateGraph()
    for c in contracts:
        for f in c.functions:
            sg.transitions.append(StateTransition(
                function=f"{c.name}.{f.name}",
                reads=set(f.reads),
                writes=set(f.writes),
                visibility=f.visibility,
            ))
    for a in sg.transitions:
        if a.visibility not in {"public","external"}:
            continue
        for b in sg.transitions:
            if a.function == b.function or b.visibility not in {"public","external"}:
                continue
            produced=a.writes & (b.reads | b.writes)
            consumed=a.writes & b.reads
            if consumed:
                # Direct producer -> consumer dependency is stronger than
                # mere shared writes.
                score=min(.95, .62 + .11*len(consumed) + .04*len(produced))
                sg.causal_edges.append(CausalEdge(
                    a.function,b.function,
                    tuple(sorted(produced)),tuple(sorted(consumed)),
                    round(score,3)
                ))
    return sg

def candidate_sequences(state_graph, max_len=3):
    edges=state_graph.causal_edges
    outgoing={}
    for e in edges:
        outgoing.setdefault(e.source,[]).append(e)

    result=[]
    seen=set()

    def walk(path, edge_scores):
        if len(path)>=2:
            key=tuple(path)
            if key not in seen:
                seen.add(key)
                result.append((key, min(edge_scores)))
        if len(path)>=max_len:
            return
        for e in outgoing.get(path[-1],[]):
            if e.target in path:
                continue
            walk(path+[e.target],edge_scores+[e.score])

    for e in edges:
        walk([e.source,e.target],[e.score])

    # Prefer longer causally-linked paths, then stronger edges.
    result.sort(key=lambda x:(-len(x[0]),-x[1],x[0]))
    return [x[0] for x in result[:250]]
