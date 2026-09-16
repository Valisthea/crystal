"""Asking Crystal a question, rather than running it and reading what falls out.

`ResearchQuestion` is Crystal's first input contract. Before it, the only way in
was `research(project, use_solc=..., packs=...)` — a description of how to run
the tool, never of what it should try to settle.

**Architecture note.** This is a Crystal-native contract, introduced before the
Arcadia seam exists and with no dependency on Arcadia or MIRA. Connecting those
layers later is expected to add fields, adapters or a translation layer. Such
changes must preserve the standalone contract and must not move global authority
— scheduling, coverage, saturation, hypothesis authority, submission — into
Crystal. That is deliberate sequencing, not a defect: a seam built over an
incoherent interior encodes the incoherence, and this repository has twice
shipped an interior that needed correcting first.

What this step establishes is narrow and worth stating plainly: Crystal can be
*asked* something through a formal contract. It does not mean Crystal
understands the question semantically, nor that anything can yet schedule it.
"""

from .capabilities import CAPABILITIES, KNOWN, availability, export, unknown
from .runner import (
    EXECUTED,
    REFUSED_INVALID,
    REFUSED_UNPLANNABLE,
    QuestionRun,
    execute,
)
from .legacy import from_legacy_call, to_legacy_kwargs
from .model import (
    AT_EXECUTION,
    DEPTHS,
    ENFORCEABLE_BUDGET,
    ENFORCEABLE_CONSTRAINTS,
    EXPECTED_OUTPUTS,
    SCHEMA_MAJOR,
    SCHEMA_VERSION,
    SURFACE_KINDS,
    PriorEvidence,
    ResearchQuestion,
    SourceSnapshot,
    Surface,
    Target,
)
from .serialization import SemanticDrift, dump, dumps, load, loads
from .strategy import STRATEGIES, UNMEASURED, StrategyPlan, StrategyRecord, plan
from .validation import UnsupportedSchema, ValidationResult, parse_guard, validate

__all__ = [
    "AT_EXECUTION",
    "CAPABILITIES",
    "EXECUTED",
    "DEPTHS",
    "ENFORCEABLE_BUDGET",
    "ENFORCEABLE_CONSTRAINTS",
    "EXPECTED_OUTPUTS",
    "KNOWN",
    "SCHEMA_MAJOR",
    "SCHEMA_VERSION",
    "STRATEGIES",
    "SURFACE_KINDS",
    "UNMEASURED",
    "PriorEvidence",
    "QuestionRun",
    "REFUSED_INVALID",
    "REFUSED_UNPLANNABLE",
    "ResearchQuestion",
    "SemanticDrift",
    "SourceSnapshot",
    "StrategyPlan",
    "StrategyRecord",
    "Surface",
    "Target",
    "UnsupportedSchema",
    "ValidationResult",
    "availability",
    "dump",
    "dumps",
    "execute",
    "export",
    "from_legacy_call",
    "load",
    "loads",
    "parse_guard",
    "plan",
    "to_legacy_kwargs",
    "unknown",
    "validate",
]
