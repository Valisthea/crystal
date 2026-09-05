from dataclasses import dataclass

from ..graphs.state import attacker_reachable_functions, one_shot_functions

# A five-contract protocol should hand a reviewer a shortlist, not a catalogue.
# Call-flow edges through interface-typed handles (what makes cross-contract
# composition visible at all) multiply the raw sequence count, so the candidates
# are deduplicated, pruned to attacker-reachable heads, and capped per head.
MAX_PER_HEAD = 3
MAX_CANDIDATES = 20


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


def _head(chain) -> str:
    return chain[0] if chain else ""


def compose(result):
    if "state_graph" not in result:
        return []
    edge_map=_edge_map(result)
    impact_by_path={}
    for x in result["impact_paths"]:
        impact_by_path.setdefault(tuple(x.path),[]).append(x)

    # A one-shot initializer already ran on the deployed proxy, so it can never
    # appear in a live attack sequence. Drop any chain of length > 1 that touches
    # one rather than letting `initialize -> ...` outrank a real defect chain.
    one_shot = one_shot_functions(result["state_graph"])

    out=[]
    for seq_obj in result["sequence_hypotheses"][:100]:
        seq=list(seq_obj.sequence)

        if len(seq) > 1 and one_shot.intersection(seq):
            continue

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

    return _rank_and_cap(out, result["state_graph"])


def _rank_and_cap(candidates, state_graph):
    """Deduplicate by chain, prune unreachable heads, and cap per head.

    This is what turns an open call-flow graph into a triageable shortlist
    without removing the edges that make cross-contract composition visible.
    """
    # Deduplicate by chain: the same sequence reached through a different
    # mechanism/state set is one candidate, kept at its best score.
    best_by_chain: dict[tuple, CompositionCandidate] = {}
    for candidate in candidates:
        key = tuple(candidate.chain)
        current = best_by_chain.get(key)
        if current is None or candidate.score > current.score:
            best_by_chain[key] = candidate

    # Prune chains whose head an unprivileged attacker cannot call. A privileged
    # or one-shot head is not an entry an exploit can start from; the graph only
    # produced it because storage happened to connect. Tails stay unrestricted.
    reachable = attacker_reachable_functions(state_graph)
    pruned = [
        candidate for candidate in best_by_chain.values()
        if not candidate.chain or _head(candidate.chain) in reachable
    ]

    ranked = sorted(pruned, key=lambda x: (-x.score, -len(x.chain), x.chain))

    # Cap the fan-out of any single head so one entry point cannot flood the
    # shortlist with near-duplicate tails.
    per_head: dict[str, int] = {}
    capped: list[CompositionCandidate] = []
    for candidate in ranked:
        head = _head(candidate.chain)
        seen = per_head.get(head, 0)
        if seen >= MAX_PER_HEAD:
            continue
        per_head[head] = seen + 1
        capped.append(candidate)

    return capped[:MAX_CANDIDATES]
