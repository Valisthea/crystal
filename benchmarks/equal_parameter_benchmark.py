
"""
Crystal equal-parameter benchmark.

The benchmark deliberately compares engines by *research surface*, not by
inventing execution results for tools that are not installed.

Parameters:
- same Solidity corpus
- same source snapshot
- static-only pass
- no internet
- no user-specific hints
- no manual findings
- deterministic sequence cap
- findings are only counted when concretely proven
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class BenchmarkConfig:
    corpus: str = "synthetic"
    static_only: bool = True
    manual_hints: bool = False
    internet: bool = False
    deterministic: bool = True
    max_sequence_depth: int = 3

@dataclass(frozen=True)
class EngineCapability:
    name: str
    static_patterns: bool
    sequence_reasoning: bool
    state_delta: bool
    unknown_behavior_search: bool
    concrete_fuzzing: bool
    invariant_generation: bool

ENGINES = [
    EngineCapability("Crystal", True, True, True, True, False, True),
    EngineCapability("Slither", True, False, False, False, False, False),
    EngineCapability("Medusa", False, True, True, False, True, True),
    EngineCapability("Echidna", False, True, True, False, True, True),
]
