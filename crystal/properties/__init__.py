"""Property compilation: campaign invariants into executable harness source.

A campaign pack carries `CampaignInvariant`s. Until now they were text attached
to a candidate. This package turns each one into a property harness for
Foundry, Medusa and Halmos — or into an `UNSUPPORTED` with the precise reason,
which is a correct result whenever the statement cannot be expressed exactly.

    from crystal.properties import compile_properties, Fixture

    compiled = compile_properties(campaigns, contracts, fixture)
    for item in compiled:
        item.backend, item.source, item.filename, item.unsupported_reason

The pipeline is: `statement.parse_invariant` binds the statement to a property
form against `model.Resolver` (Crystal's parsed target); `harness.build_plan`
lays out one handler per entry point over the fixture's deployment; and
`harness.compile_plan` renders the form for each backend. Every compiled
property records which invariant it came from and which contract-qualified
state it reads.
"""

from __future__ import annotations

import re
from pathlib import Path

from .fixture import Actor, Fixture, Instance, Recipe, SetupCall
from .harness import HarnessPlan, PropertyPlan, build_plan, compile_plan
from .ir import (
    BACKENDS,
    FOUNDRY,
    HALMOS,
    MEDUSA,
    CompiledProperty,
    Exclusion,
    Independence,
    Relation,
    Replay,
    Unsupported,
)
from .model import Resolver
from .statement import parse_invariant

__all__ = [
    "Actor",
    "BACKENDS",
    "CompiledProperty",
    "FOUNDRY",
    "Fixture",
    "HALMOS",
    "Instance",
    "MEDUSA",
    "Recipe",
    "Resolver",
    "SetupCall",
    "compile_properties",
    "parse_invariant",
    "property_name",
    "split_report",
    "write_compiled",
]


def property_name(campaign_id: str, index: int) -> str:
    """`flyover-p5-proven-payment-settlement` -> `P5`; otherwise `I<n>`."""
    match = re.search(r"(?:^|-)p(\d+)(?:-|$)", campaign_id or "", re.I)
    return f"P{match.group(1)}" if match else f"I{index}"


def _iter_invariants(invariants):
    """Accept campaign definitions, (campaign_id, invariant) pairs, or bare
    invariants; yield (campaign_id, invariant, pack)."""
    index = 0
    for item in invariants:
        if hasattr(item, "invariants") and hasattr(item, "campaign_id"):
            for invariant in item.invariants:
                index += 1
                yield item.campaign_id, invariant, getattr(item, "pack", "") or "pack"
        elif isinstance(item, tuple) and len(item) == 2:
            index += 1
            yield item[0], item[1], "pack"
        else:
            index += 1
            yield f"invariant-{index}", item, "pack"


def compile_properties(invariants, contracts, fixture: Fixture | None = None,
                       backends=BACKENDS, scope=None, pack: str | None = None) -> list[CompiledProperty]:
    """Compile every invariant for every backend.

    `invariants` may be `CampaignDefinition`s (their `invariants` and
    `campaign_id` are used), `(campaign_id, CampaignInvariant)` pairs, or bare
    `CampaignInvariant`s. `contracts` is Crystal's parsed model of the target.
    `scope` restricts which contracts count as the target (defaults to every
    concrete, non-test contract). Without a `fixture` nothing can be deployed,
    so every expressible invariant compiles to UNSUPPORTED naming that.
    """
    invariants = list(invariants)
    items = list(_iter_invariants(invariants))
    if scope is None:
        # The campaigns' own scope decides what the harness drives; a pack that
        # names `Collateral*` does not want `PauseRegistry` handlers.
        scopes = [item.scope for item in invariants if hasattr(item, "scope")]
        if scopes and any(s.allowed_contracts for s in scopes):
            scope = {
                c.name for c in contracts
                if any(s.accepts_contract(c.name) for s in scopes if s.allowed_contracts)
            }
        elif fixture is not None:
            scope = {instance.contract for instance in fixture.instances}
    resolver = Resolver(contracts, scope=scope)
    pack_name = pack or (items[0][2] if items else "pack")
    pack_name = re.sub(r"\W", "_", pack_name)

    plans: list[PropertyPlan] = []
    for index, (campaign_id, invariant, _) in enumerate(items, 1):
        parsed = parse_invariant(invariant, resolver)
        name = property_name(campaign_id, index)
        reads = _reads_of(parsed.form)
        plans.append(PropertyPlan(
            name, campaign_id, invariant.statement, parsed.form, list(parsed.notes), (), reads,
        ))

    compiled: list[CompiledProperty] = []
    if fixture is None:
        for plan in plans:
            reason = (plan.ir.reason if isinstance(plan.ir, Unsupported) else
                      "no deployment fixture: Crystal will not invent how the target is "
                      "deployed, who its actors are, or how to build calls it cannot "
                      "synthesise; supply a Fixture")
            for backend in backends:
                compiled.append(CompiledProperty(
                    backend, "", "", reason, plan.name, plan.campaign_id, plan.statement,
                    plan.ir.form, plan.reads, (), {}, tuple(plan.notes),
                ))
        return compiled

    scoped = [c for c in resolver.concrete if fixture.instance_for(c.name) is not None]
    harness = build_plan(resolver, fixture, pack_name, scoped)
    for plan in plans:
        compiled.extend(compile_plan(harness, plan, backends))
    return compiled


def _reads_of(form) -> tuple[str, ...]:
    if isinstance(form, Relation):
        return tuple(dict.fromkeys(
            [t.state.access() for t in form.lhs + form.rhs if t.state is not None]
            + [f"balance({t.contract})" for t in form.lhs + form.rhs if t.kind == "balance"]
        ))
    if isinstance(form, Replay) and form.reach is not None:
        return (form.reach.access(),)
    if isinstance(form, Independence):
        return (form.state.access(),)
    return ()


def write_compiled(compiled, root) -> list[Path]:
    """Write every supported harness (and its extra files) under `root`."""
    written: list[Path] = []
    root = Path(root)
    for item in compiled:
        if not item.supported:
            continue
        path = root / item.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.source, encoding="utf-8")
        written.append(path)
        for name, content in item.extra_files.items():
            extra = path.parent / (name if name.endswith(".sol") else f"{item.name}-{item.backend}-{name}")
            extra.write_text(content, encoding="utf-8")
            written.append(extra)
    return written


def split_report(compiled) -> str:
    """The compiled / refused split, one line per invariant and backend."""
    lines = []
    for item in compiled:
        if item.supported:
            lines.append(f"[COMPILED]    {item.name} {item.backend:<8} {item.form:<13} -> {item.filename}")
        else:
            lines.append(f"[UNSUPPORTED] {item.name} {item.backend:<8} {item.form:<13} :: {item.unsupported_reason}")
    return "\n".join(lines)
