"""What a Solidity `Name(...)` can be when it is not a call.

`Exp({mantissa: x})` builds a struct, `CToken(addr)` casts an address,
`uint256(x)` converts a value and `type(T).max` reads a compile-time constant.
None of them transfers control or has an effect, yet every one is spelled like
a call, and both Solidity front-ends used to record them as one. Downstream a
detector then grouped two entry points on "the same callee `Exp`" and reported
an asymmetry about a struct construction.

The catalog here is the evidence both front-ends consult before recording a
call. A name counts as a conversion only on declared evidence: an elementary
type, or a contract/interface/library/struct/enum the project declares. When
the project also declares a function or modifier by the same name Crystal
cannot tell which one `Name(...)` reaches, so the call is kept: a lost real
call is worse than a spurious one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Comments and string literals, in one left-to-right pass. A string is matched
# whole so a `//` inside it is not a comment; a quote inside a comment is
# consumed with the comment. Offsets are not preserved, which is fine here:
# the catalog wants names, not lines. `base.strip_comments` does preserve them
# but walks the text in Python, too slow to run over a whole project twice.
COMMENT_OR_STRING_RE = re.compile(
    r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|//[^\n]*|/\*.*?\*/', re.S
)


def _declaration_text(text: str) -> str:
    return COMMENT_OR_STRING_RE.sub(
        lambda m: m.group(0) if m.group(0)[0] in "\"'" else " ", text or ""
    )


# Elementary types spelled as a conversion: address(x), payable(x), uint256(x),
# bytes32(x), string(b), bytes(s), int(x), ufixed128x18(x).
ELEMENTARY_TYPE_RE = re.compile(
    r"^(?:address|payable|bool|string|byte|bytes(?:[1-9]|[12]\d|3[0-2])?"
    r"|u?int(?:\d{1,3})?|u?fixed(?:\d+x\d+)?)$"
)

# `type(T).max`, `type(C).creationCode`: a meta-type lookup, never a call.
META_TYPE_NAME = "type"

DECLARATION_RE = re.compile(
    r"\b(contract|interface|library|struct|enum|function|modifier)\s+([A-Za-z_]\w*)"
)
# `type Foo is uint256;` — Foo.wrap(x) / Foo.unwrap(x) are conversions too.
USER_VALUE_TYPE_RE = re.compile(r"\btype\s+([A-Za-z_]\w*)\s+is\b")
CONTRACT_KEYWORDS = {"contract", "interface", "library"}
STRUCT_KEYWORDS = {"struct", "enum"}
VALUE_TYPE_CONVERSIONS = {"wrap", "unwrap"}


@dataclass(frozen=True)
class TypeCatalog:
    """Names the project declares, grouped by what `Name(...)` would mean."""

    contracts: frozenset = frozenset()   # contract, interface and library names
    structs: frozenset = frozenset()     # struct and enum names
    value_types: frozenset = frozenset()  # user-defined value types (`type X is ...`)
    functions: frozenset = frozenset()   # function and modifier names: the ambiguity guard

    def merged(self, other: "TypeCatalog | None") -> "TypeCatalog":
        if other is None:
            return self
        return TypeCatalog(
            self.contracts | other.contracts,
            self.structs | other.structs,
            self.value_types | other.value_types,
            self.functions | other.functions,
        )

    def is_conversion(self, name: str) -> bool:
        """`name(...)` with no receiver builds or converts a value, calls nothing."""
        if ELEMENTARY_TYPE_RE.match(name):
            return True
        if name in self.functions:
            return False
        if name == META_TYPE_NAME:
            return True
        return name in self.contracts or name in self.structs

    def is_qualified_conversion(self, receiver: str, name: str) -> bool:
        """`Owner.Name(...)`: a struct literal or enum conversion spelled through
        its owner (`Lib.S({a: 1})`), or a user-defined value type wrapped."""
        if name in self.functions:
            return False
        if receiver in self.value_types and name in VALUE_TYPE_CONVERSIONS:
            return True
        return receiver in self.contracts and name in self.structs


EMPTY_CATALOG = TypeCatalog()


def catalog_from_text(text: str) -> TypeCatalog:
    """Declared names of one source file, read from comment-stripped text."""
    contracts: set[str] = set()
    structs: set[str] = set()
    functions: set[str] = set()
    stripped = _declaration_text(text)
    for keyword, name in DECLARATION_RE.findall(stripped):
        if keyword in CONTRACT_KEYWORDS:
            contracts.add(name)
        elif keyword in STRUCT_KEYWORDS:
            structs.add(name)
        else:
            functions.add(name)
    value_types = set(USER_VALUE_TYPE_RE.findall(stripped))
    return TypeCatalog(
        frozenset(contracts), frozenset(structs), frozenset(value_types),
        frozenset(functions),
    )


def catalog_from_paths(paths) -> TypeCatalog:
    """Declared names across a whole project, so a cast to a contract declared
    in another file (`CToken(cToken)` in `Comptroller.sol`) resolves too."""
    catalog = EMPTY_CATALOG
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        catalog = catalog.merged(catalog_from_text(text))
    return catalog
