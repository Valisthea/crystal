"""From property forms and a fixture to harness source, one backend at a time.

The three backends share one harness model: a deployed system, a bounded actor
set, one handler per entry point (synthesised from the signature where the
engine can, supplied by a fixture recipe where it cannot), a holder set that
records every address the harness ever handed to the contracts, and the
property evaluated over reachable state. What differs is only the idiom:

* Foundry  — `setUp()`, handlers driven through `targetSelector`, one
             `invariant_P()` per property, and an `afterInvariant()` gate that
             fails the run when a property's subject entry points never
             succeeded, so a vacuous campaign cannot report PASS;
* Medusa   — the same harness deployed from the constructor, `property_P()`
             returning a bool;
* Halmos   — the same deployment, then one `check_P_...()` per (property,
             entry point) with symbolic arguments and a control check that
             must fail, so a pruned path cannot report PASS either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import __release__
from .fixture import Fixture, Recipe
from .ir import (
    BACKENDS,
    FOUNDRY,
    HALMOS,
    MEDUSA,
    CompiledProperty,
    Exclusion,
    FunctionRef,
    Independence,
    Relation,
    Replay,
    Unsupported,
)
from .model import Resolver, canonical_type

VM_ADDRESS = "address(uint160(uint256(keccak256(\"hevm cheat code\"))))"
PANIC_SELECTOR = "0x4e487b71"


@dataclass
class Handler:
    id: int
    contract: str
    handle: str
    function: FunctionRef
    kind: str  # "synth" | "recipe"
    params: tuple[str, ...]
    body: str
    feeds: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        return self.function.qualified

    @property
    def act_name(self) -> str:
        return f"act_{self.id}_{self.contract}_{self.function.name}"

    @property
    def build_name(self) -> str:
        return f"_build_{self.id}"

    def param_names(self) -> list[str]:
        return [p.split()[-1] for p in self.params]


@dataclass
class PropertyPlan:
    name: str
    campaign_id: str
    statement: str
    ir: object
    notes: list[str] = field(default_factory=list)
    subjects: tuple[int, ...] = ()  # handler ids whose success the vacuity gate demands
    reads: tuple[str, ...] = ()


@dataclass
class HarnessPlan:
    fixture: Fixture
    resolver: Resolver
    pragma: str
    pack: str
    handlers: list[Handler] = field(default_factory=list)
    ledgers: dict[str, str] = field(default_factory=dict)  # qualified mapping -> ledger name
    imports: dict[str, str] = field(default_factory=dict)  # symbol -> import path
    skipped: list[str] = field(default_factory=list)

    def handlers_of(self, contract: str) -> list[Handler]:
        return [h for h in self.handlers if h.contract == contract]

    def handler_for(self, function: FunctionRef) -> Handler | None:
        for handler in self.handlers:
            if handler.contract == function.contract and handler.function.name == function.name \
                    and handler.function.params == function.params:
                return handler
        return None


# ── planning ─────────────────────────────────────────────────────────────

def ledger_name(qualified: str) -> str:
    """`PegOutContract::_pegOutQuotes` -> `PegOutContract___pegOutQuotes`, the
    suffix of the generated `_crystal_pickKey_*` / `_keys_*` members."""
    return re.sub(r"\W", "_", qualified)


def _clean(type_name: str) -> str:
    text = re.sub(r"\s+", " ", type_name or "").strip()
    return re.sub(r"\b(memory|calldata|storage)\b", "", text).strip()


def _owner_import(resolver: Resolver, contract, owner: str, fixture: Fixture) -> str | None:
    """Where the harness imports `owner` (a library/interface/contract) from,
    preferring the file the scoped contract itself imports it from."""
    if owner == contract.name:
        return None
    for imported in getattr(contract, "imports", ()) or ():
        text = str(imported).replace("\\", "/")
        if Path(text).name == f"{owner}.sol":
            if text.startswith("."):
                base = Path(contract.path).parent / text
                try:
                    return fixture.import_path(str(base.resolve()))
                except OSError:
                    return fixture.import_path(str(base))
            return fixture.import_path(text)
    path = resolver.import_for(owner)
    return fixture.import_path(path) if path else None


class _Synth:
    """Synthesises a builder body from a signature, or says why it cannot."""

    def __init__(self, plan: HarnessPlan, contract, function, handle: str):
        self.plan = plan
        self.resolver = plan.resolver
        self.contract = contract
        self.function = function
        self.handle = handle
        self.lines: list[str] = []
        self.touched: list[str] = []
        self.params: list[str] = []
        self.args: list[str] = []
        self.uses_salt = False
        self.counter = 0

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def build(self) -> tuple[bool, str]:
        for index, parameter in enumerate(self.function.params):
            expr, ok, reason = self.value_for(_clean(parameter.type_name), parameter.name, index)
            if not ok:
                return False, reason
            self.args.append(expr)
        if self.uses_salt:
            self.params.insert(0, "uint256 salt")
        return True, ""

    def value_for(self, type_name: str, name: str, index: int, depth: int = 0) -> tuple[str, bool, str]:
        resolver = self.resolver
        if type_name in {"address", "address payable"}:
            param = self.fresh("p")
            self.params.append(f"uint256 {param}")
            var = self.fresh("a")
            self.lines.append(f"address {var} = _crystal_actor({param});")
            self.touched.append(var)
            return (f"payable({var})" if type_name == "address payable" else var), True, ""
        match = re.fullmatch(r"(u?)int(\d*)", type_name)
        if match:
            width = match.group(2) or "256"
            param = self.fresh("p")
            if match.group(1) == "u":
                self.params.append(f"uint256 {param}")
                return (param if width == "256" else f"uint{width}({param})"), True, ""
            self.params.append(f"int256 {param}")
            return (param if width == "256" else f"int{width}({param})"), True, ""
        if type_name == "bool":
            param = self.fresh("p")
            self.params.append(f"bool {param}")
            return param, True, ""
        if type_name == "bytes32":
            ledger = self.ledger_for(name)
            param = self.fresh("p")
            if ledger is not None:
                self.params.append(f"uint256 {param}")
                var = self.fresh("k")
                self.lines.append(
                    f"bytes32 {var} = _crystal_pickKey_{ledger}({param}); "
                    f"if ({var} == bytes32(0)) {{ c.skip = true; return c; }} c.key = {var};"
                )
                return var, True, ""
            self.params.append(f"bytes32 {param}")
            return param, True, ""
        if re.fullmatch(r"bytes\d+", type_name):
            param = self.fresh("p")
            self.params.append(f"{type_name} {param}")
            return param, True, ""
        if type_name in {"bytes", "string"}:
            return ('""' if type_name == "string" else 'hex""'), True, ""
        if type_name.endswith("[]"):
            inner = canonical_type(type_name[:-2], resolver, self.contract)
            if inner is None:
                return "", False, f"cannot synthesise `{type_name} {name}`"
            return f"new {type_name[:-2]}[](0)", True, ""
        members = resolver.enum_members(type_name, self.contract)
        if members is not None:
            if not members:
                return "", False, f"enum {type_name} has no readable members"
            self.uses_salt = True
            self.register_owner(type_name)
            return f"{type_name}(uint8(_crystal_mix(salt, {index}) % {len(members)}))", True, ""
        fields = resolver.struct_fields(type_name, self.contract)
        if fields is not None:
            if depth > 2:
                return "", False, f"struct {type_name} nests too deeply to synthesise"
            self.uses_salt = True
            self.register_owner(type_name)
            var = self.fresh("s")
            self.lines.append(f"{type_name} memory {var};")
            for position, (ftype, fname) in enumerate(fields):
                expr, ok, reason = self.field_for(_clean(ftype), f"{name}.{fname}", index * 32 + position, depth + 1)
                if not ok:
                    return "", False, reason
                self.lines.append(f"{var}.{fname} = {expr};")
            return var, True, ""
        return "", False, f"cannot synthesise `{type_name} {name}` (needs a fixture recipe)"

    def field_for(self, type_name: str, name: str, mix: int, depth: int) -> tuple[str, bool, str]:
        """Struct fields come from one salt rather than one fuzz input each."""
        resolver = self.resolver
        if type_name in {"address", "address payable"}:
            var = self.fresh("a")
            self.lines.append(f"address {var} = _crystal_actor(_crystal_mix(salt, {mix}));")
            self.touched.append(var)
            return (f"payable({var})" if type_name == "address payable" else var), True, ""
        match = re.fullmatch(r"(u?)int(\d*)", type_name)
        if match:
            width = match.group(2) or "256"
            if match.group(1) == "u":
                return f"uint{width}(_crystal_mix(salt, {mix}))", True, ""
            return f"int{width}(int256(_crystal_mix(salt, {mix}) >> 1))", True, ""
        if type_name == "bool":
            return f"(_crystal_mix(salt, {mix}) & 1) == 1", True, ""
        if type_name == "bytes32":
            return f"bytes32(_crystal_mix(salt, {mix}))", True, ""
        if re.fullmatch(r"bytes\d+", type_name):
            return f"{type_name}(bytes32(_crystal_mix(salt, {mix})))", True, ""
        if type_name in {"bytes", "string"}:
            return ('""' if type_name == "string" else 'hex""'), True, ""
        if type_name.endswith("[]"):
            return f"new {type_name[:-2]}[](0)", True, ""
        members = resolver.enum_members(type_name, self.contract)
        if members:
            self.register_owner(type_name)
            return f"{type_name}(uint8(_crystal_mix(salt, {mix}) % {len(members)}))", True, ""
        fields = resolver.struct_fields(type_name, self.contract)
        if fields is not None and depth <= 2:
            self.register_owner(type_name)
            var = self.fresh("s")
            self.lines.append(f"{type_name} memory {var};")
            for position, (ftype, fname) in enumerate(fields):
                expr, ok, reason = self.field_for(_clean(ftype), f"{name}.{fname}", mix * 32 + position, depth + 1)
                if not ok:
                    return "", False, reason
                self.lines.append(f"{var}.{fname} = {expr};")
            return var, True, ""
        return "", False, f"cannot synthesise struct field `{type_name} {name}`"

    def register_owner(self, type_name: str) -> None:
        owner, _, _ = type_name.rpartition(".")
        if not owner or owner in self.plan.imports:
            return
        path = _owner_import(self.resolver, self.contract, owner, self.plan.fixture)
        if path:
            self.plan.imports[owner] = path

    def ledger_for(self, param_name: str) -> str | None:
        """A ledger for a mapping this function indexes with `param_name`."""
        body = self.function.body or ""
        for qualified, ledger in self.plan.ledgers.items():
            contract_name, _, variable = qualified.partition("::")
            if contract_name != self.contract.name:
                continue
            if re.search(rf"\b{re.escape(variable)}\s*\[\s*{re.escape(param_name)}\s*\]", body):
                return ledger
        return None


def _role_expression(resolver: Resolver, contract, handle: str, role: str) -> str | None:
    bare = role.split(".")[-1]
    for variable in contract.state_vars:
        if variable.name == bare and variable.visibility == "public":
            return f"{handle}.{bare}()"
    if bare == "DEFAULT_ADMIN_ROLE":
        return f"{handle}.DEFAULT_ADMIN_ROLE()"
    return None


def _is_overloaded(contract, function) -> bool:
    return sum(1 for f in contract.functions if f.name == function.name and f.is_entry_point) > 1


def _canonical_signature(resolver: Resolver, contract, function) -> str | None:
    parts = []
    for parameter in function.params:
        canonical = canonical_type(parameter.type_name, resolver, contract)
        if canonical is None:
            return None
        parts.append(canonical)
    return f"{function.name}({','.join(parts)})"


def build_plan(resolver: Resolver, fixture: Fixture, pack: str, contracts: list) -> HarnessPlan:
    """Handlers for every entry point of every deployed, scoped contract."""
    pragma = resolver.pragma(contracts[0]) if contracts else "^0.8.20"
    plan = HarnessPlan(fixture, resolver, pragma, pack)

    # Ledgers first: recipes declare which mappings they feed.
    for recipe in fixture.recipes.values():
        if recipe.feeds and recipe.feeds not in plan.ledgers:
            plan.ledgers[recipe.feeds] = ledger_name(recipe.feeds)

    for contract in contracts:
        instance = fixture.instance_for(contract.name)
        if instance is None:
            plan.skipped.append(f"{contract.name}: no instance in the fixture")
            continue
        plan.imports.setdefault(contract.name, fixture.import_path(instance.path))
        for function in resolver.entry_points(contract):
            ref = resolver.function_ref(contract, function)
            if ref.qualified in fixture.exclude:
                plan.skipped.append(f"{ref.qualified}: excluded by the fixture")
                continue
            recipe = fixture.recipes.get(ref.qualified)
            handler_id = len(plan.handlers)
            if recipe is not None:
                plan.handlers.append(Handler(
                    handler_id, contract.name, instance.handle, ref, "recipe",
                    tuple(recipe.params), recipe.body, recipe.feeds,
                    recipe.note or "fixture recipe",
                ))
                continue
            synth = _Synth(plan, contract, function, instance.handle)
            ok, reason = synth.build()
            if not ok:
                plan.skipped.append(f"{ref.qualified}: {reason}")
                continue
            lines = list(synth.lines)
            role_note = ""
            if ref.role:
                expression = _role_expression(resolver, contract, instance.handle, ref.role)
                if expression:
                    lines.insert(0, f"c.caller = _crystal_callerWithRole(address({instance.handle}), {expression}, seed);")
                    role_note = f"caller chosen among actors holding {ref.role}"
                else:
                    lines.insert(0, "c.caller = _crystal_actor(seed);")
                    role_note = f"onlyRole({ref.role}) is not a public constant; caller is any actor"
            else:
                lines.insert(0, "c.caller = _crystal_actor(seed);")
            params = list(synth.params)
            if ref.payable:
                params.append("uint256 v")
                lines.append(f"c.value = _crystal_bound(v, 0, {fixture.value_cap});")
            if _is_overloaded(contract, function):
                signature = _canonical_signature(resolver, contract, function)
                if signature is None:
                    plan.skipped.append(f"{ref.qualified}: overloaded and its canonical signature is undecidable")
                    continue
                encode = f'abi.encodeWithSignature("{signature}"' + "".join(f", {a}" for a in synth.args) + ")"
            else:
                encode = f"abi.encodeCall({instance.handle}.{function.name}, ({', '.join(synth.args)}))" \
                    if synth.args else f"abi.encodeCall({instance.handle}.{function.name}, ())"
            lines.append(f"c.data = {encode};")
            if synth.touched:
                lines.append(f"c.touched = new address[]({len(synth.touched)});")
                for position, var in enumerate(synth.touched):
                    lines.append(f"c.touched[{position}] = {var};")
            note = "synthesised from the signature" + (f"; {role_note}" if role_note else "")
            plan.handlers.append(Handler(
                handler_id, contract.name, instance.handle, ref, "synth",
                tuple(params), "\n".join(lines), "", note,
            ))
    return plan


# ── rendering ────────────────────────────────────────────────────────────

def _indent(text: str, level: int = 2) -> str:
    pad = "    " * level
    return "\n".join((pad + line) if line.strip() else "" for line in text.splitlines())


def _getter_call(handle: str, state, key: str | None) -> str:
    if key is None:
        return f"uint256({handle}.{state.getter}())"
    return f"uint256({handle}.{state.getter}({key}))"


def _side_function(name: str, terms, fixture: Fixture) -> str:
    lines = [f"    function {name}() internal view returns (uint256 total) {{"]
    for term in terms:
        if term.kind == "balance":
            handle = fixture.instance_for(term.contract).handle
            lines.append(f"        total += address({handle}).balance;")
        elif term.kind == "scalar":
            handle = fixture.instance_for(term.state.contract).handle
            lines.append(f"        total += {_getter_call(handle, term.state, None)};")
    sums = [t for t in terms if t.kind == "sum"]
    if sums:
        lines.append("        for (uint256 i = 0; i < _holders.length; i++) {")
        for term in sums:
            handle = fixture.instance_for(term.state.contract).handle
            lines.append(f"            total += {_getter_call(handle, term.state, '_holders[i]')};")
        lines.append("        }")
    lines.append("    }")
    return "\n".join(lines)


def _property_subjects(plan: HarnessPlan, prop: PropertyPlan) -> tuple[int, ...]:
    ir = prop.ir
    if isinstance(ir, Relation):
        contracts = set(ir.contracts())
        ids = []
        for handler in plan.handlers:
            contract = plan.resolver.by_name[handler.contract]
            function = next(
                (f for f in plan.resolver.entry_points(contract)
                 if f.name == handler.function.name
                 and tuple((_clean(p.type_name), p.name) for p in f.params) == handler.function.params),
                None,
            )
            if function is None:
                continue
            touches = False
            for state in ir.states():
                if handler.contract == state.contract and \
                        plan.resolver.writes_transitively(contract, function, state.name):
                    touches = True
                if handler.contract != state.contract and \
                        plan.resolver.reaches_writer_of(contract, function, state):
                    touches = True
            if touches or (handler.contract in contracts and handler.function.payable):
                ids.append(handler.id)
        return tuple(ids)
    if isinstance(ir, (Exclusion, Independence)):
        return tuple(h.id for f in ir.functions for h in [plan.handler_for(f)] if h is not None)
    if isinstance(ir, Replay):
        functions = list(ir.once) + list(ir.reach_writers)
        return tuple(h.id for f in functions for h in [plan.handler_for(f)] if h is not None)
    return ()


def render(plan: HarnessPlan, prop: PropertyPlan, backend: str) -> tuple[str, dict[str, str], list[str]]:
    """Harness source for one property on one backend, plus extra files and
    notes about what the harness relies on."""
    fixture = plan.fixture
    resolver = plan.resolver
    ir = prop.ir
    notes: list[str] = []
    contract_name = f"Crystal{backend.capitalize()}_{plan.pack}_{prop.name}"
    subjects = _property_subjects(plan, prop)

    imports = dict(plan.imports)
    for instance in fixture.instances:
        imports.setdefault(instance.contract, fixture.import_path(instance.path))
        if instance.proxy and instance.proxy_path:
            imports.setdefault(instance.proxy, fixture.import_path(instance.proxy_path))
    if isinstance(ir, Independence):
        for guard in ir.guards:
            if guard.error_owner and guard.error_owner not in imports:
                owner_contract = resolver.by_name.get(ir.functions[0].contract)
                path = _owner_import(resolver, owner_contract, guard.error_owner, fixture) if owner_contract else None
                if path:
                    imports[guard.error_owner] = path
    if isinstance(ir, Replay) and ir.reach is not None:
        owner = resolver.enum_owner(ir.reach.value_type, resolver.by_name.get(ir.reach.contract))
        if owner and owner not in imports:
            owner_contract = resolver.by_name.get(ir.reach.contract)
            path = _owner_import(resolver, owner_contract, owner, fixture) if owner_contract else None
            if path:
                imports[owner] = path

    out: list[str] = []
    out.append("// SPDX-License-Identifier: UNLICENSED")
    out.append(f"pragma solidity {plan.pragma};")
    out.append("")
    out.append(f"// Generated by {__release__} — property harness, backend `{backend}`.")
    out.append(f"// Property {prop.name} ({prop.campaign_id}):")
    out.append(f"//   {prop.statement}")
    out.append(f"// Compiled form: {ir.describe()}")
    out.append("// Reads:")
    for read in prop.reads:
        out.append(f"//   {read}")
    out.append("// Do not edit: regenerate from the pack.")
    out.append("")
    if backend == FOUNDRY:
        out.append('import {Test, console} from "forge-std/Test.sol";')
    for symbol, path in fixture.imports.items():
        imports[symbol] = fixture.import_path(path)
    for symbol, path in sorted(imports.items()):
        out.append(f'import {{{symbol}}} from "{path}";')
    out.append("")
    out.append("interface CrystalVm {")
    out.append("    function prank(address) external;")
    out.append("    function deal(address, uint256) external;")
    out.append("    function warp(uint256) external;")
    out.append("    function roll(uint256) external;")
    out.append("    function addr(uint256) external returns (address);")
    out.append("    function sign(uint256, bytes32) external returns (uint8, bytes32, bytes32);")
    out.append("    function etch(address, bytes calldata) external;")
    out.append("    function label(address, string calldata) external;")
    out.append("}")
    out.append("")
    out.append("interface CrystalRoles {")
    out.append("    function hasRole(bytes32, address) external view returns (bool);")
    out.append("}")
    out.append("")
    inherit = " is Test" if backend == FOUNDRY else ""
    out.append(f"contract {contract_name}{inherit} {{")
    out.append(f"    CrystalVm internal constant _vm = CrystalVm({VM_ADDRESS});")
    out.append("")
    out.append("    struct CrystalCall {")
    out.append("        address target; address caller; uint256 value; bytes data;")
    out.append("        bytes32 key; address[] touched; bool skip;")
    out.append("    }")
    out.append("")
    out.append("    // ── system under test (fixture) ──")
    for instance in fixture.instances:
        out.append(f"    {instance.contract} internal {instance.handle};")
    out.append("")
    out.append("    // ── actors (fixture) ──")
    for actor in fixture.actors:
        out.append(f"    address internal {actor.name};")
        out.append(f"    uint256 internal {actor.name}Key;")
    out.append("    address[] internal _actors;")
    out.append("")
    out.append("    // ── engine state ──")
    out.append("    address[] internal _holders;")
    out.append("    mapping(address => bool) internal _isHolder;")
    n = max(len(plan.handlers), 1)
    out.append(f"    uint256 internal constant N_HANDLERS = {len(plan.handlers)};")
    out.append(f"    uint256[{n}] internal _attempts;")
    out.append(f"    uint256[{n}] internal _successes;")
    out.append(f"    uint256[{n}] internal _reverts;")
    out.append(f"    uint256[{n}] internal _skips;")
    out.append("    bytes4[] internal _revertSelectors;")
    out.append("    mapping(bytes4 => uint256) internal _revertCount;")
    out.append("    bytes4 internal _lastRevertSelector;")
    for qualified, ledger in plan.ledgers.items():
        out.append(f"    bytes32[] internal _keys_{ledger}; // {qualified}")
        out.append(f"    mapping(bytes32 => bool) internal _known_{ledger};")
    out.append("")
    if fixture.members.strip():
        out.append("    // ── fixture members ──")
        out.append(_indent(fixture.members, 1))
        out.append("")

    # ── setup ──
    setup_name = "constructor()" if backend == MEDUSA else "function setUp() public"
    out.append(f"    {setup_name} {{")
    out.append(f"        _vm.warp({fixture.base_timestamp});")
    out.append(f"        _vm.roll({fixture.base_block});")
    prelude = fixture.prelude.get(backend, "")
    if prelude.strip():
        out.append(_indent(prelude, 2))
    # Actors first: initialiser arguments name them.
    for actor in fixture.actors:
        out.append(f"        {actor.name}Key = {actor.key};")
        out.append(f"        {actor.name} = _vm.addr({actor.name}Key);")
        out.append(f"        _vm.deal({actor.name}, {actor.funding});")
        out.append(f"        _actors.push({actor.name});")
    for instance in fixture.instances:
        ctor = ", ".join(instance.ctor_args)
        if instance.proxy:
            init = (f"abi.encodeCall({instance.contract}.{instance.init}, ({', '.join(instance.init_args)}))"
                    if instance.init else '""')
            args = instance.proxy_args.replace("$IMPL", f"impl_{instance.handle}").replace("$INIT", init)
            out.append("        {")
            out.append(f"            {instance.contract} impl_{instance.handle} = new {instance.contract}({ctor});")
            out.append(f"            {instance.proxy} proxy_{instance.handle} = new {instance.proxy}({args});")
            out.append(f"            {instance.handle} = {instance.contract}(payable(address(proxy_{instance.handle})));")
            out.append("        }")
        else:
            out.append(f"        {instance.handle} = new {instance.contract}({ctor});")
            if instance.init:
                out.append(f"        {instance.handle}.{instance.init}({', '.join(instance.init_args)});")
    for call in fixture.setup:
        if call.caller:
            out.append(f"        _vm.prank({call.caller});")
        out.append(_indent(call.code, 2))
    out.append("        _crystal_seedHolders();")
    if backend == FOUNDRY:
        out.append("        targetContract(address(this));")
        out.append(f"        bytes4[] memory selectors = new bytes4[]({len(plan.handlers) + 1});")
        for handler in plan.handlers:
            out.append(f"        selectors[{handler.id}] = this.{handler.act_name}.selector;")
        out.append(f"        selectors[{len(plan.handlers)}] = this.act_time.selector;")
        out.append("        targetSelector(FuzzSelector({addr: address(this), selectors: selectors}));")
    out.append("    }")
    out.append("")

    # ── engine core ──
    out.append(_engine_core(plan, fixture))
    out.append("")

    # ── builders and handlers ──
    for handler in plan.handlers:
        params = ", ".join(["uint256 seed"] + list(handler.params))
        out.append(f"    // {handler.label}: {handler.note}")
        # A synthesised builder only reads (actors, ledgers, roles); a recipe may sign or arm a mock.
        mutability = "view " if handler.kind == "synth" else ""
        out.append(f"    function {handler.build_name}({params}) internal {mutability}returns (CrystalCall memory c) {{")
        out.append(f"        c.target = address({handler.handle});")
        out.append(_indent(handler.body, 2))
        out.append("    }")
        if backend != HALMOS:
            out.append(f"    function {handler.act_name}({params}) public {{")
            out.append(f"        CrystalCall memory c = {handler.build_name}({', '.join(['seed'] + handler.param_names())});")
            out.append(f"        _crystal_call({handler.id}, c);")
            out.append("    }")
        out.append("")
    if backend != HALMOS:
        out.append("    function act_time(uint256 dt, uint256 db) public {")
        out.append("        _vm.warp(block.timestamp + _crystal_bound(dt, 1, 30 days));")
        out.append("        _vm.roll(block.number + _crystal_bound(db, 1, 5000));")
        out.append("    }")
        out.append("")

    # ── property ──
    hooks_success: list[str] = []
    hooks_revert: list[str] = []
    body, success, revert, extra_notes = _render_property(plan, prop, backend, subjects)
    hooks_success.extend(success)
    hooks_revert.extend(revert)
    notes.extend(extra_notes)
    out.append("    // ── hooks ──")
    out.append("    function _crystal_onSuccess(uint256 id, CrystalCall memory c) internal {")
    out.append("        id; c;")
    for ledger_name, ledger in plan.ledgers.items():
        feeders = [h.id for h in plan.handlers if h.feeds == ledger_name]
        if feeders:
            condition = " || ".join(f"id == {f}" for f in feeders)
            out.append(f"        if (({condition}) && c.key != bytes32(0) && !_known_{ledger}[c.key]) {{")
            out.append(f"            _known_{ledger}[c.key] = true; _keys_{ledger}.push(c.key);")
            out.append("        }")
    for line in hooks_success:
        out.append(_indent(line, 2))
    out.append("    }")
    out.append("    function _crystal_onRevert(uint256 id, CrystalCall memory c, bytes4 sel, bytes memory ret) internal {")
    out.append("        id; c; sel; ret;")
    for line in hooks_revert:
        out.append(_indent(line, 2))
    out.append("    }")
    out.append("")
    out.append(body)

    # ── vacuity instrument (Medusa): the fuzzer reports the maximum each
    # counter reached, so a subject entry point that never succeeded shows 0.
    if backend == MEDUSA:
        out.append("")
        for handler_id in subjects:
            handler = plan.handlers[handler_id]
            out.append(f"    /// @notice max successes of {handler.label}; 0 at the end of a campaign means {prop.name} was never exercised")
            out.append(f"    function optimize_{prop.name}_{handler.function.name}_{handler.id}_successes() public view returns (int256) {{")
            out.append(f"        return int256(_successes[{handler_id}]);")
            out.append("    }")

    # ── vacuity gate (Foundry) ──
    if backend == FOUNDRY:
        out.append("")
        out.append("    /// @notice Runs after every invariant run. Reports, per run, whether each subject")
        out.append("    /// entry point succeeded at least once: a property nobody exercised has not been")
        out.append("    /// tested, and a campaign in which no run prints CRYSTAL_NONVACUOUS is VACUOUS, not PASS.")
        out.append("    function afterInvariant() public view {")
        out.append("        uint256 total;")
        out.append("        for (uint256 i = 0; i < N_HANDLERS; i++) total += _attempts[i];")
        out.append(f'        console.log("CRYSTAL_RUN {prop.name} attempts", total);')
        for handler in plan.handlers:
            out.append(
                f'        console.log("CRYSTAL_COVERAGE {handler.label} attempts/successes/reverts", '
                f'_attempts[{handler.id}], _successes[{handler.id}], _reverts[{handler.id}]);'
            )
        out.append("        for (uint256 i = 0; i < _revertSelectors.length; i++) {")
        out.append('            console.log(string.concat("CRYSTAL_REVERT ", vm.toString(abi.encodePacked(_revertSelectors[i])), " count"), _revertCount[_revertSelectors[i]]);')
        out.append("        }")
        out.append("        bool vacuous = false;")
        for handler_id in subjects:
            handler = plan.handlers[handler_id]
            out.append(f"        if (_successes[{handler_id}] == 0) {{ vacuous = true; console.log(\"CRYSTAL_VACUOUS {prop.name}: {handler.label} never succeeded in this run\"); }}")
        out.append(f'        if (!vacuous) console.log("CRYSTAL_NONVACUOUS {prop.name}: every subject entry point succeeded in this run");')
        out.append("    }")
    out.append("}")
    source = "\n".join(out) + "\n"

    extra: dict[str, str] = {}
    for name, content in fixture.support_files.items():
        extra[name] = content
    if backend == MEDUSA:
        extra["medusa.json"] = _medusa_config(contract_name, fixture)
    if backend == HALMOS:
        extra["halmos.toml"] = "[global]\nloop = 16\nsolver-timeout-assertion = 0\n"
    if subjects:
        notes.append(
            "vacuity gate: " + ", ".join(plan.handlers[i].label for i in subjects)
            + " must each succeed at least once"
        )
    if plan.skipped:
        notes.append("not driven: " + "; ".join(plan.skipped))
    return source, extra, notes


def _engine_core(plan: HarnessPlan, fixture: Fixture) -> str:
    lines = [
        "    // ── engine core ──",
        "    function _crystal_track(address a) internal {",
        "        if (a != address(0) && !_isHolder[a]) { _isHolder[a] = true; _holders.push(a); }",
        "    }",
        "    function _crystal_seedHolders() internal {",
        "        for (uint256 i = 0; i < _actors.length; i++) _crystal_track(_actors[i]);",
    ]
    for instance in fixture.instances:
        lines.append(f"        _crystal_track(address({instance.handle}));")
    lines += [
        "        _crystal_track(address(this));",
        "    }",
        "    function _crystal_actor(uint256 seed) internal view returns (address) {",
        "        return _actors[seed % _actors.length];",
        "    }",
        "    function _crystal_mix(uint256 salt, uint256 index) internal pure returns (uint256) {",
        "        return uint256(keccak256(abi.encode(salt, index)));",
        "    }",
        "    function _crystal_bound(uint256 x, uint256 lo, uint256 hi) internal pure returns (uint256) {",
        "        if (hi <= lo) return lo;",
        "        return lo + (x % (hi - lo + 1));",
        "    }",
        "    function _crystal_callerWithRole(address target, bytes32 role, uint256 seed) internal view returns (address) {",
        "        uint256 n = _actors.length;",
        "        for (uint256 i = 0; i < n; i++) {",
        "            address a = _actors[(seed + i) % n];",
        "            if (CrystalRoles(target).hasRole(role, a)) return a;",
        "        }",
        "        return address(0);",
        "    }",
        "    function _crystal_selector(bytes memory ret) internal pure returns (bytes4 sel) {",
        "        if (ret.length < 4) return bytes4(0);",
        "        assembly { sel := mload(add(ret, 32)) }",
        "    }",
        "    function _crystal_isPanic11(bytes memory ret) internal pure returns (bool) {",
        f"        if (ret.length < 36 || _crystal_selector(ret) != bytes4({PANIC_SELECTOR})) return false;",
        "        uint256 code;",
        "        assembly { code := mload(add(ret, 36)) }",
        "        return code == 0x11;",
        "    }",
    ]
    for qualified, ledger in plan.ledgers.items():
        lines += [
            f"    function _crystal_pickKey_{ledger}(uint256 seed) internal view returns (bytes32) {{",
            f"        if (_keys_{ledger}.length == 0) return bytes32(0);",
            f"        return _keys_{ledger}[seed % _keys_{ledger}.length];",
            "    }",
        ]
    lines += [
        "    function _crystal_call(uint256 id, CrystalCall memory c) internal returns (bool ok, bytes memory ret) {",
        "        _attempts[id] += 1;",
        "        if (c.skip || c.caller == address(0)) { _skips[id] += 1; return (false, \"\"); }",
        "        _crystal_track(c.caller);",
        "        for (uint256 i = 0; i < c.touched.length; i++) _crystal_track(c.touched[i]);",
        "        if (c.value > 0) _vm.deal(c.caller, c.caller.balance + c.value);",
        "        _vm.prank(c.caller);",
        "        (ok, ret) = c.target.call{value: c.value}(c.data);",
        "        if (ok) {",
        "            _successes[id] += 1;",
        "            _crystal_onSuccess(id, c);",
        "        } else {",
        "            _reverts[id] += 1;",
        "            bytes4 sel = _crystal_selector(ret);",
        "            if (_revertCount[sel] == 0) _revertSelectors.push(sel);",
        "            _revertCount[sel] += 1;",
        "            _lastRevertSelector = sel;",
        "            _crystal_onRevert(id, c, sel, ret);",
        "        }",
        "    }",
    ]
    return "\n".join(lines)


def _medusa_config(contract_name: str, fixture: Fixture) -> str:
    import json

    return json.dumps({
        "fuzzing": {
            "workers": 4,
            "workerResetLimit": 50,
            "timeout": 0,
            "testLimit": 50000,
            "shrinkLimit": 500,
            "callSequenceLength": 60,
            "corpusDirectory": f"{fixture.harness_dir}/corpus",
            "coverageEnabled": True,
            "revertReporterEnabled": True,
            "targetContracts": [contract_name],
            "deployerAddress": "0x30000",
            "senderAddresses": ["0x10000", "0x20000", "0x30000"],
            "blockNumberDelayMax": 2000,
            "blockTimestampDelayMax": 20000,
            "transactionGasLimit": 100000000,
            "testing": {
                "stopOnFailedTest": False,
                "stopOnNoTests": True,
                "testAllContracts": False,
                "assertionTesting": {"enabled": False},
                "propertyTesting": {"enabled": True, "testPrefixes": ["property_"]},
                "optimizationTesting": {"enabled": True, "testPrefixes": ["optimize_"]},
            },
            "chainConfig": {
                "codeSizeCheckDisabled": True,
                "cheatCodes": {"cheatCodesEnabled": True, "enableFFI": False},
                "skipAccountChecks": True,
            },
        },
        "compilation": {
            "platform": "crytic-compile",
            "platformConfig": {
                "target": ".",
                "solcVersion": "",
                "exportDirectory": "",
                "args": ["--foundry-compile-all"],
            },
        },
        "slither": {"useSlither": False, "cachePath": "slither_results.json", "args": []},
        "logging": {"level": "info", "logDirectory": "", "noColor": True},
    }, indent=2)


# ── property bodies ──────────────────────────────────────────────────────

def _render_property(plan: HarnessPlan, prop: PropertyPlan, backend: str, subjects) -> tuple[str, list[str], list[str], list[str]]:
    ir = prop.ir
    if isinstance(ir, Relation):
        return _render_relation(plan, prop, backend, subjects)
    if isinstance(ir, Exclusion):
        return _render_exclusion(plan, prop, backend)
    if isinstance(ir, Replay):
        return _render_replay(plan, prop, backend)
    if isinstance(ir, Independence):
        return _render_independence(plan, prop, backend)
    raise TypeError(f"cannot render {type(ir).__name__}")


def _assert(backend: str, condition: str, message: str) -> str:
    if backend == FOUNDRY:
        return f"assertTrue({condition}, \"{message}\");"
    return f"assert({condition});"


def _property_function(backend: str, name: str, body_lines: list[str], condition: str, message: str) -> str:
    if backend == MEDUSA:
        lines = [f"    function property_{name}() public view returns (bool) {{"]
        lines += [f"        {line}" for line in body_lines]
        lines.append(f"        return {condition};")
        lines.append("    }")
        return "\n".join(lines)
    if backend == FOUNDRY:
        lines = [f"    function invariant_{name}() public view {{"]
        lines += [f"        {line}" for line in body_lines]
        lines.append(f"        {_assert(backend, condition, message)}")
        lines.append("    }")
        return "\n".join(lines)
    return ""


def _call_args(handler: Handler, prefix: str) -> tuple[str, str]:
    """Declarations and argument list for driving `handler` from a check."""
    decls = [f"uint256 {prefix}seed"] + [
        f"{p.rsplit(' ', 1)[0]} {prefix}{p.rsplit(' ', 1)[1]}" for p in handler.params
    ]
    args = [f"{prefix}seed"] + [f"{prefix}{n}" for n in handler.param_names()]
    return ", ".join(decls), ", ".join(args)


def _render_relation(plan, prop, backend, subjects):
    ir = prop.ir
    fixture = plan.fixture
    out = [
        _side_function(f"_crystal_lhs_{prop.name}", ir.lhs, fixture),
        _side_function(f"_crystal_rhs_{prop.name}", ir.rhs, fixture),
    ]
    condition = f"_crystal_lhs_{prop.name}() {ir.op} _crystal_rhs_{prop.name}()"
    message = f"{prop.name} VIOLATED: {ir.describe()}"
    if backend in {FOUNDRY, MEDUSA}:
        out.append(_property_function(backend, prop.name, [], condition, message))
    else:
        for handler_id in subjects:
            handler = plan.handlers[handler_id]
            decls, args = _call_args(handler, "")
            out.append(f"    /// @notice one symbolic step through {handler.label} from the fixture state")
            out.append(f"    function check_{prop.name}_{handler.function.name}_{handler.id}({decls}) public {{")
            out.append(f"        assert({condition}); // the fixture state satisfies the relation")
            out.append(f"        CrystalCall memory c = {handler.build_name}({args});")
            out.append("        if (c.skip) return;")
            out.append(f"        _crystal_call({handler.id}, c);")
            out.append(f"        assert({condition});")
            out.append("    }")
    notes = [f"{prop.name}: evaluated as `{condition}` over the recorded holder set"]
    return "\n".join(out), [], [], notes


def _render_exclusion(plan, prop, backend):
    ir = prop.ir
    name = prop.name
    handlers = [plan.handler_for(f) for f in ir.functions]
    missing = [f.qualified for f, h in zip(ir.functions, handlers) if h is None]
    if missing:
        raise _NoHandler(f"{name}: no handler drives {', '.join(missing)}")
    ids = [h.id for h in handlers]
    out = [
        f"    mapping(bytes32 => uint256) internal _crystal_settled_{name};",
        f"    bool internal _crystal_violated_{name};",
        f"    bytes32 internal _crystal_witness_{name};",
    ]
    condition = " || ".join(f"id == {i}" for i in ids)
    success = [
        f"if (({condition}) && c.key != bytes32(0)) {{\n"
        f"    _crystal_settled_{name}[c.key] += 1;\n"
        f"    if (_crystal_settled_{name}[c.key] > 1) {{ _crystal_violated_{name} = true; _crystal_witness_{name} = c.key; }}\n"
        f"}}"
    ]
    message = f"{name} VIOLATED: {ir.describe()}"
    if backend in {FOUNDRY, MEDUSA}:
        out.append(_property_function(backend, name, [], f"!_crystal_violated_{name}", message))
    else:
        for first, second in ((handlers[0], handlers[1]), (handlers[1], handlers[0])):
            decls_a, args_a = _call_args(first, "a_")
            decls_b, args_b = _call_args(second, "b_")
            out.append(f"    function check_{name}_{first.function.name}_then_{second.function.name}({decls_a}, {decls_b}) public {{")
            out.append(f"        CrystalCall memory a = {first.build_name}({args_a});")
            out.append(f"        CrystalCall memory b = {second.build_name}({args_b});")
            out.append("        if (a.skip || b.skip || a.key != b.key) return;")
            out.append(f"        (bool ok1, ) = _crystal_call({first.id}, a);")
            out.append(f"        (bool ok2, ) = _crystal_call({second.id}, b);")
            out.append("        assert(!(ok1 && ok2));")
            out.append("    }")
        first = handlers[0]
        decls_a, args_a = _call_args(first, "a_")
        out.append(f"    /// @notice control: claims {first.label} can never succeed. MUST FAIL, or every path above was pruned.")
        out.append(f"    function check_{name}ctl_{first.function.name}_reachable({decls_a}) public {{")
        out.append(f"        CrystalCall memory a = {first.build_name}({args_a});")
        out.append("        if (a.skip) return;")
        out.append(f"        (bool ok, ) = _crystal_call({first.id}, a);")
        out.append("        assert(!ok);")
        out.append("    }")
    notes = [f"{name}: a key counts as settled each time one of the entry points succeeds for it"]
    return "\n".join(out), success, [], notes


def _render_replay(plan, prop, backend):
    ir = prop.ir
    name = prop.name
    out = [
        f"    bool internal _crystal_violated_{name};",
        f"    bytes32 internal _crystal_witness_{name};",
    ]
    success: list[str] = []
    checks: list[tuple[Handler, str]] = []
    for function in ir.once:
        handler = plan.handler_for(function)
        if handler is None:
            raise _NoHandler(f"{name}: no handler drives {function.qualified}")
        out.append(f"    mapping(bytes32 => uint256) internal _crystal_runs_{name}_{handler.id};")
        success.append(
            f"if (id == {handler.id} && c.key != bytes32(0)) {{\n"
            f"    _crystal_runs_{name}_{handler.id}[c.key] += 1;\n"
            f"    if (_crystal_runs_{name}_{handler.id}[c.key] > 1) {{ _crystal_violated_{name} = true; _crystal_witness_{name} = c.key; }}\n"
            f"}}"
        )
        checks.append((handler, "succeeds twice for one key"))
    if ir.reach is not None:
        handle = plan.fixture.instance_for(ir.reach.contract).handle
        owner = plan.resolver.enum_owner(ir.reach.value_type, plan.resolver.by_name.get(ir.reach.contract))
        value = f"{owner}.{ir.reach.value_type.split('.')[-1]}.{ir.reach_value}" if owner else ir.reach_value
        for function in ir.reach_writers:
            handler = plan.handler_for(function)
            if handler is None:
                raise _NoHandler(f"{name}: no handler drives {function.qualified}")
            out.append(f"    mapping(bytes32 => bool) internal _crystal_reached_{name}_{handler.id};")
            # A successful writer call on a key already at the value is a second reach.
            success.append(
                f"if (id == {handler.id} && c.key != bytes32(0)) {{\n"
                f"    if (_crystal_reached_{name}_{handler.id}[c.key]) {{ _crystal_violated_{name} = true; _crystal_witness_{name} = c.key; }}\n"
                f"    if ({handle}.{ir.reach.getter}(c.key) == {value}) _crystal_reached_{name}_{handler.id}[c.key] = true;\n"
                f"}}"
            )
            checks.append((handler, f"reaches {ir.reach_value} twice for one key"))
    message = f"{name} VIOLATED: {ir.describe()}"
    if backend in {FOUNDRY, MEDUSA}:
        out.append(_property_function(backend, name, [], f"!_crystal_violated_{name}", message))
    else:
        for handler, what in checks:
            decls, args = _call_args(handler, "")
            out.append(f"    /// @notice {handler.label} {what}: the same call twice must not succeed twice")
            out.append(f"    function check_{name}_{handler.function.name}_{handler.id}_twice({decls}) public {{")
            out.append(f"        CrystalCall memory a = {handler.build_name}({args});")
            out.append("        if (a.skip) return;")
            out.append(f"        (bool ok1, ) = _crystal_call({handler.id}, a);")
            out.append(f"        CrystalCall memory b = {handler.build_name}({args});")
            out.append("        if (b.skip || b.key != a.key) return;")
            out.append(f"        (bool ok2, ) = _crystal_call({handler.id}, b);")
            out.append("        assert(!(ok1 && ok2));")
            out.append("    }")
        handler = checks[0][0]
        decls, args = _call_args(handler, "")
        out.append(f"    /// @notice control: claims {handler.label} can never succeed. MUST FAIL.")
        out.append(f"    function check_{name}ctl_{handler.function.name}_reachable({decls}) public {{")
        out.append(f"        CrystalCall memory a = {handler.build_name}({args});")
        out.append("        if (a.skip) return;")
        out.append(f"        (bool ok, ) = _crystal_call({handler.id}, a);")
        out.append("        assert(!ok);")
        out.append("    }")
    notes = [f"{name}: a successful value-writer call on a key already at the value counts as a second reach"]
    return "\n".join(out), success, [], notes


def _render_independence(plan, prop, backend):
    ir = prop.ir
    name = prop.name
    handlers = []
    for function in ir.functions:
        handler = plan.handler_for(function)
        if handler is None:
            raise _NoHandler(f"{name}: no handler drives {function.qualified}")
        handlers.append(handler)
    selectors = [g.selector_expr() for g in ir.guards]
    tests = [f"sel == {s}" for s in selectors]
    guard_test = " || ".join(tests) if tests else "false"
    out = [
        f"    bool internal _crystal_violated_{name};",
        f"    bytes4 internal _crystal_selector_{name};",
        f"    bytes32 internal _crystal_witness_{name};",
        f"    uint256 internal _crystal_handler_{name};",
        f"    function _crystal_isGuard_{name}(bytes4 sel, bytes memory ret) internal pure returns (bool) {{",
        f"        sel; ret;",
        f"        if ({guard_test}) return true;",
    ]
    if ir.arithmetic_on_state:
        out.append("        if (_crystal_isPanic11(ret)) return true;")
    out.append("        return false;")
    out.append("    }")
    condition = " || ".join(f"id == {h.id}" for h in handlers)
    revert = [
        f"if (({condition}) && _crystal_isGuard_{name}(sel, ret)) {{\n"
        f"    _crystal_violated_{name} = true; _crystal_selector_{name} = sel; _crystal_witness_{name} = c.key; _crystal_handler_{name} = id;\n"
        f"}}"
    ]
    message = f"{name} VIOLATED: a settlement call reverted on {ir.state.qualified}"
    if backend in {FOUNDRY, MEDUSA}:
        witness = []
        if backend == FOUNDRY:
            witness = [
                f"if (_crystal_violated_{name}) {{",
                f'    console.log("CRYSTAL_WITNESS {name} handler id", _crystal_handler_{name});',
                f'    console.log(string.concat("CRYSTAL_WITNESS {name} revert selector ", vm.toString(abi.encodePacked(_crystal_selector_{name}))));',
                f'    console.log(string.concat("CRYSTAL_WITNESS {name} key ", vm.toString(_crystal_witness_{name})));',
                f'    console.log("CRYSTAL_WITNESS {name} {ir.state.qualified} of holders:");',
                "    for (uint256 i = 0; i < _holders.length; i++) {",
                f"        console.log(_holders[i], {plan.fixture.instance_for(ir.state.contract).handle}.{ir.state.getter}(_holders[i]));" if ir.state.is_mapping else "",
                "    }",
                "}",
            ]
        out.append(_property_function(backend, name, [w for w in witness if w], f"!_crystal_violated_{name}", message))
    else:
        # Perturb the watched state through its own writers, then settle.
        state_contract = plan.resolver.by_name[ir.state.contract]
        perturbers = [
            h for h in plan.handlers_of(ir.state.contract)
            if plan.resolver.writes_transitively(
                state_contract,
                next(f for f in plan.resolver.entry_points(state_contract) if f.name == h.function.name),
                ir.state.name,
            ) and h.kind == "synth"
        ]
        for handler in handlers:
            decls, args = _call_args(handler, "")
            for perturber in perturbers or [None]:
                suffix = f"_after_{perturber.function.name}_{perturber.id}" if perturber else ""
                pdecls, pargs = _call_args(perturber, "p_") if perturber else ("", "")
                joined = ", ".join(d for d in (decls, pdecls) if d)
                out.append(f"    /// @notice {handler.label} must not revert on {ir.state.qualified}, whatever a writer did to it")
                out.append(f"    function check_{name}_{handler.function.name}_{handler.id}{suffix}({joined}) public {{")
                if perturber:
                    out.append(f"        CrystalCall memory p = {perturber.build_name}({pargs});")
                    out.append(f"        if (!p.skip) _crystal_call({perturber.id}, p);")
                out.append(f"        CrystalCall memory c = {handler.build_name}({args});")
                out.append("        if (c.skip) return;")
                out.append(f"        (bool ok, bytes memory ret) = _crystal_call({handler.id}, c);")
                out.append(f"        assert(ok || !_crystal_isGuard_{name}(_crystal_selector(ret), ret));")
                out.append("    }")
            out.append(f"    /// @notice control: claims {handler.label} can never succeed. MUST FAIL.")
            out.append(f"    function check_{name}ctl_{handler.function.name}_{handler.id}_reachable({decls}) public {{")
            out.append(f"        CrystalCall memory c = {handler.build_name}({args});")
            out.append("        if (c.skip) return;")
            out.append(f"        (bool ok, ) = _crystal_call({handler.id}, c);")
            out.append("        assert(!ok);")
            out.append("    }")
    notes = [
        f"{name}: watched revert reasons: " + (", ".join(g.describe() for g in ir.guards) or "none (static discharge)")
    ]
    return "\n".join(out), [], revert, notes


class _NoHandler(Exception):
    pass


# ── driver ───────────────────────────────────────────────────────────────

def compile_plan(plan: HarnessPlan, prop: PropertyPlan, backends=BACKENDS) -> list[CompiledProperty]:
    out: list[CompiledProperty] = []
    ir = prop.ir
    if isinstance(ir, Unsupported):
        for backend in backends:
            out.append(CompiledProperty(
                backend, "", "", ir.reason, prop.name, prop.campaign_id, prop.statement,
                ir.form, prop.reads, (), {}, tuple(prop.notes),
            ))
        return out
    for backend in backends:
        try:
            source, extra, notes = render(plan, prop, backend)
        except _NoHandler as exc:
            out.append(CompiledProperty(
                backend, "", "", str(exc), prop.name, prop.campaign_id, prop.statement,
                ir.form, prop.reads, (), {}, tuple(prop.notes),
            ))
            continue
        suffix = ".t.sol" if backend in {FOUNDRY, HALMOS} else ".sol"
        filename = f"{plan.fixture.harness_dir}/Crystal{backend.capitalize()}_{plan.pack}_{prop.name}{suffix}"
        exercises = tuple(h.label for h in plan.handlers)
        out.append(CompiledProperty(
            backend, source, filename, None, prop.name, prop.campaign_id, prop.statement,
            ir.form, prop.reads, exercises, extra, tuple(prop.notes) + tuple(notes),
        ))
    return out
