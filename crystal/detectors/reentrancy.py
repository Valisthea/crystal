"""Ordering-based reentrancy signal.

The regex parser could not see the order of operations. With the statement IR
Crystal can say precisely: an external call at line N is followed by a write to
state read before the call at line M > N. That ordering is the mechanism, and
the trace is what a reviewer needs to falsify it.

Not every call a front-end labels "external" hands execution to code this
contract does not control. `Math.min(a, b)` runs a library routine,
`Lib.S({..})` builds a struct in memory, `Base.f()` runs inherited code and
`UD60x18.wrap(x)` converts a value type: none of them can reach an attacker.
So before a call counts as a control transfer its receiver is resolved against
what the project declares — the receiver's declared type, the libraries and
their bodies, the structs, the inheritance and the interface bindings. Only a
call on an address- or contract-typed value, a low-level call, a value transfer
or a library that can reach such a value transfers control. A receiver that
cannot be resolved keeps its signal at reduced confidence, and the evidence
says so rather than dropping it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..graphs.binding import build_bindings
from ..ir import (
    EXTERNAL_CALL,
    EXTERNAL_CALL_KINDS,
    INTERNAL_CALL,
    STATICCALL,
    VALUE_TRANSFER,
    flatten_events,
)
from .base import DetectorSignal, REENTRANCY_GUARDS, has_modifier, signal

DETECTOR = "reentrancy-ordering"

FALSIFICATION = (
    "Is the external callee trusted and immutable (no attacker-controlled code)?",
    "Does a guard elsewhere on the path already prevent re-entry?",
    "Is the post-call write idempotent, so a nested call cannot benefit?",
    "Can the attacker actually reach the external call with a contract account?",
    "Does the resolved receiver really hold code this contract does not control?",
)

REFERENCES = (
    "SWC-107", "CWE-841", "checks-effects-interactions",
)

HIGH_RISK_CALLS = {"call", "delegatecall"}

# Verdicts of the receiver resolution.
TRANSFERS = "transfers"
NO_TRANSFER = "no-transfer"
UNRESOLVED = "unresolved"

# Confidence adjustments. Each one is named in the evidence it accompanies.
UNRESOLVED_PENALTY = 0.20
OPAQUE_LIBRARY_PENALTY = 0.10
STATIC_PENALTY = 0.40
CONSTRUCTOR_PENALTY = 0.30

_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_TYPE_NAME = re.compile(r"[A-Za-z_$][\w$.]*")
# The member chain a call binds to: `a >= b.c(x)` calls `c` on `b`, so the
# receiver is the trailing chain `b`, whatever the front-end put before it.
_CHAIN_TAIL = re.compile(
    r"[A-Za-z_$][\w$]*"
    r"(?:\s*(?:\.\s*[A-Za-z_$][\w$]*|\[[^\[\]]*\]|\((?:[^()]|\([^()]*\))*\)))*\s*$"
)
_LOCATIONS = {"memory", "storage", "calldata"}
_VALUE_PREFIXES = ("uint", "int", "bool", "bytes", "string", "fixed", "ufixed")
_CONTRACT_KINDS = {"contract", "interface", "abstract"}
_BUILTIN_ROOTS = {"this", "super", "msg", "tx", "block", "abi"}
_MAX_DEPTH = 3


@dataclass(frozen=True)
class ControlTransfer:
    """What a call's receiver resolved to, and whether execution can leave."""

    verdict: str
    detail: str
    # Why the callee cannot change state (a `view`/`pure` callee, a
    # `staticcall`); empty when it can.
    static: str = ""
    # The transfer is inferred through a library whose body is not in the
    # project, so it is a possibility rather than an observed call.
    opaque_library: bool = False

    @property
    def transfers(self) -> bool:
        return self.verdict == TRANSFERS


# ---------------------------------------------------------------------------
# Type text helpers
# ---------------------------------------------------------------------------

_MAPPING = re.compile(r"\bmapping\s*\(")


def _split_type(text: str) -> tuple[str, int]:
    """Element type and the number of `[..]` hops needed to reach it.

    `mapping(address => IERC20[])` -> (`IERC20`, 2); `Lib.S memory` -> (`Lib.S`, 0).
    A container that is not fully indexed is still a container, and a member
    called on it (`push`, `pop`) is a builtin, not a message call.
    """
    flat = " ".join((text or "").split())
    dims = len(_MAPPING.findall(flat))
    if dims and "=>" in flat:
        flat = flat.rsplit("=>", 1)[-1].strip().rstrip(")").strip()
    while flat.endswith("]") and "[" in flat:
        flat = flat[:flat.rfind("[")].strip()
        dims += 1
    base = " ".join(token for token in flat.split() if token not in _LOCATIONS)
    return base, dims


def _base_type(text: str) -> str:
    """`mapping(address => IERC20[])` -> `IERC20`; `Lib.S memory` -> `Lib.S`."""
    return _split_type(text)[0]


