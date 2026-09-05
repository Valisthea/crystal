"""Revert selectors, decoded.

A fuzzer that reports `0x8f4eb604` has told the operator nothing. The same
four bytes decoded to `ResignationDelayNotMet(address,uint256,uint256)` name
the precondition that never held — which is the whole point of the witness:
when a property is VACUOUS the reason must say *what* never succeeded, and a
revert reason is how the target says why.

Crystal's core is dependency-free on purpose, so the Keccak-256 needed for
custom-error selectors lives here rather than behind a `pip install`. It is
verified against the published test vectors in the test-suite, and the tables
it builds come only from errors the target itself declares (parsed sources or
its own ABI artifacts): Crystal does not guess signatures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# -- Keccak-256 --------------------------------------------------------------

_MASK64 = (1 << 64) - 1
_RATE = 136  # bytes; 1088-bit rate for a 512-bit capacity

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# Rotation offsets r[x][y] of the rho step.
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotate(value: int, shift: int) -> int:
    shift %= 64
    if not shift:
        return value
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f(lanes: list[int]) -> list[int]:
    """Keccak-f[1600] on 25 lanes indexed `x + 5 * y`."""
    for round_constant in _ROUND_CONSTANTS:
        # theta
        column = [
            lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20]
            for x in range(5)
        ]
        parity = [
            column[(x - 1) % 5] ^ _rotate(column[(x + 1) % 5], 1) for x in range(5)
        ]
        lanes = [lanes[index] ^ parity[index % 5] for index in range(25)]
        # rho and pi
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rotate(
                    lanes[x + 5 * y], _ROTATIONS[x][y]
                )
        # chi
        lanes = [
            moved[index] ^ (
                (~moved[(index % 5 + 1) % 5 + 5 * (index // 5)])
                & moved[(index % 5 + 2) % 5 + 5 * (index // 5)]
            )
            for index in range(25)
        ]
        # iota
        lanes[0] ^= round_constant
    return lanes


def keccak256(data: bytes) -> bytes:
    """The original Keccak-256 (pad10*1 with domain byte 0x01), as Ethereum uses."""
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE:
        padded.append(0x00)
    padded[-1] |= 0x80
    lanes = [0] * 25
    for offset in range(0, len(padded), _RATE):
        block = padded[offset:offset + _RATE]
        for lane in range(_RATE // 8):
            lanes[lane] ^= int.from_bytes(block[8 * lane:8 * lane + 8], "little")
        lanes = _keccak_f(lanes)
    return b"".join(lane.to_bytes(8, "little") for lane in lanes[:4])


def selector(signature: str) -> str:
    """`"Error(string)"` -> `"0x08c379a0"`."""
    return "0x" + keccak256(signature.encode("utf-8"))[:4].hex()


# -- Signatures --------------------------------------------------------------

ERROR_STRING = "0x08c379a0"   # Error(string)
PANIC_UINT = "0x4e487b71"     # Panic(uint256)

PANIC_CODES = {
    0x00: "generic compiler panic",
    0x01: "assert(false)",
    0x11: "arithmetic overflow/underflow",
    0x12: "division by zero",
    0x21: "enum conversion out of range",
    0x22: "corrupted storage byte array",
    0x31: "pop() on empty array",
    0x32: "array index out of bounds",
    0x41: "memory allocation overflow",
    0x51: "call to uninitialised internal function",
}

_ELEMENTARY_RE = re.compile(
    r"^(address|bool|string|bytes|bytes(?:[1-9]|[12]\d|3[0-2])|"
    r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|"
    r"168|176|184|192|200|208|216|224|232|240|248|256)?)$"
)
_LOCATION_RE = re.compile(r"\b(memory|calldata|storage|payable|indexed)\b")
_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{8}(?:[0-9a-fA-F]{2})*$")


def canonical_type(type_name: str) -> str | None:
    """ABI-canonical spelling of an elementary type, or None when Crystal cannot
    derive it (structs, enums, contract types need the compiler's view)."""
    cleaned = _LOCATION_RE.sub("", type_name or "")
    cleaned = re.sub(r"\s+", "", cleaned)
    if not cleaned:
        return None
    suffix = ""
    while cleaned.endswith("]"):
        opening = cleaned.rfind("[")
        if opening < 0:
            return None
        suffix = cleaned[opening:] + suffix
        cleaned = cleaned[:opening]
    if cleaned == "uint":
        cleaned = "uint256"
    elif cleaned == "int":
        cleaned = "int256"
    elif cleaned == "byte":
        cleaned = "bytes1"
    if not _ELEMENTARY_RE.match(cleaned):
        return None
    return cleaned + suffix


def canonical_signature(name: str, types) -> str | None:
    canonical = [canonical_type(item) for item in types]
    if any(item is None for item in canonical):
        return None
    return f"{name}({','.join(canonical)})"


@dataclass(frozen=True)
class SelectorTable:
    """Four-byte selector -> declared signature, for the errors a target declares."""

    entries: dict[str, str] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()

    def signature(self, raw: str) -> str | None:
        return self.entries.get(_normalise_selector(raw))

    def decode(self, data: str) -> str:
        """Decode revert data or a tool's revert label into a readable reason.

        Accepts a bare selector, full revert data, or an already-decoded label
        (returned untouched — Medusa, for one, names known errors itself).
        """
        text = (data or "").strip()
        if not text:
            return "revert without data"
        if not _HEX_RE.match(text):
            return text
        payload = text[2:] if text.lower().startswith("0x") else text
        head = "0x" + payload[:8].lower()
        arguments = payload[8:]
        if head == ERROR_STRING:
            return f'Error("{_decode_string(arguments)}")'
        if head == PANIC_UINT:
            code = int(arguments[:64] or "0", 16) if arguments else -1
            label = PANIC_CODES.get(code, "unknown panic code")
            return f"Panic({hex(code) if code >= 0 else '?'}: {label})"
        known = self.entries.get(head)
        if known:
            return known if not arguments else f"{known} {_summarise_arguments(arguments)}"
        return f"{head} (undecoded selector)"


def _normalise_selector(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return "0x" + text[:8]


def _decode_string(arguments: str) -> str:
    try:
        offset = int(arguments[:64], 16) * 2
        length = int(arguments[offset:offset + 64], 16) * 2
        raw = bytes.fromhex(arguments[offset + 64:offset + 64 + length])
        return raw.decode("utf-8", "replace")
    except (ValueError, IndexError):
        return "<undecodable string>"


def _summarise_arguments(arguments: str) -> str:
    words = [arguments[index:index + 64] for index in range(0, len(arguments), 64)]
    rendered = []
    for word in words[:4]:
        stripped = word.lstrip("0") or "0"
        rendered.append("0x" + stripped if len(stripped) > 1 else stripped)
    if len(words) > 4:
        rendered.append("...")
    return "(" + ", ".join(rendered) + ")"


_ERROR_DECLARATION_RE = re.compile(r"\berror\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*;")
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def declared_errors(contract) -> list[tuple[str, list[str]]]:
    """`(name, [types])` for the errors a contract declares — from the parsed
    model, or from its source file when the front-end recorded none."""
    found = [
        (error.name, [parameter.type_name for parameter in error.params])
        for error in (getattr(contract, "errors", ()) or ())
    ]
    if found:
        return found
    path = Path(getattr(contract, "path", "") or "")
    if not path.is_file():
        return []
    try:
        text = _COMMENT_RE.sub("", path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []
    for name, parameters in _ERROR_DECLARATION_RE.findall(text):
        types = []
        for parameter in parameters.split(","):
            words = parameter.split()
            if not words:
                continue
            types.append(" ".join(words[:-1]) if len(words) > 1 else words[0])
        found.append((name, types))
    return found


def selector_table(contracts=(), abi_files=()) -> SelectorTable:
    """Build the table from what the target declares: `error` declarations
    (parsed, or read from the source when the front-end recorded none) and,
    when an artifact tree exists, the ABI `error` entries of the named
    artifacts. Errors whose parameter types Crystal cannot canonicalise are
    listed in `skipped` rather than hashed from a guess."""
    entries = {ERROR_STRING: "Error(string)", PANIC_UINT: "Panic(uint256)"}
    skipped: list[str] = []
    for contract in contracts:
        for name, types in declared_errors(contract):
            signature = canonical_signature(name, types)
            if signature is None:
                skipped.append(f"{contract.name}.{name}")
                continue
            entries[selector(signature)] = signature
    for path in abi_files:
        for name, types in abi_errors(path):
            signature = canonical_signature(name, types)
            if signature is None:
                skipped.append(f"{Path(path).name}:{name}")
                continue
            entries[selector(signature)] = signature
    return SelectorTable(entries, tuple(dict.fromkeys(skipped)))


def abi_errors(path) -> list[tuple[str, list[str]]]:
    """`(name, [types])` for every `error` entry in a compiler artifact's ABI."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    abi = document.get("abi") if isinstance(document, dict) else None
    if not isinstance(abi, list):
        return []
    found = []
    for entry in abi:
        if not isinstance(entry, dict) or entry.get("type") != "error":
            continue
        types = [_abi_type(item) for item in entry.get("inputs") or ()]
        found.append((entry.get("name", ""), types))
    return found


def _abi_type(item) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("type", "").startswith("tuple"):
        inner = ",".join(_abi_type(component) for component in item.get("components") or ())
        return "(" + inner + ")" + item["type"][len("tuple"):]
    return item.get("internalType", "") if item.get("type") == "" else item.get("type", "")


def artifact_files(project, names) -> list[Path]:
    """Compiler artifacts named after `names` in a Foundry (`out/`) or Hardhat
    (`artifacts/`) tree, without walking anything else."""
    root = Path(project)
    wanted = {f"{name}.json" for name in names if name}
    found: list[Path] = []
    for tree in ("out", "artifacts"):
        base = root / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*.json"):
            if path.name in wanted and path.is_file() and not path.name.endswith(".dbg.json"):
                found.append(path)
    return found
