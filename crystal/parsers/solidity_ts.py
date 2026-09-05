"""tree-sitter Solidity front-end.

Produces the same `Contract`/`Function` records as the regex fallback but with
typed signatures, real statement ordering, modifier chains, events, errors,
user types and inline-assembly awareness. Everything downstream reads the IR,
so this parser is what turns Crystal's research engines from name matching into
structural analysis.
"""

from __future__ import annotations

import ctypes
import re
from functools import lru_cache
from pathlib import Path

from .. import ir as I
from ..models import (
    SOLIDITY,
    Contract,
    ContractError,
    ContractEvent,
    ContractType,
    Function,
    Parameter,
    StateVar,
)
from .base import ParseResult
from .solidity_types import (
    EMPTY_CATALOG,
    TypeCatalog,
    catalog_from_paths,
    catalog_from_text,
)

PARSER_NAME = "tree-sitter"

CONTRACT_NODES = {
    "contract_declaration": "contract",
    "interface_declaration": "interface",
    "library_declaration": "library",
}

CALLABLE_NODES = {
    "function_definition", "constructor_definition", "fallback_receive_definition",
}

LOW_LEVEL_NAMES = {
    "call": I.LOW_LEVEL_CALL,
    "delegatecall": I.DELEGATECALL,
    "staticcall": I.STATICCALL,
    "transfer": I.VALUE_TRANSFER,
    "send": I.VALUE_TRANSFER,
}

BUILTINS = {
    "require", "assert", "revert", "keccak256", "sha256", "ecrecover",
    "addmod", "mulmod", "blockhash", "selfdestruct", "type", "gasleft",
    "ripemd160",
}

MAPPING_RE = re.compile(r"mapping\s*\((.*)\)\s*$", re.DOTALL)


def _capsule(pointer: int):
    """Wrap a raw language pointer in a PyCapsule.

    tree-sitter-solidity still exposes the legacy integer ABI. On Windows a
    64-bit pointer overflows the `unsigned long` the modern binding expects, so
    we rebuild the capsule that API wants.
    """
    new = ctypes.pythonapi.PyCapsule_New
    new.restype = ctypes.py_object
    new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    return new(ctypes.c_void_p(pointer), b"tree_sitter.Language", None)


@lru_cache(maxsize=1)
def _load():
    try:
        import tree_sitter_solidity
        from tree_sitter import Language, Parser
    except ImportError as exc:
        return None, f"tree-sitter Solidity bindings unavailable: {exc}"
    try:
        raw = tree_sitter_solidity.language()
        language = Language(_capsule(raw) if isinstance(raw, int) else raw)
        return Parser(language), "ok"
    except Exception as exc:  # pragma: no cover - depends on binding build
        return None, f"tree-sitter Solidity language failed to load: {exc}"


def available() -> bool:
    return _load()[0] is not None


def status() -> str:
    return _load()[1]


def _unwrap(node):
    while node is not None and node.type in {"statement", "expression"} \
            and node.named_child_count == 1:
        node = node.named_children[0]
    return node


def _constant_bound(header: str) -> int | None:
    match = re.search(r"<\s*=?\s*(\d+)", header or "")
    return int(match.group(1)) if match else None


def classify_callee(callee_text: str) -> tuple[str, str, str | None, bool]:
    """Classify a callee expression: (name, kind, receiver, value_attached)."""
    text = " ".join((callee_text or "").split())
    options = ""
    brace = text.find("{")
    if brace >= 0:
        options = text[brace:]
        text = text[:brace].strip()
    value_attached = "value" in options
    if "." in text:
        receiver, name = text.rsplit(".", 1)
        receiver, name = receiver.strip(), name.strip()
        if name in LOW_LEVEL_NAMES:
            return (name, LOW_LEVEL_NAMES[name], receiver,
                    value_attached or name in {"transfer", "send"})
        if receiver == "super":
            return name, I.INTERNAL_CALL, receiver, value_attached
        if receiver in {"abi", "msg", "block", "tx"}:
            return name, I.BUILTIN_CALL, receiver, False
        return name, I.EXTERNAL_CALL, receiver, value_attached
    if text in BUILTINS:
        return text, I.BUILTIN_CALL, None, False
    return text, I.INTERNAL_CALL, None, value_attached