def _is_value_type(base: str) -> bool:
    return base.startswith(_VALUE_PREFIXES)


def _declared_in(text: str, name: str) -> str | None:
    """Type of `name` in a declaration such as `(bool ok, IERC20 t) = ..`."""
    for piece in text.strip().strip("()").split(","):
        tokens = [token for token in piece.split() if token not in _LOCATIONS]
        if len(tokens) >= 2 and tokens[-1] == name:
            return " ".join(tokens[:-1])
    return None


def _member_type(struct, member: str) -> str | None:
    for entry in struct.members:
        tokens = entry.rstrip(";").split()
        if len(tokens) >= 2 and tokens[-1] == member:
            return " ".join(tokens[:-1])
    return None


def _skip_balanced(text: str, open_char: str, close_char: str) -> str:
    depth = 0
    for index, char in enumerate(text):
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[index + 1:].lstrip()
    return ""


# ---------------------------------------------------------------------------
# Receiver resolution
# ---------------------------------------------------------------------------

class CallResolver:
    """Resolves a call's receiver to what the project declares it to be.

    Built once per detector run. Every answer is a `ControlTransfer` whose
    detail is written for the reviewer: which declaration decided, and why
    that declaration can or cannot put foreign code on the stack.
    """

    def __init__(self, contracts):
        self.contracts = list(contracts)
        self.defs: dict[str, list] = {}
        self.struct_types: dict[str, object] = {}
        # Names that appear in declared types anywhere (state, params,
        # returns), each with the declaration that proves it is a type.
        self.type_names: dict[str, str] = {}
        # User-defined value types: only they have `T.wrap(x)` / `T.unwrap(x)`.
        self.udvt: set[str] = set()
        for contract in self.contracts:
            self.defs.setdefault(contract.name, []).append(contract)
            for user_type in contract.types:
                self.struct_types.setdefault(user_type.name, user_type)
                if contract.kind != "file":
                    self.struct_types[f"{contract.name}.{user_type.name}"] = user_type
            for variable in contract.state_vars:
                self._note_type(variable.type_name)
                self._note_type(variable.value_type)
            for function in contract.functions:
                for parameter in function.params + function.returns:
                    self._note_type(parameter.type_name)
                if function.ir is None:
                    continue
                for call in function.ir.calls():
                    receiver = (call.receiver or "").strip()
                    if call.callee in {"wrap", "unwrap"} and _IDENT.fullmatch(receiver):
                        self.udvt.add(receiver)
        self.bindings = build_bindings(self.contracts)
        self._bases: dict[str, list[str]] = {}

    # -- declarations ------------------------------------------------------
    def _note_type(self, text: str) -> None:
        base = _base_type(text)
        if not base or _is_value_type(base) or base.startswith(("address", "mapping", "function")):
            return
        head = base.split()[0]
        if _TYPE_NAME.fullmatch(head):
            self.type_names.setdefault(head, base)
            self.type_names.setdefault(head.split(".", 1)[0], base)

    def bases(self, name: str) -> list[str]:
        """Transitive bases, including ones the project does not define."""
        if name not in self._bases:
            out: list[str] = []
            queue, seen = [name], {name}
            while queue:
                for definition in self.defs.get(queue.pop(0), ()):
                    for base in definition.bases:
                        if base not in seen:
                            seen.add(base)
                            out.append(base)
                            queue.append(base)
            self._bases[name] = out
        return self._bases[name]

    def kinds(self, name: str) -> set[str]:
        return {definition.kind for definition in self.defs.get(name, ())}

    def functions_named(self, type_name: str, callee: str):
        for owner in [type_name] + self.bases(type_name):
            for definition in self.defs.get(owner, ()):
                for function in definition.functions:
                    if function.name == callee:
                        yield definition, function

    def public_state_named(self, type_name: str, callee: str):
        for owner in [type_name] + self.bases(type_name):
            for definition in self.defs.get(owner, ()):
                for variable in definition.state_vars:
                    if variable.name == callee and variable.visibility == "public":
                        return definition, variable
        return None

    def state_type(self, contract, name: str) -> str | None:
        for owner in [contract.name] + self.bases(contract.name):
            for definition in self.defs.get(owner, ()):
                for variable in definition.state_vars:
                    if variable.name == name:
                        return variable.value_type or variable.type_name
        return None

    @staticmethod
    def local_type(function, name: str) -> str | None:
        for parameter in function.params + function.returns:
            if parameter.name == name:
                return parameter.type_name
        if function.ir is None:
            return None
        for statement in function.ir.walk():
            if statement.kind == "var_decl" and statement.target is not None:
                found = _declared_in(statement.target.text, name)
                if found:
                    return found
        return None

    @staticmethod
    def import_path(contract, name: str) -> str | None:
        for path in getattr(contract, "imports", ()):
            if Path(path).stem == name:
                return path
        return None

    def is_type_level(self, root: str, contract) -> bool:
        return (root in self.defs or root in self.struct_types or root in self.udvt
                or root in self.type_names
                or self.import_path(contract, root) is not None)

    def struct(self, name: str, contract=None):
        """The struct or enum `name` denotes, preferring the one in scope.

        Several contracts may each declare a `TRequest`; a bare name inside
        `contract` means the one it or a base declares.
        """
        if contract is not None and "." not in name:
            for owner in [contract.name] + self.bases(contract.name):
                found = self.struct_types.get(f"{owner}.{name}")
                if found is not None:
                    return found
        found = self.struct_types.get(name)
        if found is None and "." in name:
            found = self.struct_types.get(name.rsplit(".", 1)[-1])
        return found

    def using_library(self, contract, type_name: str, callee: str) -> str | None:
        """Library a `using` directive in scope attaches to `type_name`."""
        wanted = type_name.rsplit(".", 1)[-1]
        candidates: list[str] = []
        for owner in [contract.name] + self.bases(contract.name):
            for definition in self.defs.get(owner, ()):
                for library, target in definition.using_for:
                    target_base = _base_type(target)
                    if target == "*" or target_base == type_name \
                            or target_base.rsplit(".", 1)[-1] == wanted:
                        candidates.append(library)
        for library in candidates:
            if any(True for _ in self.functions_named(library, callee)):
                return library
        return candidates[0] if candidates else None

    def type_class(self, base: str) -> str:
        """address | value | udvt | contract | library | struct | external."""
        if base.startswith("address"):
            return "address"
        if _is_value_type(base):
            return "value"
        kinds = self.kinds(base)
        if kinds & _CONTRACT_KINDS:
            return "contract"
        if kinds == {"library"}:
            return "library"
        if base in self.udvt:
            return "udvt"
        if self.struct(base) is not None or "." in base:
            # A dotted type is a member type of a library or contract; a
            # contract type is never dotted.
            return "struct"
        # Declared with a name this project does not define: a contract or
        # interface from a dependency.
        return "external"

    # -- resolution --------------------------------------------------------
    def transfer(self, call, function, contract, depth: int = 0,
                 seen: set | None = None) -> ControlTransfer:
        seen = seen if seen is not None else set()
        receiver = (call.receiver or "").strip()
        tail = _CHAIN_TAIL.search(receiver)
        if tail is not None and tail.start() > 0:
            receiver = receiver[tail.start():].strip()
        match = _IDENT.match(receiver)
        root = match.group(0) if match else ""
        rest = receiver[match.end():].strip() if match else receiver
        declared = None
        if root and root not in _BUILTIN_ROOTS:
            declared = self.local_type(function, root) or self.state_type(contract, root)

        if call.kind != EXTERNAL_CALL:
            # A low-level call, delegatecall, staticcall or value transfer
            # hands control away whatever the receiver is — unless the
            # "receiver" is a type name (`Lib.call(..)`), which is a library.
            if declared is None and root and not rest and self.is_type_level(root, contract):
                return self._type_level(root, call, function, contract, depth, seen)
            return self._low_level(call, receiver)

        if not root:
            return ControlTransfer(
                UNRESOLVED,
                f"receiver `{receiver}` of `{call.callee}` has no identifiable root; "
                "kept as a possible external call",
            )
        if call.callee.endswith("]"):
            return ControlTransfer(
                NO_TRANSFER,
                f"`new {receiver}.{call.callee}(..)` allocates an array in memory; "
                "no message call",
            )
        if declared is None and _is_value_type(root):
            return ControlTransfer(
                NO_TRANSFER,
                f"`{root}.{call.callee}` is a builtin on a value type, not a message call",
            )
        if root == "this":
            return ControlTransfer(
                NO_TRANSFER,
                f"`this.{call.callee}` is a self-call: only this contract's own code runs",
            )
        if root == "super":
            return ControlTransfer(NO_TRANSFER, f"`super.{call.callee}` runs inherited code")
        if root in {"msg", "tx"}:
            if receiver.startswith(("msg.sender", "tx.origin")):
                return ControlTransfer(
                    TRANSFERS, f"receiver `{receiver}` is an address the caller chooses",
                )
            return ControlTransfer(
                NO_TRANSFER, f"`{receiver}.{call.callee}` is a builtin, not a message call",
            )
        if root in {"abi", "block"}:
            return ControlTransfer(
                NO_TRANSFER, f"`{receiver}.{call.callee}` is a builtin, not a message call",
            )
        if declared is not None:
            return self._typed(root, declared, rest, call, function, contract, depth, seen)
        if rest.startswith("("):
            return self._cast_or_call(root, rest, call, function, contract, depth, seen)
        if rest:
            return ControlTransfer(
                UNRESOLVED,
                f"receiver `{receiver}` starts from `{root}`, which is not a declared "
                "variable; kept as a possible external call",
            )
        return self._type_level(root, call, function, contract, depth, seen)

    @staticmethod
    def _low_level(call, receiver: str) -> ControlTransfer:
        if call.kind == STATICCALL:
            return ControlTransfer(
                TRANSFERS, f"low-level `staticcall` on `{receiver}`",
                static="a `staticcall` callee cannot change state, so it cannot "
                       "re-enter state-changing code",
            )
        if call.kind == VALUE_TRANSFER:
            return ControlTransfer(
                TRANSFERS,
                f"`{receiver}.{call.callee}` is a transfer: the receiver's code runs",
            )
        return ControlTransfer(
            TRANSFERS,
            f"low-level `{call.callee}` on `{receiver}` hands execution to whatever "
            "code sits there",
        )

    def _follow(self, current: str, dims: int, chain: str,
                contract=None) -> tuple[str, int, str, str]:
        """Follow `.field`, `[index]` and view-call hops from a `current` value.

        `dims` is how many container dimensions of `current` are still not
        indexed. Returns (type reached, dims left, stop, member). `stop` is
        empty when the chain was fully typed; `call` when a hop is a call
        that is not a project view function (control leaves there);
        `builtin` when a member is applied to a container (`push`, `pop`);
        `member` when a member could not be typed; `syntax` when the text is
        not a plain chain.
        """
        while chain:
            if chain[0] == "[":
                chain = _skip_balanced(chain, "[", "]")
                dims = max(0, dims - 1)
                continue
            if chain[0] == "(":
                return current, dims, "call", ""
            if chain[0] == ".":
                member_match = _IDENT.match(chain, 1)
                if member_match is None:
                    return current, dims, "syntax", ""
                member = member_match.group(0)
                chain = chain[member_match.end():].lstrip()
                if chain.startswith("("):
                    if dims:
                        return current, dims, "builtin", member
                    returned = self._view_return(current, member)
                    if returned is None:
                        return current, dims, "call", member
                    current, dims = returned
                    chain = _skip_balanced(chain, "(", ")")
                    continue
                if dims:
                    return current, dims, "member", member
                struct = self.struct(current, contract)
                member_type = _member_type(struct, member) if struct is not None else None
                if member_type is None:
                    return current, dims, "member", member
                current, dims = _split_type(member_type)
                continue
            return current, dims, "syntax", ""
        return current, dims, "", ""

    def _view_return(self, type_name: str, member: str) -> tuple[str, int] | None:
        """Return type of `type_name.member()` when that is a project view function.

        A view hop runs under STATICCALL, so what matters for control is the
        value it returns and what is then called on it.
        """
        if self.type_class(type_name) != "contract":
            return None
        for _, target in self.functions_named(type_name, member):
            if target.mutability in {"view", "pure"} and len(target.returns) == 1 \
                    and target.returns[0].type_name:
                return _split_type(target.returns[0].type_name)
            return None
        getter = self.public_state_named(type_name, member)
        if getter is not None and not getter[1].is_mapping and "[" not in getter[1].type_name:
            return _split_type(getter[1].type_name)
        return None

    def _typed(self, root, declared, rest, call, function, contract, depth, seen):
        base, dims = _split_type(declared)
        reached, dims, stop, member = (
            self._follow(base, dims, rest, contract) if rest else (base, dims, "", "")
        )
        if stop == "builtin" or (not stop and dims):
            builtin = member or call.callee
            return ControlTransfer(
                NO_TRANSFER,
                f"receiver `{root}{rest}` is a container (`{declared}`); `.{builtin}` is a "
                "builtin on it, not a message call",
            )
        if stop == "call":
            hop = f"{root}.{member}(..)" if member else f"{root}(..)"
            if self.type_class(reached) in {"address", "contract", "external"}:
                return ControlTransfer(
                    TRANSFERS,
                    f"receiver `{root}{rest}` begins with the call `{hop}` on `{root}` "
                    f"(declared `{declared}`): control leaves at that first hop",
                )
            return ControlTransfer(
                UNRESOLVED,
                f"receiver `{root}{rest}` calls `{hop}` on `{root}` (declared `{declared}`), "
                "whose result is not typed; kept as a possible external call",
            )
        if stop == "member":
            return ControlTransfer(
                UNRESOLVED,
                f"`{root}` is declared `{declared}`; member `{member}` of `{reached}` is not "
                "typed in this project, so the receiver could not be resolved",
            )
        if stop:
            return ControlTransfer(
                UNRESOLVED,
                f"receiver `{root}{rest}` is not a plain member chain on `{root}` (declared "
                f"`{declared}`); kept as a possible external call",
            )
        expr = f"{root}{rest}"
        origin = (
            f"receiver `{root}` is declared `{declared}`" if expr == root
            else f"receiver `{expr}` is `{reached}` (from `{root}`, declared `{declared}`)"
        )
        return self._classify(origin, reached, call, function, contract, depth, seen)

    def _classify(self, origin, base, call, function, contract, depth, seen):
        """Decide for a receiver whose type is `base`; `origin` says how it was found."""
        klass = self.type_class(base)
        if klass == "address":
            return ControlTransfer(
                TRANSFERS, f"{origin}: any code can sit behind an address",
            )
        if klass in {"value", "udvt"}:
            library = self.using_library(contract, base, call.callee)
            if library and library in self.defs:
                return self._library(library, call, function, contract, depth, seen,
                                     via=f"{origin}; `using {library} for {base}`")
            what = "a user-defined value type" if klass == "udvt" else "a value type"
            return ControlTransfer(
                NO_TRANSFER,
                f"{origin}, {what}; `.{call.callee}` can only be a `using .. for` library "
                "routine, and a value cannot hold code",
            )
        if klass == "contract":
            return self._contract_typed(origin, base, call)
        if klass == "struct":
            library = self.using_library(contract, base, call.callee)
            if library and library in self.defs:
                return self._library(library, call, function, contract, depth, seen,
                                     via=f"{origin}; `using {library} for {base}`")
            if library:
                return ControlTransfer(
                    UNRESOLVED,
                    f"{origin}, a struct; `.{call.callee}` is a `using {library} for "
                    f"{base}` routine whose body is outside this project",
                )
            return ControlTransfer(
                UNRESOLVED,
                f"{origin}, a struct; `.{call.callee}` must be a library routine, but no "
                "`using` directive in scope names it",
            )
        if klass == "library":
            return ControlTransfer(
                UNRESOLVED,
                f"{origin}, a library type, which is not callable; kept as a possible "
                "external call",
            )
        return ControlTransfer(
            TRANSFERS,
            f"{origin}, a contract type this project does not define; treated as an "
            "external call",
        )

    def _contract_typed(self, origin, base, call) -> ControlTransfer:
        static = ""
        for definition, target in self.functions_named(base, call.callee):
            if target.mutability in {"view", "pure"}:
                static = (
                    f"callee `{definition.name}.{call.callee}` is declared "
                    f"`{target.mutability}`: Solidity (>=0.5) issues STATICCALL for it, so "
                    "the callee cannot change state or re-enter state-changing code"
                )
            break
        else:
            getter = self.public_state_named(base, call.callee)
            if getter is not None:
                static = (
                    f"callee `{getter[0].name}.{call.callee}` is a public state variable "
                    "getter: Solidity (>=0.5) issues STATICCALL for it, so the callee "
                    "cannot change state or re-enter state-changing code"
                )
        implementations = [
            name for name in self.bindings.implementations(base) if name != base
        ]
        kind = "an interface" if "interface" in self.kinds(base) else "a contract"
        detail = f"{origin}, {kind} in this project"
        if implementations:
            detail += "; implemented by " + ", ".join(implementations[:4])
        return ControlTransfer(TRANSFERS, detail, static=static)

    def _cast_or_call(self, root, rest, call, function, contract, depth, seen):
        receiver = root + rest
        after = _skip_balanced(rest, "(", ")")
        if root in {"address", "payable"}:
            return ControlTransfer(
                TRANSFERS,
                f"receiver `{receiver}` is an address expression: any code can sit behind it",
            )
        own = next(iter(self.functions_named(contract.name, root)), None)
        if own is not None:
            returns = own[1].returns
            if len(returns) == 1 and returns[0].type_name:
                return self._typed(receiver, returns[0].type_name, after, call,
                                   function, contract, depth, seen)
            return ControlTransfer(
                UNRESOLVED,
                f"receiver `{receiver}` is the result of `{root}()`, whose return type "
                "is not tracked; kept as a possible external call",
            )
        klass = self.type_class(root)
        if klass == "contract":
            return self._contract_typed(
                f"`{receiver}` casts an address to `{root}`", root, call,
            )
        if klass == "external":
            return ControlTransfer(
                TRANSFERS,
                f"`{receiver}` casts an address to `{root}`, a contract type this project "
                "does not define; the code behind it is not visible",
            )
        if klass in {"value", "udvt"}:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{receiver}` is a value; `.{call.callee}` can only be a `using .. for` "
                "library routine",
            )
        return ControlTransfer(
            UNRESOLVED,
            f"receiver `{receiver}` could not be resolved ({root} is {klass}); kept as a "
            "possible external call",
        )

    def _type_level(self, root, call, function, contract, depth, seen):
        """`Root.callee(..)` where `Root` is not a variable."""
        qualified = f"{root}.{call.callee}"
        kinds = self.kinds(root)
        if "library" in kinds:
            return self._library(root, call, function, contract, depth, seen)
        if root == contract.name or root in self.bases(contract.name):
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}` names a function of this contract or an inherited one: "
                "it runs internally",
            )
        user_type = self.struct_types.get(qualified)
        if user_type is not None:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}` is a {user_type.kind} constructor: it builds a value in memory",
            )
        if kinds & _CONTRACT_KINDS:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}` is a type-qualified reference to `{root}`; a contract type "
                "cannot be called without an instance, so no message call occurs",
            )
        if root in self.struct_types:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}` is a member of user type `{root}`, not a message call",
            )
        if root in self.udvt:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}`: `{root}` is a user-defined value type (wrap/unwrap "
                "conversions appear in this project); a type-level member is not a message call",
            )
        # An imported name is resolved before the type-name rule: a library
        # such as `SafeERC20` is a type namespace too, and only its arguments
        # tell whether it can reach external code.
        path = self.import_path(contract, root)
        if path is not None:
            return self._opaque_library(root, path, call, function, contract)
        example = self.type_names.get(root)
        if example is not None:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}`: `{root}` is a type name in this project (it appears in the "
                f"declared type `{example}`), so this is a type-level member (constructor "
                "or conversion), not a message call",
            )
        return ControlTransfer(
            UNRESOLVED,
            f"receiver `{root}` is not declared as a variable or type anywhere in this "
            "project; kept as a possible external call",
        )

    def _library(self, library, call, function, contract, depth, seen, via=""):
        """A call into a library the project defines: read its body."""
        qualified = f"{library}.{call.callee}"
        user_type = self.struct_types.get(qualified)
        if user_type is not None:
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}` is a {user_type.kind} constructor: it builds a value in memory",
            )
        found = next(iter(self.functions_named(library, call.callee)), None)
        if found is None:
            return ControlTransfer(
                UNRESOLVED,
                f"`{qualified}`: library `{library}` is in this project but declares no "
                f"`{call.callee}`; kept as a possible external call",
            )
        holder, target = found
        key = (holder.name, target.name)
        if key in seen:
            return ControlTransfer(
                NO_TRANSFER, f"`{qualified}` is already being resolved on this path",
            )
        seen.add(key)
        result = self._body(holder, target, depth + 1, seen)
        prefix = f"{via}: " if via else ""
        return ControlTransfer(result.verdict, prefix + result.detail,
                               static=result.static, opaque_library=result.opaque_library)

    def _body(self, holder, function, depth, seen) -> ControlTransfer:
        label = f"{holder.name}.{function.name}"
        if function.ir is None:
            return ControlTransfer(
                UNRESOLVED, f"`{label}` has no statement IR; kept as a possible external call",
            )
        if function.ir.has_assembly:
            return ControlTransfer(
                UNRESOLVED,
                f"`{label}` contains inline assembly, which can `call` without the parser "
                "seeing it; kept as a possible external call",
            )
        if depth > _MAX_DEPTH:
            return ControlTransfer(
                UNRESOLVED, f"`{label}`: resolution stopped at depth {_MAX_DEPTH}",
            )
        unresolved: str | None = None
        for inner in function.ir.calls():
            if inner.kind in EXTERNAL_CALL_KINDS:
                verdict = self.transfer(inner, function, holder, depth, seen)
                if verdict.transfers:
                    return ControlTransfer(
                        TRANSFERS,
                        f"`{label}` reaches `{inner.text[:80]}` at line {inner.line}: "
                        f"{verdict.detail}",
                        static=verdict.static, opaque_library=verdict.opaque_library,
                    )
                if verdict.verdict == UNRESOLVED and unresolved is None:
                    unresolved = (
                        f"`{label}` calls `{inner.text[:80]}` at line {inner.line}, which "
                        f"could not be resolved: {verdict.detail}"
                    )
            elif inner.kind == INTERNAL_CALL and not inner.receiver:
                found = next(iter(self.functions_named(holder.name, inner.callee)), None)
                if found is None:
                    continue
                key = (found[0].name, found[1].name)
                if key in seen:
                    continue
                seen.add(key)
                verdict = self._body(found[0], found[1], depth + 1, seen)
                if verdict.transfers:
                    return ControlTransfer(
                        TRANSFERS, f"`{label}` -> {verdict.detail}",
                        static=verdict.static, opaque_library=verdict.opaque_library,
                    )
                if verdict.verdict == UNRESOLVED and unresolved is None:
                    unresolved = f"`{label}` -> {verdict.detail}"
        if unresolved:
            return ControlTransfer(UNRESOLVED, unresolved)
        return ControlTransfer(
            NO_TRANSFER,
            f"`{label}` is a library routine in this project whose body makes no external call",
        )

    def _opaque_library(self, root, path, call, function, contract) -> ControlTransfer:
        """A library the project imports but does not define.

        Its body is invisible, so the only thing that can be checked is what
        it is given: a library reaches external code only through an
        address- or contract-typed value it receives.
        """
        qualified = f"{root}.{call.callee}"
        arguments = call.arguments
        if len(arguments) == 1 and arguments[0].text.lstrip().startswith("{"):
            return ControlTransfer(
                NO_TRANSFER,
                f"`{qualified}({{..}})` is a named-field literal: it builds a struct in "
                f"memory (`{root}` comes from `{path}`)",
            )
        reaches: list[str] = []
        unresolved: list[str] = []
        for argument in arguments:
            verdict, what = self._argument(argument, function, contract)
            if verdict == TRANSFERS:
                reaches.append(what)
            elif verdict == UNRESOLVED:
                unresolved.append(what)
        if reaches:
            return ControlTransfer(
                TRANSFERS,
                f"`{qualified}` is a library outside this project (`{path}`) that receives "
                f"{reaches[0]}; a library can only reach external code through such an "
                "argument, and whether this one does is not visible",
                opaque_library=True,
            )
        if unresolved:
            return ControlTransfer(
                UNRESOLVED,
                f"`{qualified}` is a library outside this project (`{path}`); argument "
                f"{unresolved[0]} could not be typed, so whether it reaches external code "
                "is not visible",
            )
        return ControlTransfer(
            NO_TRANSFER,
            f"`{qualified}` is a library outside this project (`{path}`); every argument "
            "is value-typed, so it cannot reach code behind an address",
        )

    def _argument(self, argument, function, contract) -> tuple[str, str]:
        text = argument.text.strip()
        if "msg.sender" in text or "tx.origin" in text:
            return TRANSFERS, "`msg.sender`" if "msg.sender" in text else "`tx.origin`"
        match = _IDENT.match(text)
        if match is None:
            return NO_TRANSFER, ""
        root = match.group(0)
        rest = text[match.end():].strip()
        if root in {"true", "false"} or (root == "this" and not rest):
            return NO_TRANSFER, ""
        if root in {"this", "super"} and rest.startswith("."):
            # The result of one of this contract's own functions.
            member_match = _IDENT.match(rest, 1)
            member = member_match.group(0) if member_match else ""
            own = next(iter(self.functions_named(contract.name, member)), None) if member else None
            if own is not None and len(own[1].returns) == 1 and own[1].returns[0].type_name:
                return self._argument_type(
                    f"{root}.{member}(..)", _base_type(own[1].returns[0].type_name), text,
                )
            return UNRESOLVED, f"`{text[:60]}`"
        if rest.startswith("("):
            if root == "address":
                inner = rest[1:].strip()
                if inner.startswith("this"):
                    return NO_TRANSFER, ""
                return TRANSFERS, f"`{text[:60]}` (an address)"
            if root == "payable":
                return TRANSFERS, f"`{text[:60]}` (an address)"
            klass = self.type_class(root)
            if klass in {"value", "udvt"}:
                return NO_TRANSFER, ""
            if klass == "contract":
                return TRANSFERS, f"`{text[:60]}` (cast to `{root}`)"
            own = next(iter(self.functions_named(contract.name, root)), None)
            if own is not None:
                returns = own[1].returns
                if len(returns) == 1 and returns[0].type_name:
                    return self._argument_type(root, _base_type(returns[0].type_name), text)
                return UNRESOLVED, f"`{text[:60]}`"
            if klass == "external":
                return TRANSFERS, f"`{text[:60]}` (cast to `{root}`, a contract type outside this project)"
            return UNRESOLVED, f"`{text[:60]}`"
        declared = self.local_type(function, root) or self.state_type(contract, root)
        if declared is None:
            if self.is_type_level(root, contract):
                return NO_TRANSFER, ""
            return UNRESOLVED, f"`{root}`"
        base, dims = _split_type(declared)
        if rest:
            base, dims, stop, _member = self._follow(base, dims, rest, contract)
            if stop == "syntax" and self.type_class(base) in {"value", "udvt"}:
                # Arithmetic over a value: still a value.
                return NO_TRANSFER, ""
            if stop:
                return UNRESOLVED, f"`{text[:60]}`"
        # A container of addresses handed to a library is as good as an
        # address, so the dimensions left do not change the answer.
        return self._argument_type(root, base, text)

    def _argument_type(self, root, base, text) -> tuple[str, str]:
        klass = self.type_class(base)
        if klass in {"address", "contract", "external"}:
            return TRANSFERS, f"`{root}` (`{base}`)"
        if klass in {"value", "udvt"}:
            return NO_TRANSFER, ""
        if klass == "struct":
            struct = self.struct(base)
            if struct is None:
                return UNRESOLVED, f"`{root}` (`{base}`)"
            for entry in struct.members:
                tokens = entry.rstrip(";").split()
                if len(tokens) >= 2 and self.type_class(
                        _base_type(" ".join(tokens[:-1]))) in {"address", "contract", "external"}:
                    return TRANSFERS, f"`{root}` (struct `{base}` holding an address)"
            return NO_TRANSFER, ""
        return UNRESOLVED, f"`{root}` (`{base}`)"


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def _trace(events, call_index, writes) -> tuple[str, ...]:
    window = [events[call_index]] + [events[i] for i in writes]
    return tuple(
        f"L{event.line} {event.kind}"
        + (f"[{event.variable}]" if event.variable else "")
        + f": {event.detail}"
        for event in window
    )


