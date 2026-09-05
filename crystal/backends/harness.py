"""Fuzzer harness pieces shared by Medusa and Echidna.

Measured on Medusa 1.5.1, 2026-09-05: a harness that only exposes
`property_*` getters compiles, registers its properties, then reports
`[PASSED]` for each of them after `calls: 0` — "cannot generate fuzzed call
as there are no methods to call". The fuzzer never calls into the contract the
harness deployed. Green, and nothing happened.

So the harness forwards every entry point it can express. Each handler ends
with a witness line that only executes when the forwarded call succeeded: a
revert report counts the handler's successes, a coverage report marks the
line. Monotonicity ghosts are high-water marks updated in every handler, so a
`property_x_monotonic` compares against the largest value ever observed rather
than against the zero it was declared with.

Fidelity caveat, stated once: the harness deploys the target, so every
forwarded call arrives from the deployer. Owner-only paths are reachable and
authority is not modelled; a VIOLATED verdict still needs the human gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .actions import entry_points, fuzzable_parameters, needs_value
from .base import _identifier, ghost_declarations

WITNESS_COUNTER = "_crystalWitness"
WITNESS_LINE = f"{WITNESS_COUNTER} += 1; // reached only if the forwarded call succeeded"


@dataclass(frozen=True)
class HandlerPlan:
    """Handler source lines, and which entry point each handler forwards."""

    lines: tuple[str, ...] = ()
    handlers: dict[str, object] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()

    def action(self, handler: str) -> str:
        function = self.handlers[handler]
        return f"{function.contract}.{function.name}"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.handlers)


def monotonic_subjects(properties) -> list[str]:
    return list(dict.fromkeys(
        prop.subject for prop in properties
        if getattr(prop, "kind", "") == "monotonicity" and getattr(prop, "subject", "")
    ))


def fuzz_handlers(contract, properties, contracts=()) -> HandlerPlan:
    """One forwarding handler per entry point whose arguments the fuzzer can
    supply; the others are listed with the reason, never approximated."""
    monotonic = monotonic_subjects(properties)
    lines: list[str] = []
    handlers: dict[str, object] = {}
    skipped: list[str] = []
    used: set[str] = set()
    for function in entry_points(contract):
        signature = (
            f"{contract.name}.{function.name}"
            f"({', '.join(parameter.type_name for parameter in function.params)})"
        )
        parameters = fuzzable_parameters(function)
        if parameters is None:
            skipped.append(
                f"{signature}: no handler — a struct, enum or nested-array argument "
                f"cannot be fuzzed from a generated harness"
            )
            continue
        name = base = f"h_{_identifier(function.name)}"
        index = 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        used.add(name)
        declaration = ", ".join(f"{kind} p_{label}" for kind, label in parameters)
        arguments = ", ".join(f"p_{label}" for _, label in parameters)
        payable = needs_value(contract, function, contracts)
        lines += [
            f"    // forwards {function.name} so the fuzzer can move the state the properties read",
            f"    function {name}({declaration}) public{' payable' if payable else ''} {{",
            f"        target.{function.name}{'{value: msg.value}' if payable else ''}({arguments});",
            f"        {WITNESS_LINE}",
        ]
        for subject in monotonic:
            ghost = f"_ghost_{_identifier(subject)}"
            lines.append(f"        {ghost} = _crystalMax({ghost}, target.{subject}());")
        lines += ["    }", ""]
        handlers[name] = function
    return HandlerPlan(tuple(lines), handlers, tuple(skipped))


def declarations(properties) -> list[str]:
    """Ghost high-water marks, the witness counter, and the helper they need."""
    lines = list(ghost_declarations(properties))
    lines.append(f"    uint256 internal {WITNESS_COUNTER};")
    if monotonic_subjects(properties):
        lines += [
            "",
            "    function _crystalMax(uint256 a, uint256 b) internal pure returns (uint256) {",
            "        return a > b ? a : b;",
            "    }",
        ]
    return lines
