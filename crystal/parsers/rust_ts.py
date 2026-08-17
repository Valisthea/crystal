"""tree-sitter Rust front-end with Substrate and Anchor awareness.

Rust concepts are mapped onto Crystal's abstract model so every downstream
engine keeps working unchanged:

    #[frame_support::pallet] mod pallet   -> Contract(kind="pallet")
    #[pallet::storage] type Balances      -> StateVar
    #[pallet::call] pub fn transfer(..)   -> Function(kind="extrinsic")
    #[program] mod my_program             -> Contract(kind="program")
    #[account] struct Vault               -> Contract(kind="account")
    impl Counter { pub fn inc(&mut self) } -> Contract + Function

Storage access through the Substrate API (`Balances::<T>::mutate`, `::put`,
`::insert`, ...) is lowered into ordinary IR assignments so the symbolic engine
produces real state deltas for pallets.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .. import ir as I
from ..models import RUST, ConfigBinding, Contract, ContractError, ContractEvent
from ..models import ContractType, Function, Parameter, RuntimeWiring, StateVar
from .base import ParseResult

PARSER_NAME = "tree-sitter"

STORAGE_WRITE_METHODS = {
    "put": "=", "set": "=", "insert": "=", "mutate": "mutate",
    "try_mutate": "mutate", "mutate_exists": "mutate",
    "try_mutate_exists": "mutate", "append": "+=", "kill": "clear",
    "remove": "clear", "take": "clear", "clear": "clear", "swap": "=",
    "put_ref": "=", "translate": "mutate",
}
STORAGE_READ_METHODS = {
    "get", "try_get", "contains_key", "iter", "iter_keys", "iter_values",
    "decode_len", "exists", "iter_prefix", "count",
}

GUARD_MACROS = {"ensure", "assert", "assert_eq", "assert_ne", "debug_assert", "require"}

# Result/Option plumbing. `withdraw(..).map(|x| ..)` is a withdrawal, not a map:
# classifying the chain by its last segment hides the operation that matters.
RUST_COMBINATORS = {
    "map", "map_err", "map_or", "map_or_else", "and_then", "or_else", "unwrap",
    "unwrap_or", "unwrap_or_default", "unwrap_or_else", "expect", "ok", "ok_or",
    "ok_or_else", "into", "try_into", "borrow", "borrow_mut", "as_ref", "as_mut",
    "clone", "to_owned", "cloned", "copied", "into_iter", "iter", "collect",
    "unwrap_err", "inspect", "inspect_err",
}

# Conversions and constructors: they produce a value, they do not run anything.
CONVERSION_CALLS = {
    "into", "from", "new", "default", "clone", "to_owned", "encode", "decode",
    "as_ref", "as_mut", "to_vec", "to_string", "try_into", "try_from",
    "saturated_into", "unique_saturated_into", "using_encoded",
}

# Lookups, accessors and arithmetic. They read, they do not hand over control.
READ_ONLY_CALLS = {
    "lookup", "unlookup", "convert", "current_block_number", "block_number",
    "len", "is_empty", "count", "contains", "contains_key", "iter", "keys",
    "values", "decode_len", "min", "max", "abs", "pow", "sqrt", "hash",
    "account_id", "account_truncating", "sovereign_account", "now", "get",
    "hash_of", "blake2_256", "twox_64", "keccak_256", "sha2_256", "using",
}
READ_ONLY_PREFIXES = ("saturating_", "checked_", "wrapping_", "overflowing_",
                      "is_", "has_", "can_", "should_", "expect_")

CPI_CALLS = {
    "invoke", "invoke_signed", "invoke_signed_unchecked",
    "transfer", "transfer_checked", "mint_to", "burn", "close_account",
}

EXTERNAL_PATH_HINTS = (
    "CpiContext", "cpi::", "token::", "system_program::", "solana_program::",
    "T::Currency", "Currency::", "pallet_", "::dispatch", "XcmpQueue",
)

SUBSTRATE_STORAGE_TYPES = (
    "StorageValue", "StorageMap", "StorageDoubleMap", "StorageNMap",
    "CountedStorageMap", "StorageVec",
)

# Traits whose implementors are DECODED from the transaction rather than built
# by the runtime. Every field of such a type is attacker-chosen by contract, not
# by heuristic: that is what the trait means.
USER_DECODED_TRAITS = (
    "TransactionExtension", "SignedExtension", "Decode", "Call",
)

# Filenames and attributes that mark fixtures. A test builder mutates state and
# skips authority checks by design; reporting it is noise, not a finding.
TEST_FILE_STEMS = {"tests", "test", "mock", "mocks", "benchmarking", "fixtures",
                   "test_utils", "testing", "mock_runtime"}
TEST_MODULE_NAMES = {"tests", "test", "mock", "mocks", "testing", "benchmarking"}

# Anchored on the whole attribute, not searched as a substring: `bench` matched
# inside `cfg(feature="runtime-benchmarks")` on a production impl and marked the
# entire type as a fixture.
_TEST_ATTRIBUTE_RE = re.compile(
    r"^(?:test|bench|ignore|should_panic|test_case|rstest|tokio::test"
    r"|cfg\(test\b|cfg\(any\(test\b|cfg\(all\(test\b"
    r"|cfg\(feature=\"runtime-benchmarks\"\)"
    r"|cfg_attr\(test\b)"
)


def _is_test_path(path: str) -> bool:
    stem = Path(path).stem.lower()
    if stem in TEST_FILE_STEMS:
        return True
    parts = {part.lower() for part in Path(path).parts}
    return bool(parts & {"tests", "test", "benches", "mock", "mocks"})


def _is_test_attribute(attributes: list[str]) -> bool:
    return any(
        _TEST_ATTRIBUTE_RE.match(attribute.lower().replace(" ", ""))
        for attribute in attributes
    )


@lru_cache(maxsize=1)
def _load():
    try:
        import tree_sitter_rust
        from tree_sitter import Language, Parser
    except ImportError as exc:
        return None, f"tree-sitter Rust bindings unavailable: {exc}"
    try:
        return Parser(Language(tree_sitter_rust.language())), "ok"
    except Exception as exc:  # pragma: no cover - depends on binding build
        return None, f"tree-sitter Rust language failed to load: {exc}"


def available() -> bool:
    return _load()[0] is not None


def status() -> str:
    return _load()[1]


def _attribute_names(attributes: list[str]) -> set[str]:
    return {a.split("(")[0].strip() for a in attributes}


def _has_attribute(attributes: list[str], *needles: str) -> bool:
    joined = " ".join(attributes)
    return any(needle in joined for needle in needles)


class _FileParser:
    def __init__(self, source: bytes, path: str):
        self.source = source
        self.path = path
        self.state_names: set[str] = set()
        self.storage_items: dict[str, StateVar] = {}
        self.unsupported: list[str] = []
        self.is_test_file = _is_test_path(path)
        self.test_depth = 0
        self.wirings: list[RuntimeWiring] = []
        self.bindings: list[ConfigBinding] = []

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
        if node is None:
            return None
        for child in node.named_children:
            if child.type in types:
                return child
        return None

    def field(self, node, name: str):
        return node.child_by_field_name(name) if node is not None else None

    def identifiers(self, node) -> tuple[str, ...]:
        if node is None:
            return ()
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in {"identifier", "field_identifier", "type_identifier"}:
                found.append(self.text(current))
            stack.extend(reversed(current.named_children))
        return tuple(dict.fromkeys(found))

    def state_in(self, node) -> tuple[str, ...]:
        return tuple(sorted(set(self.identifiers(node)) & self.state_names))

    def expr(self, node) -> I.IRExpr:
        return I.IRExpr(
            node.type if node is not None else "empty", self.flat(node),
            self.lvalue_base(node), identifiers=self.identifiers(node),
            line=self.line(node) if node is not None else 0,
        )

    def lvalue_base(self, node) -> str | None:
        """`self.count` -> `count`; `*balance` -> `balance`; `x` -> `x`."""
        if node is None:
            return None
        if node.type == "field_expression":
            return self.text(self.field(node, "field")) or None
        if node.type in {"unary_expression", "reference_expression"}:
            return self.lvalue_base(node.named_children[0]) if node.named_child_count else None
        if node.type in {"identifier", "type_identifier"}:
            return self.text(node)
        if node.type == "index_expression" and node.named_child_count:
            return self.lvalue_base(node.named_children[0])
        identifiers = self.identifiers(node)
        return identifiers[0] if identifiers else None

    # -- call classification ---------------------------------------------
    def classify_call(self, node) -> I.IRCall | None:
        function_node = self.field(node, "function")
        callee_text = self.flat(function_node) or self.flat(node)
        segments = [s for s in re.split(r"::|\.", re.sub(r"<[^>]*>", "", callee_text)) if s]
        name = segments[-1].strip() if segments else callee_text
        receiver = "::".join(segments[:-1]) or None

        kind = I.INTERNAL_CALL
        if name in CONVERSION_CALLS or name in READ_ONLY_CALLS \
                or name.startswith(READ_ONLY_PREFIXES):
            # `pallet_balances::Call::<T>::transfer_keep_alive { .. }.into()`
            # BUILDS a dispatchable, it does not execute one. Reading it as a
            # control transfer makes every call-construction look re-entrant.
            kind = I.BUILTIN_CALL
        elif name[:1].isupper():
            # Rust convention: functions are snake_case, types and enum variants
            # are CamelCase. `DispatchTime::At(..)` and `Ok(..)` construct data.
            kind = I.BUILTIN_CALL
        elif any(hint in callee_text for hint in EXTERNAL_PATH_HINTS):
            kind = I.EXTERNAL_CALL
        elif name in CPI_CALLS and receiver:
            kind = I.EXTERNAL_CALL
        elif receiver and receiver.split("::")[0] in self.storage_items:
            kind = I.BUILTIN_CALL
        elif receiver and receiver not in {"Self", "self"}:
            kind = I.EXTERNAL_CALL if receiver[:1].isupper() else I.INTERNAL_CALL

        return I.IRCall(
            name, kind, self.line(node), self.flat(node), receiver,
            tuple(self.expr(a) for a in self._arguments(node)),
            "value" in callee_text or name in {"transfer", "invoke", "invoke_signed"},
        )

    def _arguments(self, node) -> list:
        arguments = self.field(node, "arguments")
        return list(arguments.named_children) if arguments is not None else []

    def _storage_target(self, node) -> tuple[str, str, list] | None:
        """Recognise `Item::<T>::method(args)` on a known storage item."""
        function_node = self.field(node, "function")
        if function_node is None:
            return None
        callee = re.sub(r"<[^>]*>", "", self.flat(function_node))
        segments = [s for s in callee.split("::") if s]
        if len(segments) < 2:
            return None
        method = segments[-1]
        item = segments[0]
        if item not in self.storage_items:
            return None
        if method not in STORAGE_WRITE_METHODS and method not in STORAGE_READ_METHODS:
            return None
        return item, method, self._arguments(node)

    def _closure_delta(self, arguments) -> tuple[str, object] | None:
        """Pull `|b| *b += amount` out of a `mutate` call."""
        for argument in arguments:
            if argument.type != "closure_expression":
                continue
            body = self.field(argument, "body") or (
                argument.named_children[-1] if argument.named_child_count else None
            )
            for candidate in self._walk(body):
                if candidate.type == "compound_assignment_expr":
                    operator = self._compound_operator(candidate)
                    return operator, self.field(candidate, "right")
                if candidate.type == "assignment_expression":
                    return "=", self.field(candidate, "right")
        return None

    @staticmethod
    def _walk(node):
        if node is None:
            return
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(current.named_children))

    @staticmethod
    def _compound_operator(node) -> str:
        for child in node.children:
            if not child.is_named and child.type.endswith("=") and len(child.type) <= 3:
                return child.type
        return "+="

    # -- statement lowering ----------------------------------------------
    def block(self, node) -> tuple[I.IRStmt, ...]:
        if node is None:
            return ()
        out: list[I.IRStmt] = []
        for child in node.named_children:
            statement = self.statement(child)
            if statement is not None:
                out.append(statement)
        return tuple(out)

    def statement(self, node):
        if node is None or node.type in {"line_comment", "block_comment", "attribute_item"}:
            return None
        if node.type == "expression_statement":
            inner = node.named_children[0] if node.named_child_count else None
            return self.statement(inner)
        handler = getattr(self, f"_st_{node.type}", None)
        if handler is not None:
            return handler(node)
        return I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node),
                        reads=self.state_in(node))

    def _st_let_declaration(self, node):
        value = self.field(node, "value")
        return I.IRStmt(
            I.VAR_DECL, self.line(node), self.flat(node),
            target=self.expr(self.field(node, "pattern")),
            value=self.expr(value) if value is not None else None,
            call=self._nested_call(value), reads=self.state_in(value),
        )

    def _nested_call(self, node):
        for candidate in self._walk(node):
            if candidate.type == "call_expression":
                return self.classify_call(candidate)
        return None

    def _assignment(self, node, operator: str):
        left = self.field(node, "left")
        right = self.field(node, "right")
        base = self.lvalue_base(left)
        reads = set(self.state_in(right))
        if operator != "=":
            reads |= set(self.state_in(left))
        return I.IRStmt(
            I.ASSIGN, self.line(node), self.flat(node),
            target=self.expr(left), operator=operator, value=self.expr(right),
            call=self._nested_call(right),
            writes=(base,) if base in self.state_names else (),
            reads=tuple(sorted(reads)),
        )

    def _st_assignment_expression(self, node):
        return self._assignment(node, "=")

    def _st_compound_assignment_expr(self, node):
        return self._assignment(node, self._compound_operator(node))

    def _st_macro_invocation(self, node):
        name = self.text(self.field(node, "macro") or self.child(node, "identifier"))
        name = name.split("::")[-1]
        if name in GUARD_MACROS:
            return I.IRStmt(I.REQUIRE, self.line(node), self.flat(node),
                            condition=self.expr(node), reads=self.state_in(node),
                            note=name)
        return I.IRStmt(I.CALL, self.line(node), self.flat(node),
                        call=I.IRCall(name, I.INTERNAL_CALL, self.line(node),
                                      self.flat(node), None, (), False),
                        reads=self.state_in(node))

    def _st_try_expression(self, node):
        # `foo(..)?` — the `?` is error propagation, the call is the operation.
        inner = node.named_children[0] if node.named_child_count else None
        return self.statement(inner) if inner is not None else None

    def _unwrap_combinators(self, node):
        """Descend a `.map(..).ok_or(..)` chain to the call that does the work."""
        current = node
        for _ in range(8):
            function_node = self.field(current, "function")
            if function_node is None:
                return current
            name = re.split(r"::|\.", re.sub(r"<[^>]*>", "", self.flat(function_node)))[-1]
            if name.strip() not in RUST_COMBINATORS:
                return current
            inner = None
            for candidate in self._walk(function_node):
                if candidate.type == "call_expression":
                    inner = candidate
                    break
            if inner is None:
                return current
            current = inner
        return current

    def _st_call_expression(self, node):
        node = self._unwrap_combinators(node)
        storage = self._storage_target(node)
        if storage is not None:
            return self._storage_statement(node, *storage)
        call = self.classify_call(node)
        if call is not None and call.callee in GUARD_MACROS:
            return I.IRStmt(I.REQUIRE, self.line(node), self.flat(node),
                            condition=self.expr(node), reads=self.state_in(node))
        return I.IRStmt(I.CALL, self.line(node), self.flat(node), call=call,
                        reads=self.state_in(node))

    def _storage_statement(self, node, item: str, method: str, arguments: list):
        if method in STORAGE_READ_METHODS:
            return I.IRStmt(
                I.CALL, self.line(node), self.flat(node),
                call=I.IRCall(method, I.BUILTIN_CALL, self.line(node),
                              self.flat(node), item, (), False),
                reads=(item,), note=f"storage-read:{method}",
            )
        operation = STORAGE_WRITE_METHODS[method]
        value_node = arguments[-1] if arguments else None
        operator = "="
        if operation == "mutate":
            delta = self._closure_delta(arguments)
            if delta is not None:
                operator, value_node = delta
            else:
                operator = "?="
                self.unsupported.append(
                    f"unmodelled storage mutation {item}::{method} "
                    f"at line {self.line(node)}"
                )
        elif operation == "clear":
            operator, value_node = "=", None
        elif operation == "+=":
            operator = "+="
        return I.IRStmt(
            I.ASSIGN, self.line(node), self.flat(node),
            target=I.IRExpr("identifier", item, item, line=self.line(node)),
            operator=operator,
            value=self.expr(value_node) if value_node is not None
            else I.IRExpr("number_literal", "0", None, line=self.line(node)),
            writes=(item,), reads=tuple(sorted(set(self.state_in(value_node)) | {item}
                                               if operator != "=" else self.state_in(value_node))),
            note=f"storage-write:{method}",
        )

    def _st_if_expression(self, node):
        condition = self.field(node, "condition")
        consequence = self.field(node, "consequence")
        alternative = self.field(node, "alternative")
        return I.IRStmt(
            I.IF, self.line(node), self.flat(condition or node),
            condition=self.expr(condition), body=self.block(consequence),
            orelse=self.block(alternative.named_children[0])
            if alternative is not None and alternative.named_child_count else (),
            reads=self.state_in(condition),
        )

    def _st_match_expression(self, node):
        body = self.field(node, "body")
        arms: tuple[I.IRStmt, ...] = ()
        if body is not None:
            for arm in body.named_children:
                if arm.type != "match_arm":
                    continue
                value = self.field(arm, "value")
                arms = arms + (self.block(value) if value is not None
                               and value.type == "block" else
                               tuple(x for x in [self.statement(value)] if x))
        return I.IRStmt(
            I.IF, self.line(node), self.flat(self.field(node, "value") or node),
            condition=self.expr(self.field(node, "value")), body=arms,
            reads=self.state_in(self.field(node, "value")), note="match",
        )

    def _loop(self, node, condition_node, body_node, note):
        return I.IRStmt(
            I.LOOP, self.line(node), self.flat(condition_node or node),
            condition=self.expr(condition_node), body=self.block(body_node),
            reads=self.state_in(condition_node), note=note,
            loop_bound=_range_bound(self.flat(condition_node)),
        )

    def _st_for_expression(self, node):
        return self._loop(node, self.field(node, "value"), self.field(node, "body"), "for")

    def _st_while_expression(self, node):
        return self._loop(node, self.field(node, "condition"),
                          self.field(node, "body"), "while")

    def _st_loop_expression(self, node):
        return self._loop(node, None, self.field(node, "body"), "loop")

    def _st_block(self, node):
        return I.IRStmt(I.BLOCK, self.line(node), "block", body=self.block(node))

    def _st_unsafe_block(self, node):
        self.unsupported.append(f"unsafe block at line {self.line(node)}")
        return I.IRStmt(I.BLOCK, self.line(node), "unsafe block",
                        body=self.block(self.child(node, "block")), note="unsafe")

    def _st_return_expression(self, node):
        value = node.named_children[0] if node.named_child_count else None
        return I.IRStmt(I.RETURN, self.line(node), self.flat(node),
                        value=self.expr(value) if value is not None else None,
                        reads=self.state_in(node))

    # -- declarations ----------------------------------------------------
    def parameters(self, node) -> list[Parameter]:
        parameters = self.field(node, "parameters")
        if parameters is None:
            return []
        out: list[Parameter] = []
        for child in parameters.named_children:
            if child.type == "self_parameter":
                out.append(Parameter("self", self.flat(child)))
            elif child.type == "parameter":
                out.append(Parameter(
                    self.text(self.field(child, "pattern")),
                    self.flat(self.field(child, "type")),
                ))
        return out

    def build_function(self, node, contract_name: str, kind: str,
                       visibility: str, attributes: list[str]) -> Function:
        name = self.text(self.field(node, "name"))
        body_node = self.field(node, "body")
        outer_unsupported = list(self.unsupported)
        self.unsupported = []
        statements = self.block(body_node)
        function_unsupported = tuple(self.unsupported)
        self.unsupported = outer_unsupported

        writes: set[str] = set()
        calls: list[str] = []
        external = False
        for statement in statements:
            for sub in statement.walk():
                writes |= set(sub.writes)
                if sub.call is not None:
                    if sub.call.kind in I.EXTERNAL_CALL_KINDS:
                        external = True
                    if sub.call.callee:
                        calls.append(sub.call.callee)
        reads = set(self.identifiers(body_node)) & self.state_names
        reads |= writes

        return_node = self.field(node, "return_type")
        function = Function(
            contract_name, name, visibility,
            "view" if not writes and not external else "stateful",
            sorted({a.split("(")[0] for a in attributes})[:16], reads, writes,
            (["<low-level-call>"] if external else []) + sorted(set(calls))[:32],
            self.line(node), self.text(body_node),
        )
        function.params = self.parameters(node)
        function.returns = (
            [Parameter("", self.flat(return_node))] if return_node is not None else []
        )
        function.kind = kind
        function.language = RUST
        function.parser = PARSER_NAME
        function.path = self.path
        function.end_line = node.end_point[0] + 1
        function.is_test = (
            self.is_test_file or self.test_depth > 0 or _is_test_attribute(attributes)
        )
        # `origin` is supplied by the dispatch layer, not chosen by the caller;
        # `self` is the decoded extension. Everything else on an entry point is
        # attacker-chosen.
        function.user_inputs = [
            parameter.name for parameter in function.params
            if parameter.name and parameter.name not in {"self", "origin", "_origin"}
        ] if kind in {"extrinsic", "instruction"} or visibility in {"external", "public"} else []
        function.ir = I.IRFunctionBody(
            statements=statements, has_assembly=False,
            unsupported=function_unsupported, source=self.text(body_node),
        )
        return function

    def storage_variable(self, node, contract_name: str, attributes: list[str]) -> StateVar:
        name = self.text(self.field(node, "name") or self.child(node, "type_identifier"))
        type_node = self.field(node, "type")
        type_text = self.flat(type_node)
        keys: list[str] = []
        value_type = ""
        arguments = re.search(r"<(.*)>", type_text, re.DOTALL)
        if arguments:
            parts = [p.strip() for p in _split_generics(arguments.group(1))]
            parts = [p for p in parts if p not in {"_"}]
            if type_text.startswith("StorageMap") and len(parts) >= 3:
                keys, value_type = [parts[1]], parts[2]
            elif type_text.startswith("StorageDoubleMap") and len(parts) >= 5:
                keys, value_type = [parts[1], parts[3]], parts[4]
            elif parts:
                value_type = parts[0]
        return StateVar(
            contract_name, name, type_text, "storage", self.line(node),
            key_types=keys, value_type=value_type, language=RUST,
        )

    # -- item traversal ---------------------------------------------------
    def items_with_attributes(self, node):
        """Yield (item, attributes) pairs; attributes precede items in Rust."""
        pending: list[str] = []
        for child in node.named_children:
            if child.type == "attribute_item":
                pending.append(self.flat(self.child(child, "attribute") or child))
                continue
            if child.type in {"line_comment", "block_comment"}:
                continue
            yield child, pending
            pending = []

    def run(self, root) -> list[Contract]:
        contracts: list[Contract] = []
        structs: dict[str, list] = {}
        struct_attributes: dict[str, list[str]] = {}
        impls: list[tuple] = []

        for item, attributes in self.items_with_attributes(root):
            if item.type == "mod_item":
                contracts.extend(self.module(item, attributes))
            elif item.type == "struct_item":
                name = self.text(self.field(item, "name"))
                structs[name] = list(self._fields(item))
                struct_attributes[name] = attributes
            elif item.type == "impl_item":
                impls.append((item, attributes))
            elif item.type == "type_item":
                self._collect_wiring(item)
            elif item.type == "macro_invocation":
                self._collect_runtime_macro(item)

        contracts.extend(self.plain_impls(impls, structs, struct_attributes))
        return contracts

    def _collect_config_bindings(self, body, trait_name: str, target: str) -> None:
        """`impl pallet_x::Config for Runtime { type Y = Concrete; }`.

        This is the only place the runtime says which concrete type a pallet's
        `T::Y` actually is, so without it every cross-pallet call through an
        associated type dead-ends.
        """
        if not trait_name or trait_name.rsplit("::", 1)[-1] != "Config":
            return
        module = trait_name.rsplit("::", 1)[0] or trait_name
        for member in body.named_children:
            if member.type != "type_item":
                continue
            name = self.text(self.field(member, "name")
                             or self.child(member, "type_identifier"))
            value = self.field(member, "type")
            if not name or value is None:
                continue
            self.bindings.append(ConfigBinding(
                module, name, self.flat(value), target, self.path, self.line(member),
            ))

    def _collect_wiring(self, item) -> None:
        """`pub type TxExtension = (A<R>, B<R>, ...)` — an ordered pipeline.

        Every entry runs on every transaction, so this tuple is where two
        modules that disagree about what is allowed actually meet.
        """
        name = self.text(self.field(item, "name") or self.child(item, "type_identifier"))
        type_node = self.field(item, "type")
        if not name or type_node is None or type_node.type != "tuple_type":
            return
        # Comments are named children of a tuple type. Counting them shifts
        # every index after the first comment, and the index IS the ordering
        # claim this record exists to make.
        members = [
            re.sub(r"<[^>]*>", "", self.flat(child)).strip()
            for child in type_node.named_children
            if child.type not in {"line_comment", "block_comment", "attribute_item"}
        ]
        members = [member for member in members
                   if member and not member.startswith(("//", "/*", "\\"))]
        if len(members) < 2:
            return
        self.wirings.append(RuntimeWiring(
            name, "extension-pipeline", members, self.path, self.line(item), RUST,
        ))

    def _collect_runtime_macro(self, item) -> None:
        """`construct_runtime! { ... }` — the pallet composition and its order."""
        macro = self.text(self.field(item, "macro") or self.child(item, "identifier"))
        if macro.split("::")[-1] != "construct_runtime":
            return
        body = self.flat(item)
        members = [
            match.group(1) for match in
            re.finditer(r"(?m)^\s*(?:#\[[^\]]*\]\s*)?([A-Z]\w*)\s*:\s*[A-Za-z_]",
                        self.text(item))
        ]
        if not members:
            members = re.findall(r"\b([A-Z]\w*)\s*:\s*[a-z_]+::", body)
        if len(members) < 2:
            return
        self.wirings.append(RuntimeWiring(
            "construct_runtime", "runtime-pallets", members, self.path,
            self.line(item), RUST,
        ))

    def _fields(self, item):
        body = self.field(item, "body")
        if body is None:
            return
        if body.type == "ordered_field_declaration_list":
            # Tuple struct: fields are positional, addressed as `self.0`.
            # `ChargeTransactionPayment(#[codec(compact)] BalanceOf<T>)` holds
            # the transaction tip in field 0, so missing these loses the input.
            position = 0
            for field_node in body.named_children:
                if field_node.type in {"attribute_item", "visibility_modifier"}:
                    continue
                yield (str(position), self.flat(field_node), self.line(field_node))
                position += 1
            return
        for field_node in body.named_children:
            if field_node.type == "field_declaration":
                yield (
                    self.text(self.field(field_node, "name")),
                    self.flat(self.field(field_node, "type")),
                    self.line(field_node),
                )

    def module(self, node, attributes: list[str]) -> list[Contract]:
        body = self.field(node, "body")
        if body is None:
            return []
        module_name = self.text(self.field(node, "name"))
        is_pallet = _has_attribute(attributes, "pallet", "frame_support::pallet")
        is_program = _has_attribute(attributes, "program")

        test_module = (
            _is_test_attribute(attributes) or module_name.lower() in TEST_MODULE_NAMES
        )
        if test_module:
            self.test_depth += 1
        try:
            return self._module_body(node, body, module_name, attributes,
                                     is_pallet, is_program)
        finally:
            if test_module:
                self.test_depth -= 1

    def _module_body(self, node, body, module_name, attributes,
                     is_pallet, is_program) -> list[Contract]:
        if not (is_pallet or is_program):
            # A plain `mod` still holds real types. Dropping its impls lost every
            # struct declared inside a module, production or fixture alike.
            nested: list[Contract] = []
            structs: dict[str, list] = {}
            struct_attributes: dict[str, list[str]] = {}
            impls: list[tuple] = []
            for item, item_attributes in self.items_with_attributes(body):
                if item.type == "mod_item":
                    nested.extend(self.module(item, item_attributes))
                elif item.type == "struct_item":
                    struct_name = self.text(self.field(item, "name"))
                    structs[struct_name] = list(self._fields(item))
                    struct_attributes[struct_name] = item_attributes
                elif item.type == "impl_item":
                    impls.append((item, item_attributes))
            nested.extend(self.plain_impls(impls, structs, struct_attributes))
            return nested

        name = module_name
        if module_name in {"pallet", "program"}:
            name = _module_label(self.path)
        contract = Contract(
            name, self.path, self.line(node),
            kind="pallet" if is_pallet else "program", language=RUST,
            end_line=node.end_point[0] + 1, parser=PARSER_NAME,
        )

        members = list(self.items_with_attributes(body))
        for item, item_attributes in members:
            if item.type == "type_item" and _has_attribute(item_attributes, "pallet::storage"):
                variable = self.storage_variable(item, contract.name, item_attributes)
                contract.state_vars.append(variable)
            elif item.type == "struct_item" and _has_attribute(item_attributes, "account"):
                for field_name, field_type, line in self._fields(item):
                    contract.state_vars.append(StateVar(
                        contract.name, field_name, field_type, "account", line,
                        language=RUST,
                    ))
            elif item.type == "enum_item":
                variants = [
                    self.text(self.field(variant, "name") or variant)
                    for variant in (self.field(item, "body").named_children
                                    if self.field(item, "body") is not None else [])
                    if variant.type == "enum_variant"
                ]
                if _has_attribute(item_attributes, "pallet::event", "event"):
                    for variant in variants:
                        contract.events.append(ContractEvent(variant, [], self.line(item)))
                elif _has_attribute(item_attributes, "pallet::error", "error_code"):
                    for variant in variants:
                        contract.errors.append(ContractError(variant, [], self.line(item)))
                else:
                    contract.types.append(ContractType(
                        self.text(self.field(item, "name")), "enum", variants,
                        self.line(item),
                    ))

        self.storage_items = {v.name: v for v in contract.state_vars}
        self.state_names = set(self.storage_items)

        for item, item_attributes in members:
            if item.type != "impl_item":
                continue
            entry = (
                _has_attribute(item_attributes, "pallet::call") or is_program
            )
            for member, member_attributes in self.items_with_attributes(
                self.field(item, "body") or item
            ):
                if member.type != "function_item":
                    continue
                public = self.child(member, "visibility_modifier") is not None
                kind = "extrinsic" if (entry and is_pallet) else (
                    "instruction" if (entry and is_program) else "function"
                )
                visibility = "external" if (entry and public) else (
                    "public" if public else "internal"
                )
                contract.functions.append(self.build_function(
                    member, contract.name, kind, visibility,
                    item_attributes + member_attributes,
                ))

        self.storage_items = {}
        self.state_names = set()
        contract.module = module_name
        contract.is_test = self.is_test_file or self.test_depth > 0
        if contract.is_test:
            for function in contract.functions:
                function.is_test = True
        return [contract]

    def plain_impls(self, impls, structs, struct_attributes) -> list[Contract]:
        grouped: dict[str, Contract] = {}
        for item, attributes in impls:
            type_node = self.field(item, "type")
            type_name = re.sub(r"<[^>]*>", "", self.flat(type_node)).strip()
            if not type_name or type_name in {"Pallet", "Self"}:
                continue
            contract = grouped.get(type_name)
            if contract is None:
                fields = structs.get(type_name, [])
                own_attributes = struct_attributes.get(type_name, [])
                contract = Contract(
                    type_name, self.path, self.line(item), kind=(
                        "account" if _has_attribute(own_attributes, "account") else "struct"
                    ),
                    language=RUST, end_line=item.end_point[0] + 1, parser=PARSER_NAME,
                )
                contract.state_vars = [
                    StateVar(type_name, field_name, field_type, "field", line,
                             language=RUST)
                    for field_name, field_type, line in fields
                ]
                grouped[type_name] = contract

            trait_node = self.field(item, "trait")
            trait_name = ""
            if trait_node is not None:
                trait_name = re.sub(r"<[^>]*>", "", self.flat(trait_node)).strip()
                contract.bases.append(trait_name)
                contract.traits.append(trait_name)
                if any(marker in trait_name for marker in USER_DECODED_TRAITS):
                    # The trait contract says this type is decoded from the
                    # transaction, so its fields are attacker-chosen.
                    contract.user_decoded = True

            self.state_names = {v.name for v in contract.state_vars}
            body = self.field(item, "body")
            if body is not None:
                self._collect_config_bindings(body, trait_name, type_name)
                for member, member_attributes in self.items_with_attributes(body):
                    if member.type != "function_item":
                        continue
                    public = self.child(member, "visibility_modifier") is not None
                    contract.functions.append(self.build_function(
                        member, contract.name, "function",
                        "public" if public else "internal",
                        attributes + member_attributes,
                    ))
            self.state_names = set()

        for contract in grouped.values():
            # A type is a fixture only when EVERYTHING in it is. One
            # benchmark-gated method does not make a production adapter a mock.
            contract.is_test = self.is_test_file or bool(
                contract.functions
                and all(function.is_test for function in contract.functions)
            )
            if self.is_test_file:
                for function in contract.functions:
                    function.is_test = True
        return list(grouped.values())


def _module_label(path: str) -> str:
    """`pallets/vault/src/lib.rs` -> `vault`, not `lib`."""
    source = Path(path)
    if source.stem not in {"lib", "mod", "main"}:
        return source.stem
    for parent in source.parents:
        if parent.name and parent.name not in {"src", "source"}:
            return parent.name
    return source.stem


def _split_generics(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _range_bound(header: str) -> int | None:
    match = re.search(r"\.\.=?\s*(\d+)", header or "")
    return int(match.group(1)) if match else None


def parse_text_detailed(text: str, path: str):
    """Return (contracts, wirings, bindings) — both are file-level, not per-type."""
    parser, _ = _load()
    if parser is None:
        return [], [], []
    source = text.encode("utf-8")
    tree = parser.parse(source)
    file_parser = _FileParser(source, str(path))
    contracts = file_parser.run(tree.root_node)
    return contracts, file_parser.wirings, file_parser.bindings


def parse_text(text: str, path: str) -> list[Contract]:
    return parse_text_detailed(text, path)[0]


def parse_file_detailed(path):
    return parse_text_detailed(
        Path(path).read_text(encoding="utf-8", errors="ignore"), str(path)
    )


def parse_file(path) -> list[Contract]:
    return parse_file_detailed(path)[0]


def parse_sources(paths) -> list[Contract]:
    out: list[Contract] = []
    for path in paths:
        if Path(path).suffix.lower() == ".rs":
            out.extend(parse_file(path))
    return out


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), PARSER_NAME, RUST)
