from dataclasses import dataclass, field
from typing import Any

@dataclass
class SourceLocation:
    file: str
    start_line: int | None = None
    start_column: int | None = None

@dataclass
class ASTFunction:
    contract: str
    name: str
    node_id: int | None
    visibility: str | None
    mutability: str | None
    modifiers: list[str] = field(default_factory=list)
    location: SourceLocation | None = None

@dataclass
class ASTStateVariable:
    contract: str
    name: str
    node_id: int | None
    type_name: str | None
    visibility: str | None
    location: SourceLocation | None = None

@dataclass
class ASTContract:
    name: str
    node_id: int | None
    bases: list[str] = field(default_factory=list)
    functions: list[ASTFunction] = field(default_factory=list)
    state_variables: list[ASTStateVariable] = field(default_factory=list)

@dataclass
class CompilerModel:
    contracts: list[ASTContract] = field(default_factory=list)
    raw_errors: list[dict[str, Any]] = field(default_factory=list)
    available: bool = False
    compiler_version: str | None = None
