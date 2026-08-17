from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class Edge:
    source: str
    target: str
    kind: str
    evidence: dict = field(default_factory=dict)

@dataclass
class ProgramGraph:
    nodes: set[str] = field(default_factory=set)
    edges: list[Edge] = field(default_factory=list)

    def add_node(self, node):
        self.nodes.add(node)

    def add_edge(self, source, target, kind, **evidence):
        self.nodes.add(source)
        self.nodes.add(target)
        self.edges.append(Edge(source, target, kind, evidence))

def build_graph(contracts):
    graph = ProgramGraph()
    for contract in contracts:
        graph.add_node(contract.name)
        for f in contract.functions:
            fn = f"{contract.name}.{f.name}"
            graph.add_edge(contract.name, fn, "contains")
            for v in f.reads:
                graph.add_edge(fn, f"{contract.name}.state.{v}", "reads")
            for v in f.writes:
                graph.add_edge(fn, f"{contract.name}.state.{v}", "writes")

    writers, readers = defaultdict(list), defaultdict(list)
    for c in contracts:
        for f in c.functions:
            for v in f.writes: writers[(c.name,v)].append(f.name)
            for v in f.reads: readers[(c.name,v)].append(f.name)

    for (c,v), ws in writers.items():
        for w in set(ws):
            for r in set(readers.get((c,v), [])):
                if w != r:
                    graph.add_edge(f"{c}.{w}", f"{c}.{r}", "state_dependency", variable=v)
    return graph
