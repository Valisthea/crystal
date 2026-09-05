"""Anomalies in symbolic state deltas.

An anomaly is an asymmetry between accounting variables that a protocol is
expected to keep coherent. The accounting pairs come from the protocol model
when one is available, so the check is no longer limited to the hardcoded
`totalAssets` / `totalSupply` names.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..naming import bare_name, contract_of, qualify

DEFAULT_PAIRS = (("totalAssets", "totalSupply"),)

UNRESOLVED_MARKERS = ("MUTATE:", "OPAQUE:", "PHI(", "DIV_BY_ZERO")


@dataclass(frozen=True)
class DeltaAnomaly:
    sequence: list[str]
    state: str
    delta: str
    kind: str
    reason: str
    confidence: float
    counterpart: str = ""
    counterpart_delta: str = ""


def _nonzero(expression: str) -> bool:
    return (expression or "0").strip() != "0"


def _by_contract(state_names) -> dict[str, set[str]]:
    """Group declared state names by the contract that owns them.

    Accepts the qualified set the pipeline passes; a set of bare names groups
    under one anonymous namespace, which degrades to the old project-wide
    behaviour rather than failing.
    """
    if isinstance(state_names, dict):
        return {key: set(value) for key, value in state_names.items()}
    grouped: dict[str, set[str]] = {}
    for name in state_names or ():
        grouped.setdefault(contract_of(name), set()).add(bare_name(name))
    return grouped


def _pairs(protocol_model, state_names):
    """Accounting pairs where *both* sides really exist in one contract.

    Comparing `totalAssets` against a `totalSupply` the protocol never declares
    would report every asset movement as an asymmetry. So would comparing one
    contract's `totalAssets` against a different contract's `totalSupply`:
    two protocols sharing a vocabulary are not an accounting relation.
    """
    found: list[tuple[str, str]] = []
    for relation in getattr(protocol_model, "accounting_relations", []) or []:
        left = bare_name(relation.left.rsplit(".", 1)[-1])
        right = bare_name(relation.right.rsplit(".", 1)[-1])
        if left != right:
            found.append((left, right))
    found.extend(DEFAULT_PAIRS)
    pairs = list(dict.fromkeys(found))
    if state_names is None:
        return pairs
    declared = _by_contract(state_names)
    return [
        pair for pair in pairs
        if any(pair[0] in names and pair[1] in names
               for names in declared.values())
    ]


def _bind(delta_keys, asset_name: str, share_name: str):
    """Bind a bare accounting pair to the qualified keys of one delta.

    A sequence spanning two contracts carries both `Vault::totalAssets` and
    `Pool::totalAssets`; each is compared against the supply of its *own*
    contract. A side absent from a namespace stays qualified so the anomaly
    names the slot that did not move.
    """
    slots: dict[str, dict[str, str]] = {}
    for key in delta_keys:
        slots.setdefault(contract_of(key), {})[bare_name(key)] = key

    bound: list[tuple[str, str]] = []
    for slot in sorted(slots):
        names = slots[slot]
        if asset_name not in names and share_name not in names:
            continue
        bound.append((
            names.get(asset_name, qualify(slot, asset_name)),
            names.get(share_name, qualify(slot, share_name)),
        ))
    return bound


def _asymmetry(delta, asset_name: str, share_name: str) -> DeltaAnomaly | None:
    """Compare one bound accounting pair within a single delta."""
    asset = delta.delta.get(asset_name, "0")
    share = delta.delta.get(share_name, "0")

    if _nonzero(asset) and not _nonzero(share):
        return DeltaAnomaly(
            delta.sequence, asset_name, asset, "asset-only-mutation",
            "asset state changes while share supply has no symbolic delta",
            min(.96, delta.confidence + .08), share_name, share,
        )
    if _nonzero(share) and not _nonzero(asset):
        return DeltaAnomaly(
            delta.sequence, share_name, share, "share-only-mutation",
            "share supply changes while asset state has no symbolic delta",
            min(.94, delta.confidence + .06), asset_name, asset,
        )
    if _nonzero(asset) and _nonzero(share) and asset != share:
        return DeltaAnomaly(
            delta.sequence, asset_name, asset, "asset-share-asymmetry",
            f"symbolic delta({asset_name})={asset} differs from "
            f"delta({share_name})={share}",
            min(.96, delta.confidence + .08), share_name, share,
        )
    return None


def detect_delta_anomalies(deltas, protocol_model=None, state_names=None):
    pairs = _pairs(protocol_model, state_names)
    out: list[DeltaAnomaly] = []

    for delta in deltas:
        for bare_asset, bare_share in pairs:
            for bound in _bind(delta.delta, bare_asset, bare_share):
                anomaly = _asymmetry(delta, *bound)
                if anomaly is not None:
                    out.append(anomaly)

        for state, expression in sorted(delta.delta.items()):
            if any(marker in expression for marker in UNRESOLVED_MARKERS):
                out.append(DeltaAnomaly(
                    delta.sequence, state, expression, "unresolved-effect",
                    "the symbolic model could not resolve this effect exactly; "
                    "the sequence needs concrete or manual validation",
                    min(.70, delta.confidence),
                ))

    seen = set()
    unique = []
    for anomaly in out:
        key = (tuple(anomaly.sequence), anomaly.state, anomaly.kind, anomaly.delta)
        if key not in seen:
            seen.add(key)
            unique.append(anomaly)
    return sorted(unique, key=lambda x: (-x.confidence, x.sequence, x.kind))
