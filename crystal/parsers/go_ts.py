"""tree-sitter Go front-end.

Go has no contracts, no `msg.sender`, no storage and no `require`. Every one of
those has to be *decided*, and a wrong decision makes every detector
downstream silently meaningless. The decisions, and why:

CONTRACT
    A named struct type with a method set   -> Contract(kind="struct")
    A package                               -> Contract(kind="package")
    An interface                            -> Contract(kind="interface")

    The struct is the unit with state and operations on it — a use case
    holding its repositories, a watcher holding the quotes it tracks, a
    wallet holding its connection — which is exactly what the Rust front-end
    does with `impl Counter { .. }`. Its methods are siblings: two exported
    methods of one repository reaching the same collection write are the
    "two entry points of the same type" the guard-asymmetry detector compares.
    Package-level functions and `var`s need a home too (a handler factory, a
    validator, a registry map): the package is Go's own encapsulation unit, so
    it is the contract that owns them. Method-less structs (DTOs, entities)
    become `ContractType`s of the package, not contracts: they have no
    operations for a detector to compare.

    A struct's `bases` are the interfaces it satisfies — decided by *method
    signature* over the whole project, the way the compiler decides it, not
    by method name (a `Run`-only interface would otherwise match every use
    case) — plus the struct types it embeds (promoted fields and methods are
    inheritance in every sense Crystal cares about). This is what lets a call
    through an interface-typed field resolve to the implementation that will
    actually run, which is the only way the shared callee's `writes` can be
    known and the coupling grade can be anything but "unresolved".

ENTRY POINT
    HTTP handler  (signature `(http.ResponseWriter, *http.Request)`, or a
                   factory returning `http.HandlerFunc` whose returned closure
                   has that signature)                 -> visibility "external"
    Exported method / function                          -> visibility "public"
    Unexported                                          -> visibility "internal"

    A handler is reachable by whoever can send a request, whether or not it
    is exported: reachability is by wiring, not by capitalisation. An
    exported method is callable by any package, which is the same claim the
    Rust front-end makes for `pub fn` on a plain impl — and it is what makes
    two exported methods of one type the siblings the guard-asymmetry
    detector compares. Go has no constructor: a `NewX` factory is an ordinary
    function (`NewErrorResponse(..)` runs on every request), and one that only
    builds a value is `view`, so a call to it is never a state transition.

    What "public" does NOT claim: that an untrusted principal can call it.
    The trust boundary of a Go service is the network, so a detector whose
    premise is "any account may call this" (missing-access-control,
    reentrancy) reads an exported method as more exposed than it is; the
    guard-asymmetry and unbounded-input detectors do not depend on that
    premise and are the ones this front-end is for.

CALLS THAT ARE NOT TRANSITIONS
    Writing the response (`w.WriteHeader`, `http.Error`), reading the request
    or the context, Stringer/accessor methods (`String()`, `Cmp`), lock
    operations and pure stdlib are `builtin`; purity then propagates through
    internal calls once every file of the group is lowered (see
    `GoCatalog.finalize`). Without this every error-response helper became a
    "shared transition" and every handler's `errors.Is` branch a guard its
    sibling lacked — 33 signals on the engagement target, none real.

STATE
    Struct field                    -> StateVar(visibility="field")
    Package-level `var` / `const`   -> StateVar(visibility="package")

    A field is only ever reached through the receiver, so the receiver is
    spelled `self` in the IR text (`useCase.repo` -> `self.repo`), which is
    the spelling the language-neutral symbolic engine already understands.
    A mutex is *not* state and does not name the state it protects: Go has no
    lexical link between a lock and the fields it guards, so `Lock`/`Unlock`
    are recorded as builtin calls with a note and nothing is invented.

CALLS
    Same-package function, method on the receiver, project package  -> internal
    Through a field, a local, a parameter, or a third-party package -> external
    Builtins, conversions, pure stdlib, lock operations             -> builtin

    Dispatch through an interface-typed field hands control to code chosen at
    wiring time, the same way a Solidity call on an interface-typed variable
    does, so it is external and resolved through `bases` afterwards.

UNTRUSTED INPUT
    A `*http.Request` parameter is chosen by whoever sent it. A local filled
    by a decoder from that request (`DecodeRequest(w, req, &body)`) or read
    from it (`mux.Vars(req)["id"]`) is *derived* from it; the pointer write and
    the accessor call are both invisible to the symbolic engine, so the
    front-end lowers each into an explicit assignment (`body = req`, noted
    `decoded-from-request`) and the taint becomes real rather than asserted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .. import ir as I
from ..models import GO, Contract, ContractType, Function, Parameter, StateVar
from .base import ParseResult
from .go_regex import (
    CONTEXT_TYPES,
    DECODE_CALLS,
    GO_BUILTIN_FUNCS,
    GO_BUILTIN_TYPES,
    HTTP_RESPONSE_FUNCS,
    LOCK_METHODS,
    LOCK_NOTES,
    PURE_PACKAGES,
    REQUEST_TYPES,
    RESPONSE_TYPES,
    STRINGER_METHODS,
    TERMINATING_SELECTORS,
    classify_shape,
    declared_type,
    is_exported,
    is_test_file,
    module_path_for,
    package_label_for_path,
    typed_receiver,
    user_inputs_for,
)

PARSER_NAME = "tree-sitter"

_IDENTIFIER_TYPES = {"identifier", "field_identifier", "type_identifier", "package_identifier"}
_CONVERSION_FUNCTION_TYPES = {
    "slice_type", "array_type", "map_type", "pointer_type", "parenthesized_type",
    "channel_type", "function_type", "qualified_type", "generic_type", "struct_type",
    "interface_type",
}
_SKIP_STATEMENTS = {
    "comment", "break_statement", "continue_statement", "goto_statement",
    "fallthrough_statement", "empty_statement", "type_declaration",
}
_SWITCH_STATEMENTS = {
    "expression_switch_statement", "type_switch_statement", "select_statement",
}


@lru_cache(maxsize=1)
def _load():
    try:
        import tree_sitter_go
        from tree_sitter import Language, Parser
    except ImportError as exc:
        return None, f"tree-sitter Go grammar not installed ({exc}); regex fallback in use"
    try:
        return Parser(Language(tree_sitter_go.language())), "ok"
    except Exception as exc:  # pragma: no cover - depends on binding build
        return None, f"tree-sitter Go language failed to load: {exc}"


def available() -> bool:
    return _load()[0] is not None


def status() -> str:
    return _load()[1]


def backend_name() -> str:
    return "tree-sitter" if available() else "regex"


# ---------------------------------------------------------------------------
# Project catalog
# ---------------------------------------------------------------------------

@dataclass
class _TypeInfo:
    name: str
    kind: str                      # struct | interface | other
    label: str                     # package label
    path: str
    line: int
    end_line: int
    fields: list = field(default_factory=list)      # (name, type text, line, embedded)
    elems: dict = field(default_factory=dict)       # interface: name -> (sig, params, results, line)
    embedded: list = field(default_factory=list)    # embedded type texts
    underlying: str = ""


def _text(source: bytes, node) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _flat(source: bytes, node) -> str:
    return " ".join(_text(source, node).split())


def _strip_type(text: str) -> str:
    """`*Store[T]` -> `Store`; `pkg.Type` -> `pkg.Type`."""
    return re.sub(r"\[.*\]$", "", text.strip().lstrip("*&")).strip()


class GoCatalog:
    """What every file of the group declares, resolved before any file is lowered.

    Built once per scan over all Go files (the registry's `type_catalog` hook),
    it answers the questions no single file can: which package a directory is,
    which interfaces a struct satisfies, whether two packages declare the same
    type name, and which contract object a method belongs to when the struct
    is declared in another file.
    """

    def __init__(self, entries):
        parser, _ = _load()
        self.parser = parser
        self.sources: dict[str, bytes] = {}
        self.trees: dict[str, object] = {}
        self.package_name: dict[str, str] = {}
        self.package_dir: dict[str, str] = {}
        self.imports: dict[str, dict[str, str]] = {}
        self.labels: dict[str, str] = {}
        self.dir_import: dict[str, str] = {}
        self.import_labels: dict[str, str] = {}
        self.module_path = ""
        self.module_root = ""
        self.types: dict[tuple[str, str], _TypeInfo] = {}
        self.methods: dict[tuple[str, str], dict[str, tuple]] = {}
        self.functions: dict[tuple[str, str], int] = {}
        # Package-level variables by package label, across every file of the
        # package: a method in one file reads a `var` declared in another.
        self.package_vars: dict[str, set[str]] = {}
        self.names: dict[tuple[str, str], str] = {}
        self.bases: dict[tuple[str, str], list[str]] = {}
        self.contracts: dict[tuple[str, str], Contract] = {}
        self.packages: dict[str, Contract] = {}
        self.emitted: set = set()
        self._import_cache: dict[str, str] = {}
        # Every function lowered so far, and the files still to lower: the
        # purity fixpoint needs the whole group, so it runs when the last one
        # is done.
        self.built: list[tuple[str, Contract, Function]] = []
        self.pending: set[str] = set()

        for path, source in entries:
            path = str(path)
            self.sources[path] = source
            self.trees[path] = parser.parse(source) if parser is not None else None
        self.pending = set(self.sources)
        if parser is None:
            return
        self._collect_packages()
        self._assign_labels()
        for path in self.sources:
            self._collect_declarations(path)
        self._assign_names()
        self._compute_bases()

    # -- packages and labels ------------------------------------------------
    def _collect_packages(self) -> None:
        for path, tree in self.trees.items():
            directory = Path(path).parent.as_posix()
            self.package_dir[path] = directory
            name = ""
            for node in tree.root_node.named_children:
                if node.type == "package_clause":
                    identifier = node.named_children[0] if node.named_child_count else None
                    name = _text(self.sources[path], identifier)
                    break
            self.package_name[path] = name or Path(path).parent.name
        first = next(iter(self.sources), None)
        if first is not None:
            directory = Path(first).parent
            self.module_path = module_path_for(str(directory))
            if self.module_path:
                for candidate in [directory, *directory.parents]:
                    if (candidate / "go.mod").is_file():
                        self.module_root = candidate.as_posix()
                        break

    def _assign_labels(self) -> None:
        """One readable, unique label per package directory.

        The declared name is kept when it is unique. Two `watcher` packages
        become `entrypoints_watcher` and `usecases_watcher`; `main` says
        nothing on its own, so it is always qualified by its directory.
        """
        production: dict[str, str] = {}
        test_only: dict[str, str] = {}
        for path, name in self.package_name.items():
            directory = self.package_dir[path]
            if is_test_file(path):
                test_only.setdefault(directory, name)
            else:
                production[directory] = name
        # A directory holding nothing but tests is a fixture package; it gets
        # its own `_test` label and never forces a production package of the
        # same name to be qualified.
        for directory, name in test_only.items():
            if directory not in production:
                self.labels[directory] = name if name.endswith("_test") else f"{name}_test"
        groups: dict[str, list[str]] = {}
        for directory, name in production.items():
            groups.setdefault(name, []).append(directory)
        for name, directories in groups.items():
            if len(directories) == 1 and name != "main":
                self.labels[directories[0]] = name
                continue
            used: set[str] = set()
            for directory in sorted(directories):
                parts = [p for p in directory.split("/") if p and p != "."]
                tail = parts[:-1] if parts and parts[-1] == name else parts
                # Every member of a colliding group is qualified: a bare
                # `utils` next to `e_utils` would read as the unique one.
                candidates = ["_".join(tail[-depth:] + [name]) for depth in range(1, len(tail) + 1)]
                chosen = next((c for c in candidates if c not in used), None)
                if chosen is None:
                    chosen = f"{name}_{len(used)}"
                used.add(chosen)
                self.labels[directory] = chosen
        for directory, label in self.labels.items():
            if self.module_root:
                relative = Path(directory).as_posix()
                root = self.module_root
                if relative.startswith(root):
                    relative = relative[len(root):].strip("/")
                    import_path = f"{self.module_path}/{relative}" if relative else self.module_path
                    self.dir_import[directory] = import_path
                    self.import_labels[import_path] = label

    def label_of(self, path: str) -> str:
        directory = self.package_dir.get(str(path), Path(path).parent.as_posix())
        label = self.labels.get(directory)
        if label is None:
            label = package_label_for_path(path, self.package_name.get(str(path), ""))
        return label

    def module_of(self, path: str) -> str:
        directory = self.package_dir.get(str(path), Path(path).parent.as_posix())
        return self.dir_import.get(directory) or directory

    def label_for_import(self, import_path: str) -> str | None:
        """The project package an import path names, or None for a foreign one.

        A module-prefixed import names a project package even when none of its
        files are in the scanned set (a partial scan, a test fixture): the call
        still goes to code the project owns, so it is internal, labelled by
        the path's last segment.
        """
        if not import_path:
            return None
        if import_path in self._import_cache:
            return self._import_cache[import_path] or None
        found = self.import_labels.get(import_path)
        if found is None and self.module_path and (
                import_path == self.module_path
                or import_path.startswith(self.module_path + "/")):
            found = import_path.rsplit("/", 1)[-1]
        if found is None and not self.module_path:
            for directory, label in sorted(self.labels.items(), key=lambda x: -len(x[0])):
                if import_path == directory or import_path.endswith("/" + directory):
                    found = label
                    break
        self._import_cache[import_path] = found or ""
        return found

    # -- declarations ---------------------------------------------------------
    def _collect_declarations(self, path: str) -> None:
        source = self.sources[path]
        tree = self.trees[path]
        label = self.label_of(path)
        self._imports_of(path)
        for node in tree.root_node.named_children:
            if node.type == "type_declaration":
                for spec in node.named_children:
                    if spec.type not in {"type_spec", "type_alias"}:
                        continue
                    self._collect_type(path, label, spec)
            elif node.type == "function_declaration":
                name = _text(source, node.child_by_field_name("name"))
                self.functions[(label, name)] = node.start_point[0] + 1
            elif node.type == "method_declaration":
                receiver_type = self._receiver_type(source, node)
                name = _text(source, node.child_by_field_name("name"))
                if receiver_type and name:
                    self.methods.setdefault((label, receiver_type), {})[name] = \
                        self._signature(path, node)
            elif node.type in {"var_declaration", "const_declaration"}:
                wanted = "const_spec" if node.type == "const_declaration" else "var_spec"
                for spec in self._walk_type(node, wanted):
                    for name_node in spec.children_by_field_name("name"):
                        name = _text(source, name_node)
                        if name and name != "_":
                            self.package_vars.setdefault(label, set()).add(name)

    @staticmethod
    def _walk_type(node, wanted: str):
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == wanted:
                yield current
                continue
            stack.extend(reversed(current.named_children))

    def _receiver_type(self, source: bytes, node) -> str:
        receiver = node.child_by_field_name("receiver")
        if receiver is None:
            return ""
        for child in receiver.named_children:
            if child.type == "parameter_declaration":
                type_node = child.child_by_field_name("type")
                return _strip_type(_flat(source, type_node))
        return ""

    def _collect_type(self, path: str, label: str, spec) -> None:
        source = self.sources[path]
        name = _text(source, spec.child_by_field_name("name"))
        type_node = spec.child_by_field_name("type")
        if not name or type_node is None:
            return
        kind = {"struct_type": "struct", "interface_type": "interface"}.get(type_node.type, "other")
        info = _TypeInfo(name, kind, label, path, spec.start_point[0] + 1,
                         spec.end_point[0] + 1, underlying=_flat(source, type_node)[:120])
        if kind == "struct":
            body = next((c for c in type_node.named_children
                         if c.type == "field_declaration_list"), None)
            for declaration in (body.named_children if body is not None else []):
                if declaration.type != "field_declaration":
                    continue
                names = declaration.children_by_field_name("name")
                field_type = _flat(source, declaration.child_by_field_name("type"))
                line = declaration.start_point[0] + 1
                if not names:
                    info.embedded.append(field_type)
                    info.fields.append((_strip_type(field_type).rsplit(".", 1)[-1],
                                        field_type, line, True))
                    continue
                for name_node in names:
                    info.fields.append((_text(source, name_node), field_type, line, False))
        elif kind == "interface":
            for member in type_node.named_children:
                if member.type == "method_elem":
                    elem_name = _text(source, member.child_by_field_name("name"))
                    info.elems[elem_name] = (
                        self._signature(path, member),
                        _flat(source, member.child_by_field_name("parameters")),
                        _flat(source, member.child_by_field_name("result")),
                        member.start_point[0] + 1,
                    )
                elif member.type in {"type_elem", "type_identifier", "qualified_type"}:
                    info.embedded.append(_flat(source, member))
        self.types[(label, name)] = info

    # -- signatures -----------------------------------------------------------
    def canonical_type(self, text: str, path: str) -> str:
        """Spell a type so the same type reads the same from every package."""
        label = self.label_of(path)
        aliases = self.imports.get(path) or self._imports_of(path)

        def qualified(match):
            alias, name = match.group(1), match.group(2)
            target = self.label_for_import(aliases.get(alias, "")) if alias in aliases else None
            return f"{target}.{name}" if target else f"{alias}.{name}"

        text = re.sub(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)", qualified, text)

        def bare(match):
            name = match.group(1)
            return f"{label}.{name}" if (label, name) in self.types else name

        text = re.sub(r"(?<![\w.])([A-Z]\w*)(?![\w.])", bare, text)
        return " ".join(text.split())

    def _imports_of(self, path: str) -> dict[str, str]:
        """Alias -> import path for one file, computed once."""
        if path in self.imports:
            return self.imports[path]
        source = self.sources.get(path)
        tree = self.trees.get(path)
        if source is None or tree is None:
            return {}
        aliases: dict[str, str] = {}
        for node in tree.root_node.named_children:
            if node.type != "import_declaration":
                continue
            for spec in self._walk_type(node, "import_spec"):
                import_path = _text(source, spec.child_by_field_name("path")).strip('"')
                name_node = spec.child_by_field_name("name")
                alias = _text(source, name_node) if name_node is not None else ""
                if alias in {"_", "."}:
                    continue
                aliases[alias or import_path.rsplit("/", 1)[-1]] = import_path
        self.imports[path] = aliases
        return aliases

    def _signature(self, path: str, node) -> tuple:
        source = self.sources[path]
        params: list[str] = []
        parameters = node.child_by_field_name("parameters")
        for declaration in (parameters.named_children if parameters is not None else []):
            if declaration.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
                continue
            type_text = self.canonical_type(_flat(source, declaration.child_by_field_name("type")), path)
            if declaration.type == "variadic_parameter_declaration":
                type_text = "..." + type_text
            count = max(1, len(declaration.children_by_field_name("name")))
            params.extend([type_text] * count)
        results: list[str] = []
        result = node.child_by_field_name("result")
        if result is not None:
            if result.type == "parameter_list":
                for declaration in result.named_children:
                    if declaration.type != "parameter_declaration":
                        continue
                    type_text = self.canonical_type(
                        _flat(source, declaration.child_by_field_name("type")), path)
                    count = max(1, len(declaration.children_by_field_name("name")))
                    results.extend([type_text] * count)
            else:
                results.append(self.canonical_type(_flat(source, result), path))
        return tuple(params), tuple(results)

    # -- names and bases ------------------------------------------------------
    def _assign_names(self) -> None:
        """Contract names: bare where unique, `Name@package` where two packages
        declare the same struct or interface.

        The usual Go collision is a consumer declaring the interface it needs
        under the name of the type that satisfies it (`handlers.AcceptQuoteUseCase`
        for `pegin.AcceptQuoteUseCase`). Fields are typed by the bare name, so
        the single interface keeps it and every struct is qualified: a lookup
        by type name then lands on the interface, whose implementations carry
        it in `bases`.
        """
        by_name: dict[str, list[tuple[str, str]]] = {}
        for key, info in self.types.items():
            if info.kind in {"struct", "interface"}:
                by_name.setdefault(key[1], []).append(key)
        for key, info in self.types.items():
            label, name = key
            peers = by_name.get(name, [])
            if len(peers) <= 1:
                self.names[key] = name
                continue
            interfaces = [k for k in peers if self.types[k].kind == "interface"]
            if info.kind == "interface" and len(interfaces) == 1:
                self.names[key] = name
            else:
                self.names[key] = f"{name}@{label}"

    def resolve_type(self, text: str, path: str) -> tuple[str, str] | None:
        """`quote.PeginQuoteRepository` from `path` -> its catalog key, if project-declared."""
        cleaned = _strip_type(text)
        if "." in cleaned:
            alias, name = cleaned.rsplit(".", 1)
            aliases = self.imports.get(path) or {}
            label = self.label_for_import(aliases.get(alias, ""))
            if label and (label, name) in self.types:
                return label, name
            return None
        label = self.label_of(path)
        return (label, cleaned) if (label, cleaned) in self.types else None

    def method_set(self, key: tuple[str, str], depth: int = 0) -> dict[str, tuple]:
        found = dict(self.methods.get(key, {}))
        info = self.types.get(key)
        if info is None or depth >= 2:
            return found
        for embedded in info.embedded:
            target = self.resolve_type(embedded, info.path)
            if target is not None and target != key:
                for name, signature in self.method_set(target, depth + 1).items():
                    found.setdefault(name, signature)
        return found

    def interface_set(self, key: tuple[str, str], depth: int = 0) -> dict[str, tuple]:
        info = self.types.get(key)
        if info is None or info.kind != "interface":
            return {}
        found = {name: elem[0] for name, elem in info.elems.items()}
        if depth >= 4:
            return found
        for embedded in info.embedded:
            target = self.resolve_type(embedded, info.path)
            if target is not None and target != key:
                for name, signature in self.interface_set(target, depth + 1).items():
                    found.setdefault(name, signature)
        return found

    def _compute_bases(self) -> None:
        interfaces = {
            key: self.interface_set(key)
            for key, info in self.types.items() if info.kind == "interface"
        }
        interfaces = {key: elems for key, elems in interfaces.items() if elems}
        for key, info in self.types.items():
            if info.kind != "struct":
                continue
            bases: list[str] = []
            for embedded in info.embedded:
                target = self.resolve_type(embedded, info.path)
                bases.append(self.names[target] if target else _strip_type(embedded).rsplit(".", 1)[-1])
            methods = self.method_set(key)
            if methods:
                for iface_key, required in interfaces.items():
                    if all(methods.get(name) == signature for name, signature in required.items()):
                        contract_name = self.names[iface_key]
                        bases.append(contract_name)
                        if contract_name != iface_key[1]:
                            # A field is typed by the bare name; keep it resolvable.
                            bases.append(iface_key[1])
            self.bases[key] = list(dict.fromkeys(bases))

    # -- contracts ------------------------------------------------------------
    def package_contract(self, label: str, path: str, is_test: bool) -> tuple[Contract, bool]:
        contract = self.packages.get(label)
        if contract is not None:
            return contract, False
        contract = Contract(label, str(path), 1, kind="package", language=GO,
                            parser=PARSER_NAME, module=self.module_of(str(path)),
                            is_test=is_test)
        self.packages[label] = contract
        return contract, True

    def contract_for(self, key: tuple[str, str], path: str = "") -> tuple[Contract, bool]:
        contract = self.contracts.get(key)
        if contract is not None:
            return contract, False
        info = self.types.get(key)
        if info is None:
            label, name = key
            info = _TypeInfo(name, "struct", label, path, 0, 0)
        contract = Contract(
            self.names.get(key, info.name), info.path, info.line, kind=info.kind,
            language=GO, parser=PARSER_NAME, end_line=info.end_line,
            module=self.module_of(info.path), is_test=is_test_file(info.path),
        )
        contract.bases = list(self.bases.get(key, []))
        contract.traits = [b for b in contract.bases if b not in
                           {_strip_type(e).rsplit(".", 1)[-1] for e in info.embedded}]
        for name, type_text, line, embedded in info.fields:
            contract.state_vars.append(StateVar(
                contract.name, name, type_text, "field", line, language=GO,
                key_types=_map_keys(type_text), value_type=_map_value(type_text),
            ))
        if info.kind == "interface":
            for name, (signature, params, results, line) in info.elems.items():
                function = Function(
                    contract.name, name, "public" if is_exported(name) else "internal",
                    "stateful", [], set(), set(), [], line, "",
                )
                function.params = _parameters_from_text(params)
                function.returns = [Parameter("", results)] if results else []
                function.kind = "method"
                function.language = GO
                function.parser = PARSER_NAME
                function.path = info.path
                function.is_test = contract.is_test
                contract.functions.append(function)
        self.contracts[key] = contract
        return contract, True


    # -- purity ---------------------------------------------------------------
    def mark_parsed(self, path: str) -> None:
        self.pending.discard(str(path))
        if not self.pending:
            self.finalize()

    def finalize(self) -> None:
        """Propagate purity through internal calls, over the whole group.

        A function is `view` when it writes no state and every call it makes
        is a builtin or an internal call to a function that is itself `view`
        — `NewErrorResponse(..)` builds a value, `JsonErrorResponse(..)` only
        calls builders and the response writer. Until this runs a function
        that merely *calls* a builder is `stateful`, which is the conservative
        answer; the fixpoint only ever upgrades.
        """
        by_owner: dict[str, dict[str, Function]] = {}
        for label, contract, function in self.built:
            by_owner.setdefault(contract.name, {})[function.name] = function
        candidates: dict[int, tuple[str, Function]] = {}
        for label, contract, function in self.built:
            if function.writes or function.ir is None:
                continue
            if any(call.kind not in {I.BUILTIN_CALL, I.INTERNAL_CALL} for call in function.ir.calls()):
                continue
            candidates[id(function)] = (label, function)

        def target_of(label: str, function: Function, call: I.IRCall):
            if call.receiver is None:
                found = by_owner.get(function.contract, {}).get(call.callee)
                return found if found is not None else by_owner.get(label, {}).get(call.callee)
            return by_owner.get(call.receiver, {}).get(call.callee)

        changed = True
        while changed:
            changed = False
            for key, (label, function) in list(candidates.items()):
                for call in function.ir.calls():
                    if call.kind == I.BUILTIN_CALL:
                        continue
                    target = target_of(label, function, call)
                    if target is None or id(target) not in candidates or target is function:
                        if target is function:
                            continue
                        del candidates[key]
                        changed = True
                        break
        for key, (label, function) in candidates.items():
            function.mutability = "view"


def _parameters_from_text(text: str) -> list[Parameter]:
    from .go_regex import parse_params
    inner = text.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    return parse_params(inner)


def _map_keys(type_text: str) -> list[str]:
    match = re.match(r"map\[(.+?)\]", type_text or "")
    return [match.group(1)] if match else []


def _map_value(type_text: str) -> str:
    match = re.match(r"map\[(?:[^\[\]]|\[[^\]]*\])+\](.+)$", type_text or "")
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------------------
# Per-file lowering
# ---------------------------------------------------------------------------

class _FileParser:
    def __init__(self, source: bytes, path: str, catalog: GoCatalog, tree):
        self.source = source
        self.path = path
        self.catalog = catalog
        self.tree = tree
        self.label = catalog.label_of(path)
        self.imports = catalog.imports.get(path, {})
        self.is_test = is_test_file(path)
        # Set per function by `build_function`.
        self.receiver: str | None = None
        self.fields: set[str] = set()
        self.package_vars: set[str] = set()
        self.params: dict[str, str] = {}
        self.request_params: set[str] = set()
        self.locals: set[str] = set()
        self.unsupported: list[str] = []

    # -- text helpers ---------------------------------------------------------
    def text(self, node) -> str:
        return _text(self.source, node)

    def raw_flat(self, node) -> str:
        return _flat(self.source, node)

    def flat(self, node) -> str:
        """Whitespace-collapsed source with the receiver spelled `self`."""
        text = self.raw_flat(node)
        if self.receiver:
            text = re.sub(rf"(?<![\w.]){re.escape(self.receiver)}\.", "self.", text)
        return text[:400]

    @staticmethod
    def line(node) -> int:
        return node.start_point[0] + 1

    def identifiers(self, node) -> tuple[str, ...]:
        if node is None:
            return ()
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in _IDENTIFIER_TYPES:
                name = self.text(current)
                found.append("self" if name == self.receiver else name)
            stack.extend(reversed(current.named_children))
        return tuple(dict.fromkeys(found))

    def _walk(self, node):
        if node is None:
            return
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(current.named_children))

    def state_in(self, node) -> tuple[str, ...]:
        """Fields reached through the receiver, and package variables by name."""
        if node is None:
            return ()
        found: set[str] = set()
        for current in self._walk(node):
            if current.type == "selector_expression":
                operand = current.child_by_field_name("operand")
                if operand is not None and operand.type == "identifier" \
                        and self.text(operand) == self.receiver:
                    name = self.text(current.child_by_field_name("field"))
                    if name in self.fields:
                        found.add(name)
            elif current.type == "identifier":
                name = self.text(current)
                if name in self.package_vars and name != self.receiver:
                    found.add(name)
        return tuple(sorted(found))

    def expr(self, node) -> I.IRExpr:
        if node is None:
            return I.IRExpr("empty", "", None)
        return I.IRExpr(node.type, self.flat(node), self.lvalue_base(node),
                        identifiers=self.identifiers(node), line=self.line(node))

    def lvalue_base(self, node) -> str | None:
        """`useCase.repo[k]` -> `repo`; `*p` -> `p`; `x` -> `x`."""
        if node is None:
            return None
        if node.type == "selector_expression":
            operand = node.child_by_field_name("operand")
            if operand is not None and operand.type == "identifier" \
                    and self.text(operand) == self.receiver:
                return self.text(node.child_by_field_name("field")) or None
            return self.lvalue_base(operand)
        if node.type in {"index_expression", "slice_expression", "type_assertion_expression"}:
            return self.lvalue_base(node.child_by_field_name("operand"))
        if node.type in {"unary_expression", "parenthesized_expression"}:
            inner = node.child_by_field_name("operand") or (
                node.named_children[0] if node.named_child_count else None)
            return self.lvalue_base(inner)
        if node.type == "call_expression":
            return self.lvalue_base(node.child_by_field_name("function"))
        if node.type in _IDENTIFIER_TYPES:
            name = self.text(node)
            return "self" if name == self.receiver else name
        identifiers = self.identifiers(node)
        return identifiers[0] if identifiers else None

    def write_base(self, node) -> str | None:
        """The state variable an assignment target writes, or None."""
        if node is None:
            return None
        if node.type == "selector_expression":
            operand = node.child_by_field_name("operand")
            if operand is not None and operand.type == "identifier" \
                    and self.text(operand) == self.receiver:
                name = self.text(node.child_by_field_name("field"))
                return name if name in self.fields else None
            return self.write_base(operand)
        if node.type in {"index_expression", "slice_expression"}:
            return self.write_base(node.child_by_field_name("operand"))
        if node.type in {"unary_expression", "parenthesized_expression"}:
            inner = node.child_by_field_name("operand") or (
                node.named_children[0] if node.named_child_count else None)
            return self.write_base(inner)
        if node.type == "identifier":
            name = self.text(node)
            return name if name in self.package_vars else None
        return None

    def _root_identifier(self, node):
        """The identifier a member chain starts from: `a.b(c).d[e]` -> `a`."""
        current = node
        for _ in range(32):
            if current is None:
                return None
            if current.type == "identifier":
                return self.text(current)
            if current.type in {"selector_expression", "index_expression", "slice_expression",
                                "type_assertion_expression"}:
                current = current.child_by_field_name("operand")
            elif current.type == "call_expression":
                current = current.child_by_field_name("function")
            elif current.type in {"unary_expression", "parenthesized_expression"}:
                current = current.child_by_field_name("operand") or (
                    current.named_children[0] if current.named_child_count else None)
            else:
                return None
        return None

    # -- calls ----------------------------------------------------------------
    def _arguments(self, node) -> list:
        arguments = node.child_by_field_name("arguments")
        return list(arguments.named_children) if arguments is not None else []

    def classify_call(self, node) -> I.IRCall:
        function_node = node.child_by_field_name("function")
        line = self.line(node)
        text = self.flat(node)
        arguments = tuple(self.expr(a) for a in self._arguments(node))
        if function_node is None:
            return I.IRCall("", I.BUILTIN_CALL, line, text, None, arguments, False)

        if function_node.type == "identifier":
            name = self.text(function_node)
            if name in GO_BUILTIN_FUNCS or name in GO_BUILTIN_TYPES:
                kind = I.BUILTIN_CALL
            elif (self.label, name) in self.catalog.types:
                kind = I.BUILTIN_CALL          # a conversion, `Wei(x)`
            elif (self.label, name) in self.catalog.functions:
                kind = I.INTERNAL_CALL
            else:
                # A function value: a parameter, a local closure, a field.
                kind = I.EXTERNAL_CALL
            return I.IRCall(name, kind, line, text, None, arguments, False)

        if function_node.type == "selector_expression":
            operand = function_node.child_by_field_name("operand")
            name = self.text(function_node.child_by_field_name("field"))
            receiver, kind = self._receiver_kind(operand, name, len(arguments))
            return I.IRCall(name, kind, line, text, receiver, arguments, False)

        if function_node.type in {"generic_function", "index_expression"}:
            # `DecodeRequest[T](..)`: an explicit instantiation of a generic.
            inner = function_node.child_by_field_name("function") or \
                function_node.child_by_field_name("operand")
            if inner is not None and inner.type in {"identifier", "selector_expression"}:
                if inner.type == "identifier":
                    name = self.text(inner)
                    kind = I.INTERNAL_CALL if (self.label, name) in self.catalog.functions \
                        else I.EXTERNAL_CALL
                    return I.IRCall(name, kind, line, text, None, arguments, False)
                operand = inner.child_by_field_name("operand")
                name = self.text(inner.child_by_field_name("field"))
                receiver, kind = self._receiver_kind(operand, name, len(arguments))
                return I.IRCall(name, kind, line, text, receiver, arguments, False)

        if function_node.type == "func_literal":
            return I.IRCall("<closure>", I.INTERNAL_CALL, line, text, None, arguments, False)

        if function_node.type in _CONVERSION_FUNCTION_TYPES or function_node.type == "parenthesized_expression":
            return I.IRCall(self.raw_flat(function_node)[:60], I.BUILTIN_CALL, line, text,
                            None, arguments, False)
        return I.IRCall(self.raw_flat(function_node)[:60], I.EXTERNAL_CALL, line, text,
                        None, arguments, False)

    def _receiver_kind(self, operand, name: str, argc: int = 0) -> tuple[str | None, str]:
        """(receiver text, call kind) for `<operand>.name(..)`."""
        if operand is None:
            return None, I.EXTERNAL_CALL
        if operand.type == "identifier":
            identifier = self.text(operand)
            if identifier == self.receiver:
                return None, I.INTERNAL_CALL
            if identifier in self.imports and identifier not in self.params \
                    and identifier not in self.locals:
                import_path = self.imports[identifier]
                label = self.catalog.label_for_import(import_path)
                if label:
                    if (label, name) in self.catalog.types:
                        return label, I.BUILTIN_CALL      # `pkg.Type(x)` conversion
                    return label, I.INTERNAL_CALL
                segment = import_path.rsplit("/", 1)[-1]
                if identifier in PURE_PACKAGES or segment in PURE_PACKAGES:
                    return identifier, I.BUILTIN_CALL
                if segment == "http" and name in HTTP_RESPONSE_FUNCS:
                    return identifier, I.BUILTIN_CALL     # writes the response
                return identifier, I.EXTERNAL_CALL
            if name in LOCK_METHODS:
                return identifier, I.BUILTIN_CALL
            declared = self.params.get(identifier, "")
            if declared in REQUEST_TYPES or declared in CONTEXT_TYPES \
                    or declared in RESPONSE_TYPES:
                # Reading the request or the context, writing the response:
                # the handler's input and output, not state or control.
                return identifier, I.BUILTIN_CALL
            if name in STRINGER_METHODS and argc <= 1:
                return identifier, I.BUILTIN_CALL
            if declared:
                return typed_receiver(identifier, declared), I.EXTERNAL_CALL
            return identifier, I.EXTERNAL_CALL

        root = self._root_identifier(operand)
        receiver = self.flat(operand)
        if root == self.receiver and receiver.startswith("self."):
            receiver = receiver[5:]
        if name in LOCK_METHODS:
            return receiver, I.BUILTIN_CALL
        if root is None:
            return receiver, I.BUILTIN_CALL if operand.type in {
                "composite_literal", "interpreted_string_literal", "raw_string_literal",
            } else I.EXTERNAL_CALL
        if root in GO_BUILTIN_FUNCS and operand.type == "call_expression":
            return receiver, I.BUILTIN_CALL               # `new(Wei).Add(a, b)`
        if root in self.imports and root not in self.params and root != self.receiver \
                and root not in self.locals:
            import_path = self.imports[root]
            segment = import_path.rsplit("/", 1)[-1]
            if root in PURE_PACKAGES or segment in PURE_PACKAGES:
                return receiver, I.BUILTIN_CALL
            return receiver, I.EXTERNAL_CALL
        declared = self.params.get(root, "")
        if declared in REQUEST_TYPES or declared in CONTEXT_TYPES or declared in RESPONSE_TYPES:
            return receiver, I.BUILTIN_CALL
        if name in STRINGER_METHODS and argc <= 1:
            return receiver, I.BUILTIN_CALL
        return receiver, I.EXTERNAL_CALL

    def _nested_call(self, node) -> I.IRCall | None:
        for candidate in self._walk(node):
            if candidate.type == "call_expression":
                return self.classify_call(candidate)
        return None

    def _closure_blocks(self, node) -> tuple[I.IRStmt, ...]:
        """Bodies of closures inside `node`, lowered so their calls stay visible."""
        if node is None:
            return ()
        blocks: list[I.IRStmt] = []
        stack = list(node.named_children) if node.type != "func_literal" else [node]
        while stack:
            current = stack.pop()
            if current.type == "func_literal":
                body = current.child_by_field_name("body")
                blocks.append(I.IRStmt(I.BLOCK, self.line(current), "closure",
                                       body=self.block(body), note="closure"))
                continue
            stack.extend(reversed(current.named_children))
        return tuple(blocks)

    # -- statements -----------------------------------------------------------
    def block(self, node) -> tuple[I.IRStmt, ...]:
        if node is None:
            return ()
        out: list[I.IRStmt] = []
        items = node.named_children
        if node.type == "block":
            items = [child for child in node.named_children]
            if len(items) == 1 and items[0].type == "statement_list":
                items = items[0].named_children
        for child in items:
            out.extend(self.statement(child))
        return tuple(out)

    def statement(self, node) -> list[I.IRStmt]:
        if node is None or node.type in _SKIP_STATEMENTS:
            return []
        if node.type == "statement_list":
            out: list[I.IRStmt] = []
            for child in node.named_children:
                out.extend(self.statement(child))
            return out
        handler = getattr(self, f"_st_{node.type}", None)
        if handler is not None:
            return handler(node)
        return [I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node), reads=self.state_in(node))]

    def _st_expression_statement(self, node):
        inner = node.named_children[0] if node.named_child_count else None
        if inner is None:
            return []
        if inner.type == "call_expression":
            return self._call_statement(inner, note=None)
        return [I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node), reads=self.state_in(node),
                         note="receive" if inner.type == "unary_expression" else None,
                         body=self._closure_blocks(inner))]

    def _call_statement(self, call_node, note: str | None) -> list[I.IRStmt]:
        call = self.classify_call(call_node)
        line = self.line(call_node)
        text = self.flat(call_node)
        reads = self.state_in(call_node)
        body = self._closure_blocks(call_node)
        if call.receiver is None and call.callee == "panic":
            return [I.IRStmt(I.REVERT, line, text, reads=reads, note="panic")]
        if (call.receiver, call.callee) in TERMINATING_SELECTORS and \
                call.receiver in self.imports:
            return [I.IRStmt(I.REVERT, line, text, reads=reads,
                             note=f"{call.receiver}.{call.callee}")]
        if call.callee in LOCK_METHODS and call.kind == I.BUILTIN_CALL:
            lock_note = LOCK_NOTES.get(call.callee, "")
            return [I.IRStmt(I.CALL, line, text, call=call,
                             note=f"{note} {lock_note}".strip() if note else lock_note)]
        statements = [I.IRStmt(I.CALL, line, text, call=call, reads=reads, body=body, note=note)]
        statements.extend(self._decoded_locals(call, line))
        return statements

    def _decoded_locals(self, call: I.IRCall, line: int) -> list[I.IRStmt]:
        """`Decode(&x)` fed from the request: `x` is chosen by the sender."""
        if call.callee not in DECODE_CALLS or not self.request_params:
            return []
        sources = [a.text for a in call.arguments if a.base in self.request_params]
        if not sources and call.receiver not in self.request_params and \
                (call.receiver or "").split(".")[0] not in self.request_params and \
                call.callee not in {"Decode", "Unmarshal"}:
            return []
        source = sources[0] if sources else sorted(self.request_params)[0]
        out: list[I.IRStmt] = []
        for argument in call.arguments:
            if argument.text.startswith("&"):
                local = argument.text[1:].strip()
                if re.fullmatch(r"[A-Za-z_]\w*", local):
                    out.append(I.IRStmt(
                        I.ASSIGN, line, f"{local} = {source}",
                        target=I.IRExpr("identifier", local, local, identifiers=(local,), line=line),
                        operator="=",
                        value=I.IRExpr("identifier", source, source.split(".")[0],
                                       identifiers=(source.split(".")[0],), line=line),
                        note=f"decoded-from-request:{call.callee}",
                    ))
        return out

    def _request_derived(self, targets, value_node, line: int) -> I.IRStmt | None:
        """`id := mux.Vars(r)["id"]`: the local is chosen by whoever sent the request."""
        if not self.request_params or value_node is None:
            return None
        if not any(c.type in {"call_expression", "selector_expression", "index_expression"}
                   for c in self._walk(value_node)):
            return None
        mentioned = [name for name in self.identifiers(value_node) if name in self.request_params]
        if not mentioned or len(targets) != 1 or targets[0].type != "identifier":
            return None
        local = self.text(targets[0])
        if local == "_":
            return None
        return I.IRStmt(
            I.ASSIGN, line, f"{local} = {mentioned[0]}",
            target=I.IRExpr("identifier", local, local, identifiers=(local,), line=line),
            operator="=",
            value=I.IRExpr("identifier", mentioned[0], mentioned[0],
                           identifiers=(mentioned[0],), line=line),
            note=f"request-derived: {self.flat(value_node)[:120]}",
        )

    def _declaration(self, node, left, right, operator: str, declared: bool) -> list[I.IRStmt]:
        targets = list(left.named_children) if left is not None and left.type == "expression_list" \
            else ([left] if left is not None else [])
        value = right
        if right is not None and right.type == "expression_list" and right.named_child_count == 1:
            value = right.named_children[0]
        writes = tuple(dict.fromkeys(b for b in (self.write_base(t) for t in targets) if b))
        reads = set(self.state_in(value))
        if operator != "=" or any(t.type != "identifier" for t in targets):
            reads |= set(self.state_in(left))
        call = self._nested_call(value)
        statement = I.IRStmt(
            I.VAR_DECL if declared else I.ASSIGN, self.line(node), self.flat(node),
            target=self.expr(left), operator=operator, value=self.expr(value) if value is not None else None,
            call=call, writes=writes, reads=tuple(sorted(reads)),
            body=self._closure_blocks(value),
        )
        out = [statement]
        if call is not None:
            out.extend(self._decoded_locals(call, self.line(node)))
        # A decoder's own result is its error, not the request; the decoded
        # local is the derived value and was recorded just above.
        if call is None or call.callee not in DECODE_CALLS:
            derived = self._request_derived(targets, value, self.line(node))
            if derived is not None:
                out.append(derived)
        return out

    def _st_short_var_declaration(self, node):
        return self._declaration(node, node.child_by_field_name("left"),
                                 node.child_by_field_name("right"), "=", True)

    def _st_assignment_statement(self, node):
        operator = self.text(node.child_by_field_name("operator")) or "="
        return self._declaration(node, node.child_by_field_name("left"),
                                 node.child_by_field_name("right"), operator, False)

    def _st_var_declaration(self, node):
        out: list[I.IRStmt] = []
        for spec in self._walk_specs(node, "var_spec"):
            names = spec.children_by_field_name("name")
            value = spec.child_by_field_name("value")
            if value is not None and value.type == "expression_list" and value.named_child_count == 1:
                value = value.named_children[0]
            target_text = ", ".join(self.text(n) for n in names)
            call = self._nested_call(value)
            out.append(I.IRStmt(
                I.VAR_DECL, self.line(spec), self.flat(spec),
                target=I.IRExpr("identifier", target_text, self.text(names[0]) if names else None,
                                identifiers=tuple(self.text(n) for n in names), line=self.line(spec)),
                operator="=" if value is not None else None,
                value=self.expr(value) if value is not None else None,
                call=call, reads=self.state_in(value), body=self._closure_blocks(value),
            ))
            if call is not None:
                out.extend(self._decoded_locals(call, self.line(spec)))
            if call is None or call.callee not in DECODE_CALLS:
                derived = self._request_derived(list(names), value, self.line(spec))
                if derived is not None:
                    out.append(derived)
        return out

    def _st_const_declaration(self, node):
        out: list[I.IRStmt] = []
        for spec in self._walk_specs(node, "const_spec"):
            names = spec.children_by_field_name("name")
            value = spec.child_by_field_name("value")
            out.append(I.IRStmt(
                I.VAR_DECL, self.line(spec), self.flat(spec),
                target=I.IRExpr("identifier", ", ".join(self.text(n) for n in names),
                                self.text(names[0]) if names else None, line=self.line(spec)),
                operator="=" if value is not None else None,
                value=self.expr(value) if value is not None else None,
            ))
        return out

    def _walk_specs(self, node, wanted: str):
        for child in node.named_children:
            if child.type == wanted:
                yield child
            elif child.type in {"var_spec_list", "const_spec_list"}:
                for spec in child.named_children:
                    if spec.type == wanted:
                        yield spec

    def _update(self, node, operator: str):
        target = node.named_children[0] if node.named_child_count else None
        base = self.write_base(target)
        return [I.IRStmt(
            I.ASSIGN, self.line(node), self.flat(node), target=self.expr(target),
            operator=operator, value=I.IRExpr("literal", "1", None, line=self.line(node)),
            writes=(base,) if base else (), reads=self.state_in(target),
        )]

    def _st_inc_statement(self, node):
        return self._update(node, "+=")

    def _st_dec_statement(self, node):
        return self._update(node, "-=")

    def _st_if_statement(self, node):
        out: list[I.IRStmt] = []
        initializer = node.child_by_field_name("initializer")
        if initializer is not None:
            out.extend(self.statement(initializer))
        condition = node.child_by_field_name("condition")
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")
        orelse: tuple[I.IRStmt, ...] = ()
        if alternative is not None:
            orelse = tuple(self.statement(alternative)) if alternative.type == "if_statement" \
                else self.block(alternative)
        out.append(I.IRStmt(
            I.IF, self.line(node), "if " + self.flat(condition),
            condition=self.expr(condition), body=self.block(consequence), orelse=orelse,
            reads=self.state_in(condition),
        ))
        return out

    def _st_for_statement(self, node):
        header = None
        note = "for"
        for child in node.named_children:
            if child.type in {"for_clause", "range_clause"}:
                header = child
                note = "range" if child.type == "range_clause" else "for"
                break
            if child.type != "block":
                header = child
                break
        body = node.child_by_field_name("body")
        header_text = self.flat(header) if header is not None else ""
        bound = re.search(r"<\s*(\d+)", header_text)
        return [I.IRStmt(
            I.LOOP, self.line(node), ("for " + header_text).strip(),
            condition=self.expr(header) if header is not None else None,
            body=self.block(body), reads=self.state_in(header), note=note,
            loop_bound=int(bound.group(1)) if bound else None,
        )]

    def _st_expression_switch_statement(self, node):
        return self._switch(node, "switch")

    def _st_type_switch_statement(self, node):
        return self._switch(node, "type-switch")

    def _st_select_statement(self, node):
        return self._switch(node, "select")

    def _switch(self, node, note: str) -> list[I.IRStmt]:
        out: list[I.IRStmt] = []
        initializer = node.child_by_field_name("initializer")
        if initializer is not None:
            out.extend(self.statement(initializer))
        subject = node.child_by_field_name("value")
        subject_text = self.flat(subject) if subject is not None else ""
        cases: list[tuple[str, object, list]] = []
        for case in node.named_children:
            if case.type in {"expression_case", "type_case", "communication_case"}:
                heads = case.children_by_field_name("value") or case.children_by_field_name("type")
                communication = case.child_by_field_name("communication")
                if communication is not None:
                    heads = [communication]
                head_ids = {h.id for h in heads}
                values = [v for h in heads
                          for v in (h.named_children if h.type == "expression_list" else [h])]
                if case.type == "expression_case" and subject_text:
                    condition = " || ".join(f"{subject_text} == {self.flat(v)}" for v in values)
                elif case.type == "type_case":
                    condition = " || ".join(f"{subject_text} is {self.flat(v)}" for v in values)
                else:
                    condition = ", ".join(self.flat(v) for v in values)
                body_nodes = [c for c in case.named_children if c.id not in head_ids]
                cases.append((condition, case, body_nodes))
            elif case.type == "default_case":
                cases.append(("default", case, list(case.named_children)))
        if note == "select":
            # A `select` arm runs when a message arrives; that is not a
            # condition the code decides, so an arm is a block, never a guard
            # a sibling could be said to lack.
            for condition, case, body_nodes in cases:
                body = self._case_body(case, body_nodes)
                label = "select default" if condition == "default" else f"select case {condition}"
                out.append(I.IRStmt(
                    I.BLOCK, self.line(case), label[:400], body=tuple(body),
                    reads=self.state_in(case), note="select-case",
                ))
            return out
        chain: tuple[I.IRStmt, ...] = ()
        for condition, case, body_nodes in reversed(cases):
            body = self._case_body(case, body_nodes)
            if condition == "default":
                chain = tuple(body)
                continue
            identifiers = tuple(dict.fromkeys(
                self.identifiers(subject) + self.identifiers(case)))
            chain = (I.IRStmt(
                I.IF, self.line(case), f"case {condition}"[:400],
                condition=I.IRExpr("case", condition[:400], None, identifiers=identifiers,
                                   line=self.line(case)),
                body=tuple(body), orelse=chain,
                reads=tuple(sorted(set(self.state_in(subject)) | set(self.state_in(case)))),
                note=note,
            ),)
        out.extend(chain)
        return out

    def _case_body(self, case, body_nodes) -> list[I.IRStmt]:
        body: list[I.IRStmt] = []
        communication = case.child_by_field_name("communication")
        if communication is not None and communication.type in {
                "short_var_declaration", "assignment_statement"}:
            body.extend(self.statement(communication))
        elif communication is not None and communication.type == "receive_statement":
            # `case event := <-channel:` binds what was received.
            left = communication.child_by_field_name("left")
            right = communication.child_by_field_name("right")
            if left is not None:
                body.extend(self._declaration(communication, left, right, "=", True))
        for child in body_nodes:
            body.extend(self.statement(child))
        return body

    def _st_return_statement(self, node):
        value = node.named_children[0] if node.named_child_count else None
        if value is not None and value.type == "expression_list" and value.named_child_count == 1:
            value = value.named_children[0]
        return [I.IRStmt(
            I.RETURN, self.line(node), self.flat(node),
            value=self.expr(value) if value is not None else None,
            call=self._nested_call(value), reads=self.state_in(node),
            body=self._closure_blocks(value),
        )]

    def _st_go_statement(self, node):
        return self._deferred(node, "go")

    def _st_defer_statement(self, node):
        return self._deferred(node, "defer")

    def _deferred(self, node, note: str):
        call_node = next((c for c in node.named_children if c.type == "call_expression"), None)
        if call_node is None:
            return [I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node), note=note)]
        statements = self._call_statement(call_node, note=note)
        return statements

    def _st_labeled_statement(self, node):
        inner = node.named_children[-1] if node.named_child_count else None
        return self.statement(inner) if inner is not None and inner.type != "label_name" else []

    def _st_block(self, node):
        return [I.IRStmt(I.BLOCK, self.line(node), "block", body=self.block(node))]

    def _st_send_statement(self, node):
        return [I.IRStmt(I.UNKNOWN, self.line(node), self.flat(node),
                         reads=self.state_in(node), note="send")]

    # -- declarations ---------------------------------------------------------
    def parameters(self, node) -> list[Parameter]:
        out: list[Parameter] = []
        if node is None:
            return out
        for declaration in node.named_children:
            if declaration.type not in {"parameter_declaration", "variadic_parameter_declaration"}:
                continue
            type_text = self.raw_flat(declaration.child_by_field_name("type"))
            if declaration.type == "variadic_parameter_declaration":
                type_text = "..." + type_text
            names = declaration.children_by_field_name("name")
            if not names:
                out.append(Parameter("", type_text))
            for name_node in names:
                out.append(Parameter(self.text(name_node), type_text))
        return out

    def _result_text(self, node) -> str:
        result = node.child_by_field_name("result")
        return self.raw_flat(result) if result is not None else ""

    def _returned_closure(self, body):
        """The `return func(w, r) {..}` of a handler factory, if that is its shape."""
        if body is None:
            return None
        statements = body.named_children
        if len(statements) == 1 and statements[0].type == "statement_list":
            statements = statements[0].named_children
        for statement in statements:
            if statement.type != "return_statement":
                continue
            for candidate in self._walk(statement):
                if candidate.type == "func_literal":
                    return statement, candidate
        return None

    def build_function(self, node, contract: Contract, receiver_name: str | None,
                       receiver_type: str | None) -> Function:
        name = self.text(node.child_by_field_name("name"))
        params = self.parameters(node.child_by_field_name("parameters"))
        result_text = self._result_text(node)
        shape = classify_shape(params, result_text)
        body = node.child_by_field_name("body")

        outer_statements: list = []
        closure = None
        request_facing = params
        if shape == "handler-factory":
            found = self._returned_closure(body)
            if found is not None:
                return_statement, closure = found
                closure_params = self.parameters(closure.child_by_field_name("parameters"))
                if classify_shape(closure_params, "") == "handler":
                    # The factory's own parameters are injected at wiring time
                    # (the use case, the session store); only the closure's
                    # are chosen by whoever sends the request.
                    params = params + closure_params
                    request_facing = closure_params
                    shape = "handler"
                    statements_container = body.named_children
                    if len(statements_container) == 1 and statements_container[0].type == "statement_list":
                        statements_container = statements_container[0].named_children
                    outer_statements = [s for s in statements_container if s.id != return_statement.id]
                else:
                    closure = None

        self.receiver = receiver_name
        self.fields = {v.name for v in contract.state_vars} if receiver_type else set()
        self.params = {p.name: " ".join(p.type_name.split()) for p in params if p.name}
        self.request_params = {p.name for p in params
                               if " ".join(p.type_name.split()) in REQUEST_TYPES}
        self.locals = self._local_names(body)
        self.unsupported = []

        statements: list[I.IRStmt] = []
        if closure is not None:
            for statement in outer_statements:
                statements.extend(self.statement(statement))
            statements.extend(self.block(closure.child_by_field_name("body")))
            body_text = self.text(closure.child_by_field_name("body"))
        else:
            statements.extend(self.block(body))
            body_text = self.text(body)

        writes: set[str] = set()
        reads: set[str] = set()
        calls: list[str] = []
        external = False
        non_builtin = False
        for statement in statements:
            for sub in statement.walk():
                writes |= set(sub.writes)
                reads |= set(sub.reads)
                if sub.call is not None:
                    if sub.call.kind in I.EXTERNAL_CALL_KINDS:
                        external = True
                    if sub.call.kind != I.BUILTIN_CALL:
                        non_builtin = True
                    if sub.call.callee:
                        calls.append(sub.call.callee)
        reads |= writes

        if shape == "handler":
            visibility, kind = "external", "handler"
        elif receiver_type is None and name in {"init", "main"}:
            visibility, kind = "internal", name
        else:
            # A `NewX` factory is an ordinary function: Go has no constructor
            # that runs once at deployment, and `NewErrorResponse(..)` runs on
            # every request. Calling it "constructor" would hide it from the
            # resolver and turn each call to it into an opaque transition.
            visibility = "public" if is_exported(name) else "internal"
            kind = "method" if receiver_type else "function"

        function = Function(
            contract.name, name, visibility,
            "view" if not writes and not non_builtin else "stateful",
            [], reads, writes,
            (["<low-level-call>"] if external else []) + sorted(set(calls))[:32],
            self.line(node), body_text,
        )
        function.params = params
        function.returns = [Parameter("", result_text)] if result_text else []
        function.kind = kind
        function.language = GO
        function.parser = PARSER_NAME
        function.path = self.path
        function.end_line = node.end_point[0] + 1
        function.is_test = self.is_test or contract.is_test
        function.user_inputs = user_inputs_for(request_facing, shape, visibility)
        function.ir = I.IRFunctionBody(
            statements=tuple(statements), has_assembly=False,
            unsupported=tuple(self.unsupported), source=body_text,
        )
        self.receiver = None
        self.fields = set()
        self.params = {}
        self.request_params = set()
        self.locals = set()
        self.catalog.built.append((self.label, contract, function))
        return function

    def _local_names(self, body) -> set[str]:
        """Names bound inside the function: a local `quote` is not package `quote`."""
        found: set[str] = set()
        for current in self._walk(body):
            if current.type in {"short_var_declaration", "range_clause"}:
                left = current.child_by_field_name("left")
                for child in (left.named_children if left is not None else []):
                    if child.type == "identifier":
                        found.add(self.text(child))
            elif current.type in {"var_spec", "const_spec", "parameter_declaration"}:
                for name_node in current.children_by_field_name("name"):
                    found.add(self.text(name_node))
        found.discard("_")
        return found

    def _package_variables(self, node, contract: Contract) -> None:
        constant = node.type == "const_declaration"
        for spec in self._walk_specs(node, "const_spec" if constant else "var_spec"):
            value = spec.child_by_field_name("value")
            initial = self.raw_flat(value) if value is not None else ""
            type_text = declared_type(self.raw_flat(spec.child_by_field_name("type")), initial)
            for name_node in spec.children_by_field_name("name"):
                name = self.text(name_node)
                if name == "_":
                    continue
                contract.state_vars.append(StateVar(
                    contract.name, name, type_text, "package", self.line(spec),
                    constant=constant, key_types=_map_keys(type_text),
                    value_type=_map_value(type_text), language=GO,
                    initial_value=initial[:200],
                ))

    # -- file traversal ---------------------------------------------------------
    def run(self) -> list[Contract]:
        out: list[Contract] = []
        root = self.tree.root_node
        package_label = self.label
        if self.is_test and not package_label.endswith("_test"):
            package_label = f"{package_label}_test"
        package, new = self.catalog.package_contract(package_label, self.path, self.is_test)
        if new:
            out.append(package)
        package.imports = sorted(set(package.imports) | set(self.imports.values()))

        def emit(key):
            contract, created = self.catalog.contract_for(key, self.path)
            if key not in self.catalog.emitted:
                self.catalog.emitted.add(key)
                out.append(contract)
            return contract

        # Package-level state first: methods and functions read it by name,
        # including the variables sibling files of the package declare.
        for node in root.named_children:
            if node.type in {"var_declaration", "const_declaration"}:
                self._package_variables(node, package)
        self.package_vars = set(self.catalog.package_vars.get(self.label, set()))
        self.package_vars |= {v.name for v in package.state_vars}

        for node in root.named_children:
            if node.type == "type_declaration":
                for spec in node.named_children:
                    if spec.type not in {"type_spec", "type_alias"}:
                        continue
                    name = self.text(spec.child_by_field_name("name"))
                    key = (self.label, name)
                    info = self.catalog.types.get(key)
                    if info is None:
                        continue
                    if info.kind == "interface":
                        emit(key)
                    elif info.kind == "struct" and self.catalog.method_set(key):
                        emit(key)
                    elif info.kind == "struct":
                        package.types.append(ContractType(
                            name, "struct", [f"{f[0]}: {f[1]}" for f in info.fields], info.line,
                        ))
                    else:
                        package.types.append(ContractType(name, "type", [info.underlying], info.line))
            elif node.type == "function_declaration":
                function = self.build_function(node, package, None, None)
                package.functions.append(function)
            elif node.type == "method_declaration":
                receiver_type = self.catalog._receiver_type(self.source, node)
                receiver_name = None
                receiver = node.child_by_field_name("receiver")
                for child in (receiver.named_children if receiver is not None else []):
                    if child.type == "parameter_declaration":
                        name_node = child.child_by_field_name("name")
                        receiver_name = self.text(name_node) if name_node is not None else None
                key = (self.label, receiver_type)
                if key not in self.catalog.types:
                    # A method on a type declared as an alias or in a form the
                    # catalog does not model: give it a home rather than drop it.
                    self.catalog.types[key] = _TypeInfo(receiver_type, "struct", self.label,
                                                        self.path, self.line(node), 0)
                    self.catalog.names[key] = receiver_type
                owner = emit(key)
                function = self.build_function(node, owner, receiver_name or None, receiver_type)
                owner.functions.append(function)

        if not (package.functions or package.state_vars or package.types) and new:
            out.remove(package)
            del self.catalog.packages[package_label]
        self.catalog.mark_parsed(self.path)
        return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def type_catalog(files) -> GoCatalog:
    entries = []
    for path in files:
        try:
            entries.append((str(path), Path(path).read_bytes()))
        except OSError:
            continue
    return GoCatalog(entries)


def parse_text(text: str, path: str, catalog: GoCatalog | None = None) -> list[Contract]:
    parser, _ = _load()
    if parser is None:
        return []
    source = text.encode("utf-8")
    path = str(path)
    if catalog is None or path not in catalog.sources:
        catalog = GoCatalog([(path, source)])
    tree = catalog.trees[path]
    return _FileParser(catalog.sources[path], path, catalog, tree).run()


def parse_file(path, catalog: GoCatalog | None = None) -> list[Contract]:
    path = str(path)
    if catalog is not None and path in catalog.sources:
        tree = catalog.trees[path]
        if tree is None:
            return []
        return _FileParser(catalog.sources[path], path, catalog, tree).run()
    return parse_text(Path(path).read_text(encoding="utf-8", errors="ignore"), path)


def parse_sources(paths) -> list[Contract]:
    files = [Path(p) for p in paths if Path(p).suffix.lower() == ".go"]
    catalog = type_catalog(files)
    out: list[Contract] = []
    for path in files:
        out.extend(parse_file(path, catalog))
    return out


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), PARSER_NAME, GO)