def _mapping_types(type_text: str) -> tuple[list[str], str]:
    match = MAPPING_RE.match(type_text.strip())
    if not match:
        return [], ""
    inner = match.group(1)
    depth = 0
    split = -1
    for index, char in enumerate(inner):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif inner.startswith("=>", index) and depth == 0:
            split = index
            break
    if split < 0:
        return [], inner.strip()
    key = inner[:split].strip()
    value = inner[split + 2:].strip()
    nested_keys, nested_value = _mapping_types(value)
    if nested_keys:
        return [key] + nested_keys, nested_value
    return [key], value


class _FileParser:
    """Owns one source file and lowers it into Crystal records."""

    def __init__(self, source: bytes, path: str, catalog: TypeCatalog | None = None):
        self.source = source
        self.path = path
        self.catalog = catalog or EMPTY_CATALOG
        self.state_names: set[str] = set()
        self.unsupported: list[str] = []
        self.has_assembly = False

    # -- text helpers ----------------------------------------------------
    def text(self, node) -> str:
        if node is None:
            return ""
        return self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def flat(self, node) -> str:
        return " ".join(self.text(node).split())[:400]

    def line(self, node) -> int:
        return node.start_point[0] + 1

    def child(self, node, *types):
        for child in node.named_children:
            if child.type in types:
                return child
        return None

    def identifiers(self, node) -> tuple[str, ...]:
        if node is None:
            return ()
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "identifier":
                found.append(self.text(current))
            stack.extend(reversed(current.named_children))
        return tuple(dict.fromkeys(found))

    def state_in(self, node) -> tuple[str, ...]:
        return tuple(sorted(set(self.identifiers(node)) & self.state_names))

    def lvalue_base(self, node) -> str | None:
        node = _unwrap(node)
        for _ in range(16):
            if node is None:
                return None
            if node.type == "identifier":
                return self.text(node)
            if node.named_child_count == 0:
                return None
            node = _unwrap(node.named_children[0])
        return None

    def expr(self, node) -> I.IRExpr:
        node = _unwrap(node)
        return I.IRExpr(
            node.type, self.flat(node), self.lvalue_base(node),
            identifiers=self.identifiers(node), line=self.line(node),
        )

    # -- statement lowering ----------------------------------------------
    def block(self, node) -> tuple[I.IRStmt, ...]:
        out: list[I.IRStmt] = []
        for child in node.named_children:
            statement = self.statement(child)
            if statement is not None:
                out.append(statement)
        return tuple(out)

    def wrap(self, node) -> tuple[I.IRStmt, ...]:
        node = _unwrap(node)
        if node is None:
            return ()
        if node.type == "block_statement":
            return self.block(node)
        statement = self.statement(node)
        return (statement,) if statement is not None else ()

    def statement(self, node):
        node = _unwrap(node)
        if node is None or node.type == "comment":
            return None
        handler = getattr(self, f"_st_{node.type}", None)
        if handler is not None:
            return handler(node)
        return I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node),
                        reads=self.state_in(node))

    def _st_expression_statement(self, node):
        inner = _unwrap(node.named_children[0]) if node.named_child_count else None
        if inner is None:
            return None
        if inner.type == "assignment_expression":
            return self._assignment(inner, "=")
        if inner.type == "augmented_assignment_expression":
            return self._assignment(inner, self._augmented_operator(inner))
        if inner.type == "update_expression":
            return self._update(inner)
        if inner.type == "call_expression":
            return self._call_statement(inner)
        return I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node),
                        reads=self.state_in(node))

    @staticmethod
    def _augmented_operator(node) -> str:
        for child in node.children:
            if not child.is_named and child.type.endswith("="):
                return child.type
        return "+="

    def _assignment(self, node, operator: str):
        target = node.named_children[0]
        value = node.named_children[-1]
        base = self.lvalue_base(target)
        reads = set(self.state_in(value))
        if operator != "=" or target.type != "identifier":
            reads |= set(self.state_in(target))
        return I.IRStmt(
            I.ASSIGN, self.line(node), self.flat(node),
            target=self.expr(target), operator=operator, value=self.expr(value),
            call=self.extract_call(value),
            writes=(base,) if base in self.state_names else (),
            reads=tuple(sorted(reads)),
        )

    def _update(self, node):
        target = node.named_children[0]
        base = self.lvalue_base(target)
        return I.IRStmt(
            I.ASSIGN, self.line(node), self.flat(node),
            target=self.expr(target),
            operator="+=" if "++" in self.text(node) else "-=",
            value=I.IRExpr("number_literal", "1", None, line=self.line(node)),
            writes=(base,) if base in self.state_names else (),
            reads=self.state_in(target),
        )

    def arguments(self, node) -> tuple[I.IRExpr, ...]:
        return tuple(
            self.expr(child) for child in node.named_children
            if child.type == "call_argument"
        )

    def is_conversion(self, call_node) -> bool:
        """A `call_expression` that builds a struct or converts a value.

        The grammar already keeps elementary casts (`address(x)`), `payable(x)`
        and `type(T)` out of `call_expression`; what still arrives here spelled
        as a call is a user type — `Exp({...})`, `CToken(addr)`, `Lib.S(1, 2)`
        — and only the project's declarations can say so. `new C()` carries a
        `new_expression` callee and is a real constructor call; `new T[](n)`
        carries one too but only allocates memory.
        """
        callee = _unwrap(call_node.named_children[0]) if call_node.named_child_count else None
        if callee is None:
            return False
        if callee.type == "identifier":
            return self.catalog.is_conversion(self.text(callee))
        if callee.type == "new_expression":
            return self.flat(callee).endswith("]")
        if callee.type == "member_expression" and callee.named_child_count == 2:
            owner, member = callee.named_children
            if owner.type == "identifier" and member.type == "identifier":
                return self.catalog.is_qualified_conversion(
                    self.text(owner), self.text(member)
                )
        return False

    def extract_call(self, node):
        node = _unwrap(node)
        if node is None:
            return None
        if node.type != "call_expression" or self.is_conversion(node):
            # Not a call itself; the call it may wrap is what matters —
            # `Exp({mantissa: CToken(c).borrowIndex()})` reaches `borrowIndex`.
            for child in node.named_children:
                found = self.extract_call(child)
                if found is not None:
                    return found
            return None
        callee = node.named_children[0] if node.named_child_count else None
        name, kind, receiver, value = classify_callee(self.text(callee))
        return I.IRCall(name, kind, self.line(node), self.flat(node), receiver,
                        self.arguments(node), value)

    def _call_statement(self, node):
        if self.is_conversion(node):
            call = self.extract_call(node)
            if call is None:
                return I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node),
                                reads=self.state_in(node))
            return I.IRStmt(I.CALL, self.line(node), self.flat(node), call=call,
                            reads=self.state_in(node))
        callee = node.named_children[0] if node.named_child_count else None
        name, kind, receiver, value = classify_callee(self.text(callee))
        arguments = self.arguments(node)
        if name in {"require", "assert"}:
            return I.IRStmt(
                I.REQUIRE, self.line(node), self.flat(node),
                condition=arguments[0] if arguments else None,
                reads=self.state_in(node), note=name,
            )
        return I.IRStmt(
            I.CALL, self.line(node), self.flat(node),
            call=I.IRCall(name, kind, self.line(node), self.flat(node), receiver,
                          arguments, value),
            reads=self.state_in(node),
        )

    def _st_variable_declaration_statement(self, node):
        value = node.named_children[-1] if node.named_child_count > 1 else None
        return I.IRStmt(
            I.VAR_DECL, self.line(node), self.flat(node),
            target=self.expr(node.named_children[0]),
            value=self.expr(value) if value is not None else None,
            call=self.extract_call(value),
            reads=self.state_in(value),
        )

    def _st_if_statement(self, node):
        named = node.named_children
        condition = named[0] if named else None
        return I.IRStmt(
            I.IF, self.line(node), self.flat(condition or node),
            condition=self.expr(condition) if condition is not None else None,
            body=self.wrap(named[1]) if len(named) > 1 else (),
            orelse=self.wrap(named[2]) if len(named) > 2 else (),
            reads=self.state_in(condition),
        )

    def _loop(self, node, header_nodes, body_node, note):
        header = " ".join(self.flat(x) for x in header_nodes if x is not None)
        reads: set[str] = set()
        for header_node in header_nodes:
            reads |= set(self.state_in(header_node))
        return I.IRStmt(
            I.LOOP, self.line(node), header or self.flat(node),
            condition=I.IRExpr("expression", header, None, line=self.line(node)),
            body=self.wrap(body_node) if body_node is not None else (),
            reads=tuple(sorted(reads)), loop_bound=_constant_bound(header), note=note,
        )

    def _st_for_statement(self, node):
        named = list(node.named_children)
        return self._loop(node, named[:-1], named[-1] if named else None, "for")

    def _st_while_statement(self, node):
        named = list(node.named_children)
        return self._loop(node, named[:1], named[-1] if len(named) > 1 else None, "while")

    def _st_do_while_statement(self, node):
        named = list(node.named_children)
        return self._loop(node, named[-1:], named[0] if named else None, "do")

    def _st_block_statement(self, node):
        unchecked = self.text(node).lstrip().startswith("unchecked")
        return I.IRStmt(
            I.BLOCK, self.line(node), "unchecked block" if unchecked else "block",
            body=self.block(node), unchecked=unchecked,
        )

    def _st_emit_statement(self, node):
        return I.IRStmt(I.EMIT, self.line(node), self.flat(node),
                        reads=self.state_in(node))

    def _st_return_statement(self, node):
        value = node.named_children[0] if node.named_child_count else None
        return I.IRStmt(
            I.RETURN, self.line(node), self.flat(node),
            value=self.expr(value) if value is not None else None,
            call=self.extract_call(value), reads=self.state_in(node),
        )

    def _st_revert_statement(self, node):
        return I.IRStmt(I.REVERT, self.line(node), self.flat(node),
                        reads=self.state_in(node))

    def _st_assembly_statement(self, node):
        self.has_assembly = True
        self.unsupported.append(f"inline assembly at line {self.line(node)}")
        return I.IRStmt(
            I.ASSEMBLY, self.line(node), self.flat(node),
            reads=self.state_in(node), note="inline-assembly",
        )

    def _st_try_statement(self, node):
        body: tuple[I.IRStmt, ...] = ()
        for child in node.named_children:
            if child.type in {"block_statement", "catch_clause"}:
                body = body + self.block(child)
        return I.IRStmt(
            I.CALL, self.line(node), self.flat(node), call=self.extract_call(node),
            body=body, note="try", reads=self.state_in(node),
        )

    # -- declarations ----------------------------------------------------
    def parameters(self, node, field: str = "parameter") -> list[Parameter]:
        if node is None:
            return []
        return [
            self._parameter(child) for child in node.named_children
            if child.type == field
        ]

    def _parameter(self, node) -> Parameter:
        type_node = self.child(node, "type_name")
        identifier = None
        for child in node.named_children:
            if child.type == "identifier":
                identifier = child
        raw = self.text(node)
        name = self.text(identifier)
        type_name = " ".join(self.text(type_node).split()) if type_node else ""
        if not type_name:
            type_name = " ".join(raw.replace(name, " ").split()) or raw
        location = ""
        for keyword in ("memory", "calldata", "storage"):
            if re.search(rf"\b{keyword}\b", raw):
                location = keyword
        return Parameter(name, type_name, bool(re.search(r"\bindexed\b", raw)), location)

    def state_variable(self, node, contract_name: str) -> StateVar:
        type_node = self.child(node, "type_name")
        type_text = " ".join(self.text(type_node).split()) if type_node else ""
        identifier = None
        for child in node.named_children:
            if child.type == "identifier":
                identifier = child
        # Qualifiers always sit between the type and the name; slicing there
        # avoids mistaking the `=>` of a mapping type for an initializer.
        head = ""
        tail = ""
        if identifier is not None:
            head = self.source[node.start_byte:identifier.start_byte].decode("utf-8", "replace")
            tail = self.source[identifier.end_byte:node.end_byte].decode("utf-8", "replace")
        visibility_node = self.child(node, "visibility")
        visibility = self.text(visibility_node) or "default"
        if visibility == "default":
            for candidate in ("public", "private", "internal"):
                if re.search(rf"\b{candidate}\b", head):
                    visibility = candidate
                    break
        keys, value_type = _mapping_types(type_text)
        initial = ""
        stripped_tail = tail.strip()
        if stripped_tail.startswith("="):
            initial = stripped_tail[1:].strip().rstrip(";").strip()
        return StateVar(
            contract_name, self.text(identifier), type_text, visibility,
            self.line(node),
            constant=bool(re.search(r"\bconstant\b", head)),
            immutable=bool(re.search(r"\bimmutable\b", head)),
            key_types=keys, value_type=value_type, language=SOLIDITY,
            initial_value=initial[:200],
        )

    def function_name(self, node) -> tuple[str, str]:
        if node.type == "fallback_receive_definition":
            name = "receive" if self.text(node).lstrip().startswith("receive") else "fallback"
            return name, name
        kind = {
            "constructor_definition": "constructor",
            "modifier_definition": "modifier",
        }.get(node.type, "function")
        identifier = self.child(node, "identifier")
        return (self.text(identifier) if identifier is not None else kind), kind

    def build_function(self, node, contract_name: str) -> Function:
        name, kind = self.function_name(node)
        raw = self.text(node)
        header = raw.split("{", 1)[0]
        visibility_node = self.child(node, "visibility")
        mutability_node = self.child(node, "state_mutability")
        visibility = self.text(visibility_node) or "unspecified"
        mutability = self.text(mutability_node) or "stateful"
        if kind == "constructor" and not visibility_node:
            visibility = "public"
        modifiers = [
            self.text(self.child(child, "identifier") or child)
            for child in node.named_children
            if child.type == "modifier_invocation"
        ]
        returns_node = self.child(node, "return_type_definition")
        body_node = self.child(node, "function_body")

        outer_assembly, outer_unsupported = self.has_assembly, list(self.unsupported)
        self.has_assembly, self.unsupported = False, []
        statements: tuple[I.IRStmt, ...] = ()
        body_text = ""
        if body_node is not None:
            statements = self.block(body_node)
            body_text = self.text(body_node)
            if body_text.startswith("{"):
                body_text = body_text[1:-1]
        function_assembly, function_unsupported = self.has_assembly, tuple(self.unsupported)
        self.has_assembly, self.unsupported = outer_assembly, outer_unsupported

        writes: set[str] = set()
        calls: list[str] = []
        low_level = False
        for statement in statements:
            for sub in statement.walk():
                writes |= set(sub.writes)
                if sub.call is not None:
                    if sub.call.kind in I.EXTERNAL_CALL_KINDS:
                        low_level = True
                    if sub.call.callee:
                        calls.append(sub.call.callee)
        reads = set(self.identifiers(body_node)) & self.state_names
        reads |= writes

        function = Function(
            contract_name, name, visibility, mutability,
            sorted(set(modifiers))[:16], reads, writes,
            (["<low-level-call>"] if low_level else []) + sorted(set(calls))[:32],
            self.line(node), body_text,
        )
        function.params = self.parameters(node)
        function.returns = self.parameters(returns_node)
        function.kind = kind
        function.language = SOLIDITY
        function.parser = PARSER_NAME
        function.path = self.path
        function.payable = mutability == "payable" or "payable" in header
        function.end_line = node.end_point[0] + 1
        function.ir = I.IRFunctionBody(
            statements=statements, has_assembly=function_assembly,
            unsupported=function_unsupported, source=body_text,
        )
        return function

    def build_contract(self, node, kind: str, imports: list[str]) -> Contract:
        identifier = self.child(node, "identifier")
        name = self.text(identifier) or "<anonymous>"
        if kind == "contract" and self.text(node).lstrip().startswith("abstract"):
            kind = "abstract"
        bases = [
            self.text(self.child(child, "user_defined_type") or child).split("(")[0].strip()
            for child in node.named_children
            if child.type == "inheritance_specifier"
        ]
        contract = Contract(
            name, self.path, self.line(node), bases=bases, kind=kind,
            language=SOLIDITY, end_line=node.end_point[0] + 1,
            imports=list(imports), parser=PARSER_NAME,
        )

        body = self.child(node, "contract_body")
        if body is None:
            return contract
        members = list(body.named_children)

        for child in members:
            if child.type == "state_variable_declaration":
                contract.state_vars.append(self.state_variable(child, name))
            elif child.type == "event_definition":
                contract.events.append(ContractEvent(
                    self.text(self.child(child, "identifier")),
                    self.parameters(child, "event_parameter"), self.line(child),
                ))
            elif child.type == "error_declaration":
                contract.errors.append(ContractError(
                    self.text(self.child(child, "identifier")),
                    self.parameters(child, "error_parameter"), self.line(child),
                ))
            elif child.type in {"enum_declaration", "struct_declaration"}:
                contract.types.append(self._user_type(child))
            elif child.type == "using_directive":
                parts = [self.text(x) for x in child.named_children]
                if len(parts) >= 2:
                    contract.using_for.append((parts[0], parts[1]))

        previous = self.state_names
        self.state_names = {v.name for v in contract.state_vars}
        for child in members:
            if child.type in CALLABLE_NODES:
                contract.functions.append(self.build_function(child, name))
            elif child.type == "modifier_definition":
                contract.modifier_definitions.append(self.build_function(child, name))
        self.state_names = previous
        return contract

    def _user_type(self, node) -> ContractType:
        body = self.child(node, "enum_body", "struct_body")
        members = []
        if body is not None:
            members = [
                self.flat(child) for child in body.named_children
                if child.type in {"identifier", "struct_member"}
            ]
        return ContractType(
            self.text(self.child(node, "identifier")),
            "enum" if node.type == "enum_declaration" else "struct",
            members, self.line(node),
        )

    # -- entry point -----------------------------------------------------
    def run(self, root) -> list[Contract]:
        imports = []
        for child in root.named_children:
            if child.type == "import_directive":
                match = re.search(r'["\']([^"\']+)["\']', self.text(child))
                if match:
                    imports.append(match.group(1))

        contracts: list[Contract] = []
        free_functions: list[Function] = []
        file_types: list[ContractType] = []
        file_errors: list[ContractError] = []

        for child in root.named_children:
            kind = CONTRACT_NODES.get(child.type)
            if kind:
                contracts.append(self.build_contract(child, kind, imports))
            elif child.type == "error_declaration":
                file_errors.append(ContractError(
                    self.text(self.child(child, "identifier")),
                    self.parameters(child, "error_parameter"), self.line(child),
                ))
            elif child.type in {"enum_declaration", "struct_declaration"}:
                file_types.append(self._user_type(child))
            elif child.type == "function_definition":
                free_functions.append(self.build_function(child, Path(self.path).stem))

        if free_functions or file_types or file_errors:
            holder = Contract(
                f"{Path(self.path).stem}<file>", self.path, 1, kind="file",
                language=SOLIDITY, end_line=root.end_point[0] + 1,
                imports=list(imports), parser=PARSER_NAME,
            )
            holder.functions = free_functions
            holder.types = file_types
            holder.errors = file_errors
            contracts.append(holder)
        return contracts


def parse_text(text: str, path: str, catalog: TypeCatalog | None = None) -> list[Contract]:
    """Lower one file. `catalog` carries the project's declared type names so a
    cast to a contract declared elsewhere is not recorded as a call; the file's
    own declarations are always known."""
    parser, _ = _load()
    if parser is None:
        return []
    source = text.encode("utf-8")
    tree = parser.parse(source)
    known = catalog_from_text(text).merged(catalog)
    return _FileParser(source, str(path), known).run(tree.root_node)


def parse_file(path, catalog: TypeCatalog | None = None) -> list[Contract]:
    return parse_text(
        Path(path).read_text(encoding="utf-8", errors="ignore"), str(path), catalog
    )


# Project-wide declared names; `parse_project` builds this once per language.
type_catalog = catalog_from_paths


def parse_sources(paths) -> list[Contract]:
    solidity = [path for path in paths if Path(path).suffix.lower() == ".sol"]
    catalog = type_catalog(solidity)
    out: list[Contract] = []
    for path in solidity:
        out.extend(parse_file(path, catalog))
    return out


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), PARSER_NAME, SOLIDITY)
