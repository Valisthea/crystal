"""Storage graph: declared layout plus per-access read/write edges.

v1 emitted one edge per (function, variable) pair with no location and no
layout. v2 records the access path and line for every statement that touches
storage, and approximates the declaration order/packing so that adjacent-slot
questions (upgrade safety, delegatecall collisions) can be asked downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import ir as I

WORD_BITS = 256

BIT_WIDTHS = {
    "bool": 8, "address": 160, "uint8": 8, "uint16": 16, "uint32": 32,
    "uint64": 64, "uint96": 96, "uint128": 128, "uint160": 160, "uint256": 256,
    "int8": 8, "int16": 16, "int32": 32, "int64": 64, "int128": 128,
    "int256": 256, "bytes32": 256, "bytes1": 8, "bytes4": 32, "bytes8": 64,
}


@dataclass(frozen=True)
class StorageEdge:
    function: str
    variable: str
    kind: str
    line: int = 0
    access_path: str = ""
    language: str = "solidity"


@dataclass
class StorageSlot:
    contract: str
    name: str
    type_name: str
    slot: int
    offset: int
    bits: int
    dynamic: bool = False
    constant: bool = False


@dataclass
class StorageGraph:
    edges: list[StorageEdge] = field(default_factory=list)
    layout: list[StorageSlot] = field(default_factory=list)

    def __iter__(self):
        return iter(self.edges)

    def __len__(self):
        return len(self.edges)

    def __bool__(self):
        return bool(self.edges or self.layout)

    def __getitem__(self, index):
        return self.edges[index]


def _bits_of(type_name: str) -> tuple[int, bool]:
    cleaned = re.sub(r"\s+", "", (type_name or "").lower())
    if cleaned.startswith("mapping") or cleaned.endswith("[]") or \
            cleaned in {"bytes", "string"} or cleaned.startswith(("storagemap",
                                                                  "storagedoublemap",
                                                                  "hashmap")):
        return WORD_BITS, True
    if cleaned in BIT_WIDTHS:
        return BIT_WIDTHS[cleaned], False
    if cleaned.startswith("uint") or cleaned.startswith("int"):
        digits = re.sub(r"\D", "", cleaned)
        return (int(digits) if digits else WORD_BITS), False
    return WORD_BITS, False


def build_layout(contracts) -> list[StorageSlot]:
    layout: list[StorageSlot] = []
    for contract in contracts:
        slot = 0
        offset = 0
        for variable in contract.state_vars:
            if variable.constant or variable.immutable:
                layout.append(StorageSlot(
                    contract.name, variable.name, variable.type_name,
                    -1, 0, 0, False, True,
                ))
                continue
            bits, dynamic = _bits_of(variable.type_name)
            if bits == WORD_BITS or offset + bits > WORD_BITS:
                if offset:
                    slot += 1
                    offset = 0
                layout.append(StorageSlot(
                    contract.name, variable.name, variable.type_name,
                    slot, 0, bits, dynamic,
                ))
                slot += 1
            else:
                layout.append(StorageSlot(
                    contract.name, variable.name, variable.type_name,
                    slot, offset, bits, dynamic,
                ))
                offset += bits
        # Packing is an approximation: inherited layout and structs are not
        # flattened, so slots are per-contract and comparable within a contract.
    return layout


def build_storage_graph(contracts) -> StorageGraph:
    graph = StorageGraph(layout=build_layout(contracts))

    for contract in contracts:
        state_names = {variable.name for variable in contract.state_vars}
        for function in contract.functions:
            qualified = f"{contract.name}.{function.name}"
            if function.ir is None:
                for name in sorted(function.reads):
                    graph.edges.append(StorageEdge(
                        qualified, f"{contract.name}.{name}", "read",
                        function.line, name, function.language,
                    ))
                for name in sorted(function.writes):
                    graph.edges.append(StorageEdge(
                        qualified, f"{contract.name}.{name}", "write",
                        function.line, name, function.language,
                    ))
                continue

            for statement in function.ir.walk():
                path = statement.target.text if statement.target else ""
                for name in statement.writes:
                    graph.edges.append(StorageEdge(
                        qualified, f"{contract.name}.{name}", "write",
                        statement.line, path or name, function.language,
                    ))
                for name in statement.reads:
                    if name in statement.writes:
                        continue
                    graph.edges.append(StorageEdge(
                        qualified, f"{contract.name}.{name}", "read",
                        statement.line, statement.text[:120], function.language,
                    ))
                if statement.kind == I.ASSEMBLY:
                    for name in sorted(state_names & set(statement.reads)):
                        graph.edges.append(StorageEdge(
                            qualified, f"{contract.name}.{name}",
                            "assembly-unresolved", statement.line,
                            statement.text[:120], function.language,
                        ))

    seen = set()
    unique = []
    for edge in graph.edges:
        key = (edge.function, edge.variable, edge.kind, edge.line, edge.access_path)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    graph.edges = sorted(unique, key=lambda x: (x.function, x.line, x.variable, x.kind))
    return graph
