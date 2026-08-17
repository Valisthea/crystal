
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
    # Crystal gained concrete fuzzing in V1.00 Build 001: boundary values are
    # substituted into the symbolic polynomials, and Foundry/Medusa/Echidna/
    # Halmos run harnesses Crystal generates itself.
    EngineCapability("Crystal", True, True, True, True, True, True),
    EngineCapability("Slither", True, False, False, False, False, False),
    EngineCapability("Medusa", False, True, True, False, True, True),
    EngineCapability("Echidna", False, True, True, False, True, True),
]
