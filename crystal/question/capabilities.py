"""What Crystal can actually be asked to do, and by which subsystem.

A capability is a *kind of analysis*, never a tool name. A question asks for
`symbolic`; it does not ask for Halmos. That indirection is the point of §16:
a caller that names Medusa has hard-coded Crystal's current toolbox into its own
contract, and the day a provider is swapped the caller breaks for a reason that
has nothing to do with the question it was asking.

Every capability here has a real provider in this repository. None is aspirational
— a name with no producer behind it is a promise a caller would plan around and
Crystal could not keep.
"""

from __future__ import annotations

from dataclasses import dataclass

# In-process providers. Always available: they are Crystal's own modules, with
# no external executable and no optional dependency.
INTERNAL = "internal"
# External executables. Present or absent depending on the operator's machine,
# which is why `available()` has to be asked rather than assumed.
EXTERNAL = "external"


@dataclass(frozen=True)
class Capability:
    name: str
    summary: str
    providers: tuple[tuple[str, str], ...]   # (provider, INTERNAL | EXTERNAL)

    @property
    def always_available(self) -> bool:
        return any(kind == INTERNAL for _, kind in self.providers)


CAPABILITIES = {
    capability.name: capability
    for capability in (
        Capability(
            "static",
            "detector pass over the parsed IR",
            (("crystal.detectors", INTERNAL),),
        ),
        Capability(
            "dataflow",
            "value propagation, taint and influence over the semantic graph",
            (("crystal.semantics.dataflow", INTERNAL),),
        ),
        Capability(
            "symbolic",
            "state deltas as canonical polynomials, path constraints, and "
            "symbolic property checking",
            (("crystal.symbolic.engine", INTERNAL), ("halmos", EXTERNAL)),
        ),
        Capability(
            "differential",
            "behavioural difference between comparable functions",
            (("crystal.research.differential", INTERNAL),),
        ),
        Capability(
            "composition",
            "multi-step and cross-contract chain reasoning",
            (("crystal.composition", INTERNAL),),
        ),
        Capability(
            "fuzzing",
            "property fuzzing with a counterexample witness",
            (("medusa", EXTERNAL), ("echidna", EXTERNAL)),
        ),
        Capability(
            "runtime",
            "concrete EVM execution of a generated harness",
            (("foundry", EXTERNAL),),
        ),
    )
}

KNOWN = frozenset(CAPABILITIES)


def unknown(requested) -> tuple[str, ...]:
    """Requested names Crystal has no provider for.

    Returned rather than ignored. A capability accepted silently and then not
    exercised produces a result that answers a different question from the one
    that was asked, and nothing downstream can tell.
    """
    return tuple(sorted(name for name in requested or () if name not in KNOWN))


def availability(backends=None) -> dict[str, dict]:
    """Which capabilities can run here, and what is missing when they cannot.

    `backends` is the probe result for the external tools — a mapping of name to
    an object with `.available`. Absent, only the internal providers are
    reported as usable and every external one is reported as unprobed, which is
    a different claim from unavailable and is kept distinct on purpose.
    """
    probed = backends or {}
    out: dict[str, dict] = {}
    for name, capability in CAPABILITIES.items():
        usable, reasons = [], []
        for provider, kind in capability.providers:
            if kind == INTERNAL:
                usable.append(provider)
                continue
            entry = probed.get(provider)
            if entry is None:
                reasons.append(f"{provider}: not probed")
            elif getattr(entry, "available", False):
                usable.append(provider)
            else:
                reasons.append(
                    f"{provider}: {getattr(entry, 'reason', 'unavailable')}"
                )
        out[name] = {
            "available": bool(usable),
            "providers": usable,
            "unavailable": reasons,
            "summary": capability.summary,
        }
    return out


def export() -> dict:
    """The registry, for a parent system that needs to know what to ask for.

    Deliberately free of availability: what Crystal *can be asked* is a property
    of Crystal, and what is installed is a property of a machine. A parent that
    conflates them plans against one host.
    """
    return {
        "schema": "crystal-capabilities/1.0",
        "capabilities": {
            name: {
                "summary": capability.summary,
                "providers": [
                    {"provider": provider, "kind": kind}
                    for provider, kind in capability.providers
                ],
                "always_available": capability.always_available,
            }
            for name, capability in sorted(CAPABILITIES.items())
        },
    }
