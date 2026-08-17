"""Anomalies in symbolic state deltas.

An anomaly is an asymmetry between accounting variables that a protocol is
expected to keep coherent. The accounting pairs come from the protocol model
when one is available, so the check is no longer limited to the hardcoded
`totalAssets` / `totalSupply` names.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def _pairs(protocol_model, state_names):
    """Accounting pairs where *both* sides really exist in the target.

    Comparing `totalAssets` against a `totalSupply` the protocol never declares
    would report every asset movement as an asymmetry.
    """
    found: list[tuple[str, str]] = []
    for relation in getattr(protocol_model, "accounting_relations", []) or []:
        left = relation.left.rsplit(".", 1)[-1]
        right = relation.right.rsplit(".", 1)[-1]
        if left != right:
            found.append((left, right))
    found.extend(DEFAULT_PAIRS)
    if state_names is None:
        return list(dict.fromkeys(found))
    return [
        pair for pair in dict.fromkeys(found)
        if pair[0] in state_names and pair[1] in state_names
    ]


def detect_delta_anomalies(deltas, protocol_model=None, state_names=None):
    pairs = _pairs(protocol_model, state_names)
    out: list[DeltaAnomaly] = []

    for delta in deltas:
        for asset_name, share_name in pairs:
            asset = delta.delta.get(asset_name, "0")
            share = delta.delta.get(share_name, "0")
            if asset_name not in delta.delta and share_name not in delta.delta:
                continue

            if _nonzero(asset) and not _nonzero(share):
                out.append(DeltaAnomaly(
                    delta.sequence, asset_name, asset, "asset-only-mutation",
                    "asset state changes while share supply has no symbolic delta",
                    min(.96, delta.confidence + .08), share_name, share,
                ))
            elif _nonzero(share) and not _nonzero(asset):
                out.append(DeltaAnomaly(
                    delta.sequence, share_name, share, "share-only-mutation",
                    "share supply changes while asset state has no symbolic delta",
                    min(.94, delta.confidence + .06), asset_name, asset,
                ))
            elif _nonzero(asset) and _nonzero(share) and asset != share:
                out.append(DeltaAnomaly(
                    delta.sequence, asset_name, asset, "asset-share-asymmetry",
                    f"symbolic delta({asset_name})={asset} differs from "
                    f"delta({share_name})={share}",
                    min(.96, delta.confidence + .08), share_name, share,
                ))

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