def analyze_function(function, contract, cross_readers, resolver) -> list[DetectorSignal]:
    if function.ir is None or not function.ir.statements:
        return []
    events = flatten_events(function.ir)
    # `flatten_events` emits one call event per statement that carries a call,
    # in walk order, so the two sequences align one to one.
    calls = [statement.call for statement in function.ir.walk() if statement.call is not None]
    call_positions = [
        index for index, event in enumerate(events)
        if event.kind in {"external_call", "call"}
    ]
    if not any(events[index].kind == "external_call" for index in call_positions):
        return []

    guard = has_modifier(function, REENTRANCY_GUARDS)

    out: list[DetectorSignal] = []
    for order, position in enumerate(call_positions):
        call_event = events[position]
        if call_event.kind != "external_call" or order >= len(calls):
            continue
        call = calls[order]
        transfer = resolver.transfer(call, function, contract)
        if transfer.verdict == NO_TRANSFER:
            # No code the caller does not control can run here: there is no
            # mechanism, so there is no signal.
            continue
        reads_before = {
            event.variable for event in events[:position]
            if event.kind in {"state_read", "state_write"} and event.variable
        }
        writes_after = [
            index for index in range(position + 1, len(events))
            if events[index].kind == "state_write"
        ]
        if not writes_after:
            continue
        written = {events[index].variable for index in writes_after}
        checked_then_written = sorted(written & reads_before)

        confidence = 0.62
        evidence = [
            f"external call `{call_event.detail}` at line {call_event.line}",
            transfer.detail,
            "state written after the call: " + ", ".join(sorted(x for x in written if x)),
        ]
        if transfer.verdict == UNRESOLVED:
            confidence -= UNRESOLVED_PENALTY
            evidence.append(
                f"receiver unresolved: confidence lowered by {UNRESOLVED_PENALTY:.2f}, "
                "signal kept"
            )
        if transfer.opaque_library:
            confidence -= OPAQUE_LIBRARY_PENALTY
            evidence.append(
                "the transfer is inferred through a library whose body is not in the "
                f"project: confidence lowered by {OPAQUE_LIBRARY_PENALTY:.2f}"
            )
        if checked_then_written:
            confidence += 0.14
            evidence.append(
                "state read before the call and written after: "
                + ", ".join(checked_then_written)
            )
        if call.value_attached:
            confidence += 0.08
            evidence.append("call forwards value to the callee")
        if call.callee in HIGH_RISK_CALLS:
            confidence += 0.06
            evidence.append(f"low-level `{call.callee}` hands execution to the callee")

        reachable = sorted(
            f"{other}" for variable in written
            for other in cross_readers.get(variable, ())
            if other != f"{function.contract}.{function.name}"
        )
        if reachable:
            confidence += 0.04
            evidence.append(
                "same state is reachable from other entry points: "
                + ", ".join(dict.fromkeys(reachable))[:400]
            )
        if transfer.static:
            confidence -= STATIC_PENALTY
            evidence.append(
                f"{transfer.static}: confidence lowered by {STATIC_PENALTY:.2f}"
            )
        if function.kind == "constructor":
            confidence -= CONSTRUCTOR_PENALTY
            evidence.append(
                "constructor body: the contract has no code while it is being constructed, "
                f"so a callback cannot re-enter it: confidence lowered by "
                f"{CONSTRUCTOR_PENALTY:.2f}"
            )

        if transfer.verdict == UNRESOLVED:
            reason = (
                "a call that may transfer control precedes the state update; its receiver "
                "could not be resolved, so a re-entrant call observing stale state is "
                "possible but unconfirmed"
            )
        else:
            reason = (
                "an external call transfers control before the state update, so a "
                "re-entrant call observes stale state"
            )
            if transfer.static:
                reason += "; the callee is static, so only a read-only re-entry is possible"
        if guard:
            confidence -= 0.35
            reason += f"; a reentrancy guard modifier `{guard}` is present"
            evidence.append(f"guard modifier present: {guard}")

        out.append(signal(
            DETECTOR,
            f"External call precedes state update in {function.contract}.{function.name}",
            function, confidence, reason,
            evidence=evidence,
            ordered_trace=_trace(events, position, writes_after),
            falsification=FALSIFICATION,
            references=REFERENCES,
            line=call_event.line,
        ))
    return out


def detect(contracts, engine=None) -> list[DetectorSignal]:
    contracts = list(contracts)
    cross_readers: dict[str, list[str]] = {}
    for contract in contracts:
        for function in contract.functions:
            if not function.is_entry_point:
                continue
            for variable in set(function.reads) | set(function.writes):
                cross_readers.setdefault(variable, []).append(
                    f"{contract.name}.{function.name}"
                )

    resolver = CallResolver(contracts)
    out: list[DetectorSignal] = []
    for contract in contracts:
        for function in contract.functions:
            out.extend(analyze_function(function, contract, cross_readers, resolver))
    return sorted(out, key=lambda x: (-x.confidence, x.contract, x.function, x.line))
