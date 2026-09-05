"""Binding property terms to what the target actually exposes.

Everything a property reads has to be reachable from a harness: a public
getter, or a view function whose only state read is the variable in question.
Everything a property sums over has to have an enumerable key set, which in a
closed-world harness means every key ever written is an identifier the harness
handed to the contract (a parameter, `msg.sender`, a field of a parameter, or a
stored copy of one). Both checks are made here, against Crystal's parsed model,
and a failure is reported with the site that failed rather than papered over.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import ir as I
from ..graphs.binding import build_bindings
from .ir import FunctionRef, Guard, StateRef

IDENT_CHAIN_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")
ROLE_RE = re.compile(r"onlyRole\s*\(\s*([A-Za-z_][\w.]*)\s*\)")
ENUM_RE = re.compile(r"enum\s+(\w+)\s*\{([^}]*)\}")
REVERT_RE = re.compile(r"revert\s+([A-Za-z_][\w.]*)\s*\(")
REVERT_STRING_RE = re.compile(r"revert\s*\(\s*\"([^\"]*)\"")
REQUIRE_STRING_RE = re.compile(r"require\s*\(.*,\s*\"([^\"]*)\"\s*\)\s*;?$", re.S)
SUBSCRIPT_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\[(.+?)\]")
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")

PRIMITIVE_RE = re.compile(r"^(u?int\d*|address(?: payable)?|bool|bytes\d*|string)$")


def _clean_type(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return re.sub(r"\b(memory|calldata|storage)\b", "", cleaned).strip()


def canonical_type(type_name: str, resolver: "Resolver", near=None) -> str | None:
    """ABI canonical form of a declared type (`uint` -> `uint256`, structs to
    tuples, enums to `uint8`), or None when it cannot be determined."""
    text = _clean_type(type_name)
    if text.endswith("[]"):
        inner = canonical_type(text[:-2], resolver, near)
        return None if inner is None else inner + "[]"
    match = re.fullmatch(r"(.+)\[(\d+)\]", text)
    if match:
        inner = canonical_type(match.group(1), resolver, near)
        return None if inner is None else f"{inner}[{match.group(2)}]"
    if text == "uint":
        return "uint256"
    if text == "int":
        return "int256"
    if text == "address payable":
        return "address"
    if PRIMITIVE_RE.match(text):
        return text
    fields = resolver.struct_fields(text, near)
    if fields is not None:
        parts = [canonical_type(ftype, resolver, near) for ftype, _ in fields]
        if any(part is None for part in parts):
            return None
        return "(" + ",".join(parts) + ")"
    if resolver.enum_members(text, near) is not None:
        return "uint8"
    if resolver.by_name.get(text.split(".")[-1]) is not None:
        return "address"  # contract / interface typed value
    return None


class Resolver:
    """Everything the compiler needs to know about the target, in one place."""

    def __init__(self, contracts, scope=None):
        self.all = list(contracts)
        self.by_name = {c.name: c for c in self.all}
        self.concrete = [
            c for c in self.all
            if c.kind == "contract" and not getattr(c, "is_test", False)
            and (scope is None or c.name in scope)
        ]
        self.bindings = build_bindings(self.all)
        self._sources: dict[str, list[str]] = {}

    # ── sources ──────────────────────────────────────────────────────────

    def source_lines(self, path: str) -> list[str]:
        if path not in self._sources:
            try:
                self._sources[path] = Path(path).read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
            except OSError:
                self._sources[path] = []
        return self._sources[path]

    def pragma(self, contract) -> str:
        for line in self.source_lines(contract.path)[:40]:
            match = PRAGMA_RE.search(line)
            if match:
                return match.group(1).strip()
        return "^0.8.20"

    # ── types ────────────────────────────────────────────────────────────

    def owner_contract(self, near, owner: str):
        """The parsed unit declaring `owner`, resolved through `near`'s own
        imports first: a repository can hold two libraries of one name (a live
        one and a legacy one), and only the file `near` imports is the one its
        code is compiled against."""
        if near is not None:
            if owner == near.name:
                return near
            for imported in getattr(near, "imports", ()) or ():
                text = str(imported).replace("\\", "/")
                if Path(text).name != f"{owner}.sol":
                    continue
                target = (Path(near.path).parent / text) if text.startswith(".") else Path(text)
                try:
                    wanted = target.resolve()
                except OSError:
                    wanted = target
                for candidate in self.all:
                    if candidate.name != owner:
                        continue
                    try:
                        if Path(candidate.path).resolve() == wanted:
                            return candidate
                    except OSError:
                        continue
            for base in near.bases:
                if base == owner and base in self.by_name:
                    return self.by_name[base]
        return self.by_name.get(owner)

    def _type_declaration(self, type_name: str, kind: str, near=None):
        owner, _, bare = type_name.strip().rpartition(".")
        units = []
        if owner:
            unit = self.owner_contract(near, owner)
            if unit is not None:
                units.append(unit)
        elif near is not None:
            units.append(near)
            units.extend(self.by_name[b] for b in near.bases if b in self.by_name)
        units.extend(u for u in self.all if u not in units)
        for unit in units:
            for declared in unit.types:
                if declared.kind == kind and declared.name == bare:
                    return unit, declared
        return None, None

    def struct_fields(self, type_name: str, near=None) -> list[tuple[str, str]] | None:
        bare = type_name.split(".")[-1].strip()
        unit, declared = self._type_declaration(type_name, "struct", near)
        for contract in ([unit] if unit is not None else []):
            for declared in contract.types:
                if declared.kind == "struct" and declared.name == bare:
                    fields = []
                    for member in declared.members:
                        text = member.strip().rstrip(";").strip()
                        if not text:
                            continue
                        parts = text.split()
                        fields.append((" ".join(parts[:-1]), parts[-1]))
                    return fields
        return None

    def enum_members(self, type_name: str, near=None) -> list[str] | None:
        unit, declared = self._type_declaration(type_name, "enum", near)
        if unit is None:
            return None
        members = list(declared.members)
        if not members:
            # The front-ends record enum names but not members; read them
            # from the declaring file.
            text = "\n".join(self.source_lines(unit.path))
            for match in ENUM_RE.finditer(text):
                if match.group(1) == declared.name:
                    members = [m.strip() for m in match.group(2).split(",") if m.strip()]
        return members

    def enum_owner(self, type_name: str, near=None) -> str:
        unit, _ = self._type_declaration(type_name, "enum", near)
        return unit.name if unit is not None else ""

    # ── state ────────────────────────────────────────────────────────────

    def state(self, bare: str) -> list[StateRef]:
        found = []
        for contract in self.concrete:
            for variable in contract.state_vars:
                if variable.name == bare and not variable.constant and not variable.immutable:
                    keys, value = self._mapping_shape(variable)
                    getter, kind = self.getter(contract, variable, keys)
                    found.append(StateRef(
                        contract.name, variable.name, variable.type_name,
                        keys, value, getter, kind,
                    ))
        return found

    @staticmethod
    def _mapping_shape(variable) -> tuple[tuple[str, ...], str]:
        """Key types and value type, read from the declaration text when the
        front-end did not fill them in (the regex parser does not)."""
        keys = tuple(variable.key_types)
        value = variable.value_type or ""
        text = re.sub(r"\s+", "", variable.type_name or "")
        if not keys and text.startswith("mapping("):
            parsed: list[str] = []
            while text.startswith("mapping("):
                inner = text[len("mapping("):-1]
                key, _, rest = inner.partition("=>")
                parsed.append(key)
                text = rest
            keys = tuple(parsed)
            value = value or text
        return keys, value or variable.type_name

    def getter(self, contract, variable, keys=None) -> tuple[str, str]:
        if keys is None:
            keys, _ = self._mapping_shape(variable)
        if variable.visibility == "public":
            return variable.name, "auto-getter"
        candidates = []
        for function in contract.functions:
            if function.kind != "function" or not function.is_entry_point:
                continue
            if function.mutability not in {"view", "pure"}:
                continue
            reads = {
                name for name in function.reads
                if not self._is_constant(contract, name)
            }
            if reads != {variable.name} or function.writes:
                continue
            if len(function.params) != len(keys):
                continue
            if not function.returns:
                continue
            candidates.append(function)
        if not candidates:
            return "", ""
        wanted = variable.name.lstrip("_").lower()
        candidates.sort(key=lambda f: (wanted not in f.name.lower(), f.name))
        return candidates[0].name, "view-function"

    def _is_constant(self, contract, name: str) -> bool:
        return any(
            v.name == name and (v.constant or v.immutable) for v in contract.state_vars
        )

    # ── functions ────────────────────────────────────────────────────────

    def entry_points(self, contract) -> list:
        out = []
        for function in contract.functions:
            if function.kind != "function" or not function.is_entry_point:
                continue
            if function.mutability in {"view", "pure"}:
                continue
            if "initializer" in function.modifiers or function.name == "initialize":
                continue
            out.append(function)
        return out

    def function_ref(self, contract, function) -> FunctionRef:
        return FunctionRef(
            contract.name, function.name,
            tuple((_clean_type(p.type_name), p.name) for p in function.params),
            function.mutability == "payable", self.role_of(contract, function),
        )

    def find_entry_point(self, name: str) -> list[tuple[object, object]]:
        """Entry points called `name` or `Contract.name` across the scope."""
        contract_name, _, bare = name.rpartition(".")
        found = []
        for contract in self.concrete:
            if contract_name and contract.name != contract_name:
                continue
            for function in self.entry_points(contract):
                if function.name == bare:
                    found.append((contract, function))
        return found

    def role_of(self, contract, function) -> str | None:
        if "onlyRole" not in function.modifiers:
            return None
        lines = self.source_lines(contract.path)
        start = max(function.line - 1, 0)
        header = " ".join(lines[start:start + 12])
        header = header.split("{", 1)[0]
        match = ROLE_RE.search(header)
        return match.group(1) if match else None

    def internal_closure(self, contract, function) -> list:
        """`function` plus every same-contract function it reaches internally."""
        by_name: dict[str, list] = {}
        for candidate in contract.functions:
            by_name.setdefault(candidate.name, []).append(candidate)
        seen: list = []
        stack = [function]
        while stack:
            current = stack.pop()
            if any(current is s for s in seen):
                continue
            seen.append(current)
            for callee in current.calls:
                for target in by_name.get(callee, []):
                    if target.kind in {"function", "modifier"} and not target.is_entry_point:
                        stack.append(target)
                    elif target.kind == "function" and target is not current \
                            and target.visibility == "public":
                        stack.append(target)
        return seen

    def writes_transitively(self, contract, function, variable: str) -> bool:
        return any(variable in f.writes for f in self.internal_closure(contract, function))

    def entry_writers(self, contract, variable: str) -> list:
        return [
            f for f in self.entry_points(contract)
            if self.writes_transitively(contract, f, variable)
        ]

    def key_derivation(self, contract, function, key: str) -> str | None:
        """The public view function that derives `key` the way `function`
        derives it internally (`quoteHash = _hashPegInQuote(quote)` and a
        public `hashPegInQuote` calling the same helper), or None."""
        if function.ir is None:
            return None
        helper = None
        for statement in function.ir.walk():
            if statement.kind != I.VAR_DECL or statement.target is None:
                continue
            if statement.target.text.strip().split()[-1] != key:
                continue
            if statement.call is not None:
                helper = statement.call.callee
            else:
                # The regex front-end records no call on an internal helper;
                # read the initialiser `key = helper(...)` from the text.
                match = re.search(rf"\b{re.escape(key)}\s*=\s*([A-Za-z_]\w*)\s*\(", statement.text)
                helper = match.group(1) if match else None
            break
        if helper is None:
            return None
        for candidate in contract.functions:
            if candidate.kind != "function" or not candidate.is_entry_point:
                continue
            if candidate.mutability not in {"view", "pure"}:
                continue
            if len(candidate.params) != 1 or not candidate.returns:
                continue
            if candidate.name == helper or helper in candidate.calls \
                    or re.search(rf"\b{re.escape(helper)}\s*\(", candidate.body or ""):
                return candidate.name
        return None

    def value_writers(self, contract, variable: str, value: str) -> list:
        """Entry points that assign `value` (by text) to `variable`."""
        found = []
        for function in self.entry_points(contract):
            for member in self.internal_closure(contract, function):
                if member.ir is None:
                    continue
                for statement in member.ir.walk():
                    if variable in statement.writes and statement.value is not None \
                            and value in statement.value.text:
                        found.append(function)
                        break
                else:
                    continue
                break
        return found

    # ── bound calls ──────────────────────────────────────────────────────

    def bound_calls(self, contract, function) -> list[tuple[I.IRCall, object, object, float]]:
        """External calls through interface-typed handles, resolved to the
        concrete (contract, function) behind the declared type."""
        out = []
        for member in self.internal_closure(contract, function):
            if member.ir is None:
                continue
            for call in member.ir.calls():
                if call.kind != I.EXTERNAL_CALL or not call.receiver:
                    continue
                for qualified, confidence in self.bindings.resolve(
                    contract.name, call.receiver, call.callee
                ):
                    target_contract, target_name = qualified.split(".", 1)
                    target = self.by_name.get(target_contract)
                    if target is None:
                        continue
                    for candidate in target.functions:
                        if candidate.name == target_name:
                            out.append((call, target, candidate, confidence))
        return out

    def reaches_writer_of(self, contract, function, state: StateRef) -> bool:
        for _, target, callee, _ in self.bound_calls(contract, function):
            if target.name == state.contract and \
                    self.writes_transitively(target, callee, state.name):
                return True
        return False

    def reaches_reader_of(self, contract, function, state: StateRef) -> bool:
        for _, target, callee, _ in self.bound_calls(contract, function):
            if target.name == state.contract and any(
                state.name in f.reads for f in self.internal_closure(target, callee)
            ):
                return True
        return False

    def bound_writer_arithmetic(self, contract, function, state: StateRef) -> bool:
        """True when a bound callee applies checked arithmetic to the state,
        so a Panic(0x11) from the callee would itself be state-dependent."""
        for _, target, callee, _ in self.bound_calls(contract, function):
            if target.name != state.contract:
                continue
            for member in self.internal_closure(target, callee):
                if member.ir is None:
                    continue
                for statement in member.ir.walk():
                    if state.name in statement.writes and statement.operator in {"-=", "+="}:
                        return True
        return False

    # ── holder-set derivability ──────────────────────────────────────────

    def key_problems(self, contract, variable: str) -> list[str]:
        """Write sites whose key is not an identifier the harness supplied.

        Accepted keys: a parameter, `msg.sender`, `address(this)`, a member
        chain rooted in a parameter or local, or a local declared from one of
        those (or from a storage read). Rejected: anything computed — a hash,
        arithmetic, a literal, a call. A rejected site means the holder set is
        not enumerable from the harness's own inputs.
        """
        problems: list[str] = []
        for function in contract.functions:
            if function.ir is None:
                if variable in function.writes:
                    problems.append(
                        f"{contract.name}.{function.name} writes {variable} but has "
                        f"no IR to inspect the key expression"
                    )
                continue
            locals_ok = self._local_declarations(function)
            params = {p.name for p in function.params}
            for statement in function.ir.walk():
                if variable not in statement.writes:
                    continue
                text = statement.target.text if statement.target else statement.text
                match = SUBSCRIPT_RE.match(text)
                if not match or match.group(1) != variable:
                    if statement.kind == I.DELETE:
                        match = SUBSCRIPT_RE.match(statement.text.replace("delete", "", 1))
                    if not match or match.group(1) != variable:
                        problems.append(
                            f"{contract.name}.{function.name}:{statement.line} writes "
                            f"{variable} through `{text.strip()[:60]}`, not a keyed access"
                        )
                        continue
                key = match.group(2).strip()
                if not self._key_accepted(key, params, locals_ok):
                    problems.append(
                        f"{contract.name}.{function.name}:{statement.line} key "
                        f"`{key}` of {variable} is computed, not supplied by a caller"
                    )
        return problems

    def _local_declarations(self, function) -> dict[str, bool]:
        """Local name -> whether its initialiser is an accepted key source."""
        out: dict[str, bool] = {}
        if function.ir is None:
            return out
        for statement in function.ir.walk():
            if statement.kind != I.VAR_DECL or statement.target is None:
                continue
            target = statement.target.text.strip()
            name = target.split()[-1] if target else ""
            if not name or name.startswith("("):
                continue
            rhs = statement.text.split("=", 1)[1].strip().rstrip(";") if "=" in statement.text else ""
            rhs = re.sub(r"^payable\s*\((.*)\)$", r"\1", rhs).strip()
            accepted = (
                rhs in {"msg.sender", "address(this)", "tx.origin"}
                or bool(IDENT_CHAIN_RE.match(rhs))
                or bool(SUBSCRIPT_RE.match(rhs))
            )
            out[name] = accepted
        return out

    def _key_accepted(self, key: str, params: set[str], locals_ok: dict[str, bool]) -> bool:
        if key in {"msg.sender", "address(this)", "tx.origin"}:
            return True
        if not IDENT_CHAIN_RE.match(key):
            return False
        root = key.split(".")[0]
        if root in params:
            return True
        if root in locals_ok:
            return locals_ok[root]
        return False

    # ── state-dependent guards ───────────────────────────────────────────

    def state_guards(self, contract, function, state: StateRef) -> tuple[list[Guard], list[str]]:
        """Reverts of `function` (or its internal callees) whose condition
        depends on `state`, reached through a bound call to a reader of it.

        Returns (guards, problems). A guard without a distinguishable revert
        reason is a problem: the harness could not tell it apart at runtime.
        """
        guards: list[Guard] = []
        problems: list[str] = []
        readers = {
            call.line: (target, callee)
            for call, target, callee, _ in self.bound_calls(contract, function)
            if target.name == state.contract and any(
                state.name in f.reads for f in self.internal_closure(target, callee)
            )
        }
        reader_names = {callee.name for _, callee in readers.values()}
        for member in self.internal_closure(contract, function):
            if member.ir is None:
                continue
            derived: set[str] = set()
            statements = list(member.ir.walk())
            for statement in statements:
                if statement.kind in {I.VAR_DECL, I.ASSIGN} and statement.call is not None \
                        and statement.call.kind == I.EXTERNAL_CALL \
                        and statement.call.callee in reader_names \
                        and statement.target is not None:
                    derived.add(statement.target.text.strip().split()[-1])
            for statement in statements:
                if statement.kind not in {I.IF, I.REQUIRE}:
                    continue
                condition = (statement.condition.text if statement.condition else statement.text)
                depends = any(
                    re.search(rf"\b{re.escape(local)}\b", condition) for local in derived
                ) or any(
                    f"{receiver}.{callee}(" in condition.replace(" ", "")
                    for receiver, callee in self._receiver_calls(condition)
                    if callee in reader_names
                )
                if not depends:
                    continue
                if statement.kind == I.REQUIRE:
                    message = REQUIRE_STRING_RE.search(statement.text)
                    if not message:
                        problems.append(
                            f"{contract.name}.{function.name}:{statement.line} guards on "
                            f"{state.qualified} with a require() that carries no message; "
                            f"its revert cannot be told apart at runtime"
                        )
                        continue
                    guards.append(Guard(
                        f"{contract.name}.{function.name}", statement.line, condition,
                        "", "", message.group(1),
                    ))
                    continue
                for inner in statement.body:
                    if inner.kind != I.REVERT:
                        continue
                    named = REVERT_RE.search(inner.text)
                    if named:
                        owner, _, error = named.group(1).rpartition(".")
                        owner = owner or self.error_owner(contract, error)
                        if not owner:
                            problems.append(
                                f"{contract.name}.{function.name}:{inner.line} reverts with "
                                f"`{error}` which is declared nowhere in the parsed set"
                            )
                            continue
                        guards.append(Guard(
                            f"{contract.name}.{function.name}", inner.line, condition,
                            owner, error,
                        ))
                        continue
                    text = REVERT_STRING_RE.search(inner.text)
                    if text:
                        guards.append(Guard(
                            f"{contract.name}.{function.name}", inner.line, condition,
                            "", "", text.group(1),
                        ))
                        continue
                    problems.append(
                        f"{contract.name}.{function.name}:{inner.line} guards on "
                        f"{state.qualified} with a bare revert; its reason cannot be "
                        f"told apart at runtime"
                    )
        return guards, problems

    @staticmethod
    def _receiver_calls(text: str) -> list[tuple[str, str]]:
        return [(m.group(1), m.group(2)) for m in CALL_RE.finditer(text or "")]

    def error_owner(self, contract, error: str) -> str:
        """The contract, interface or library declaring `error`, searching the
        contract itself, then its bases, then everything parsed."""
        order = [contract.name] + list(contract.bases)
        for name in order:
            declared = self.by_name.get(name)
            if declared and any(e.name == error for e in declared.errors):
                return name
        for other in self.all:
            if any(e.name == error for e in other.errors):
                return other.name
        return ""

    def import_for(self, name: str) -> str | None:
        """The path of the file declaring `name`, or None."""
        declared = self.by_name.get(name)
        return declared.path if declared else None
