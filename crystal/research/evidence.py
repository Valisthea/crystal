from dataclasses import dataclass, asdict
from typing import Any
import json
from pathlib import Path

EVIDENCE_SCHEMA_VERSION = "1.0"

@dataclass(frozen=True)
class EvidenceRecord:
    schema_version: str
    tool: str
    tool_version: str
    status: str
    hypothesis: list[str]
    observations: list[str]
    state_delta: dict[str, str]
    constraints: list[str]
    execution_backend: str | None
    reproducible: bool
    trace: str | None
    novelty: float | None
    known_similarity: float | None
    limitations: list[str]
    provenance: dict[str, Any]

def make_evidence(**kwargs):
    defaults = dict(
        schema_version=EVIDENCE_SCHEMA_VERSION,
        tool="crystal",
        tool_version="1.0.0",
        status="RESEARCH",
        hypothesis=[],
        observations=[],
        state_delta={},
        constraints=[],
        execution_backend=None,
        reproducible=False,
        trace=None,
        novelty=None,
        known_similarity=None,
        limitations=[],
        provenance={},
    )
    defaults.update(kwargs)
    return EvidenceRecord(**defaults)

def write_jsonl(path, records):
    p=Path(path)
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            obj=asdict(r) if hasattr(r, "__dataclass_fields__") else r
            f.write(json.dumps(obj, sort_keys=True, default=list)+"\n")
