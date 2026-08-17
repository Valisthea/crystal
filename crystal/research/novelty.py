"""Structural novelty scoring.

v1 scored novelty by comparing function names against a hardcoded list of
inverse pairs. v2 compares behaviour:

* the *shape* of the symbolic delta (what moves, in which direction, by what
  kind of quantity) against the reference corpus;
* the rarity of that shape within the project itself;
* whether the ordering of the sequence actually changes the outcome;
* whether the effect breaks the symmetry of a paired accounting variable.

Novelty stays a research-priority signal. It is never vulnerability confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..corpus import delta_shapes, weakness_by_detector
from ..symbolic.algebra import ARG_PREFIX, ENV_PREFIX, RETURN_PREFIX, STATE_PREFIX

STEP_SUFFIX_RE = re.compile(r"#\d+")
SYMBOL_RE = re.compile(
    rf"(?:{re.escape(ARG_PREFIX)}|{re.escape(ENV_PREFIX)}|"
    rf"{re.escape(RETURN_PREFIX)}|{re.escape(STATE_PREFIX)})[^\s+\-*/()]+"
)

ASSET_TOKENS = ("asset", "balance", "reserve", "pool", "collateral", "deposit")
SHARE_TOKENS = ("supply", "share", "debt", "stake")


@dataclass(frozen=True)
class NoveltyScore:
    known_pattern_similarity: float
    known_finding_similarity: float
    novel_state_interaction: float
    novel_sequence: float
    novel_invariant_violation: float
    cross_component_novelty: float

    @property
    def overall(self):
        # Novelty is a research-priority signal, never a finding-confidence score.
        return round(
            (1 - self.known_pattern_similarity) * .15 +
            (1 - self.known_finding_similarity) * .15 +
            self.novel_state_interaction * .20 +
            self.novel_sequence * .20 +
            self.novel_invariant_violation * .15 +
            self.cross_component_novelty * .15, 3
        )


@dataclass(frozen=True)
class NovelBehavior:
    sequence: list[str]
    changed_state: list[str]
    novelty: NoveltyScore
    reason: str
    shape: tuple[str, ...] = ()
    matched_pattern: str = ""
    order_sensitive: bool = False


def shape_of(expression: str) -> str:
    """Normalize a delta into a comparable shape.

    `ARG:msg.value#1 - ARG:x#2` and `ARG:a - ARG:b` both become `*-*`, so two
    protocols implementing the same behaviour compare equal.
    """
    normalized = STEP_SUFFIX_RE.sub("", expression or "0")
    normalized = SYMBOL_RE.sub("*", normalized)
    normalized = re.sub(r"\d+\*", "n*", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized or "0"


def _delta_shape(delta) -> tuple[str, ...]:
    return tuple(
        f"{name}:{shape_of(expression)}"
        for name, expression in sorted(delta.delta.items())
    )


def _role(name: str) -> str:
    lowered = name.lower()
    if any(token in lowered for token in SHARE_TOKENS):
        return "share"
    if any(token in lowered for token in ASSET_TOKENS):
        return "asset"
    return "other"


def _corpus_similarity(delta, relation: str) -> tuple[float, str]:
    best = (0.10, "")
    changed = [v for v in delta.delta.values() if v != "0"]
    touched = list(delta.delta.values())

    for pattern in delta_shapes():
        match = pattern.get("match", {})
        similarity = float(pattern.get("similarity", 0.0))
        if match.get("relation") and match["relation"] == relation:
            best = max(best, (similarity, pattern["id"]), key=lambda x: x[0])
        if match.get("all_zero_with_touch") and touched and not changed:
            best = max(best, (similarity, pattern["id"]), key=lambda x: x[0])
        if match.get("constant_increment") and changed and all(
            re.fullmatch(r"-?\d+", value) for value in changed
        ):
            best = max(best, (similarity, pattern["id"]), key=lambda x: x[0])
        if match.get("paired_shapes"):
            assets = {
                shape_of(v) for k, v in delta.delta.items()
                if _role(k) == "asset" and v != "0"
            }
            shares = {
                shape_of(v) for k, v in delta.delta.items()
                if _role(k) == "share" and v != "0"
            }
            if assets and assets == shares:
                best = max(best, (similarity, pattern["id"]), key=lambda x: x[0])
    return best


def _relation_for(sequence, differential_candidates) -> str:
    members = {name for name in sequence}
    for candidate in differential_candidates:
        pair = set(candidate.path_a) | set(candidate.path_b)
        if pair <= members:
            return getattr(candidate, "relation", "shared-state")
    return "shared-state"


def _invariant_asymmetry(delta) -> float:
    assets = {k: v for k, v in delta.delta.items() if _role(k) == "asset"}
    shares = {k: v for k, v in delta.delta.items() if _role(k) == "share"}
    if not assets or not shares:
        return .35
    asset_shapes = {shape_of(v) for v in assets.values() if v != "0"}
    share_shapes = {shape_of(v) for v in shares.values() if v != "0"}
    if asset_shapes and not share_shapes:
        return .92
    if share_shapes and not asset_shapes:
        return .84
    if asset_shapes != share_shapes:
        return .78
    return .30


def score_novelty(contracts, state_deltas, differential_candidates,
                  engine=None, detectors=None, limit=200):
    detector_by_function: dict[str, list[str]] = {}
    for item in detectors or []:
        detector_by_function.setdefault(
            f"{item.contract}.{item.function}", []
        ).append(item.detector)

    considered = list(state_deltas[:limit])
    shape_counts: dict[tuple[str, ...], int] = {}
    for delta in considered:
        shape = _delta_shape(delta)
        shape_counts[shape] = shape_counts.get(shape, 0) + 1
    total = max(len(considered), 1)

    out: list[NovelBehavior] = []
    for delta in considered:
        shape = _delta_shape(delta)
        relation = _relation_for(delta.sequence, differential_candidates)
        known_pattern, matched = _corpus_similarity(delta, relation)

        known_finding = 0.08
        for name in delta.sequence:
            for detector in detector_by_function.get(name, []):
                entry = weakness_by_detector(detector)
                if entry:
                    known_finding = max(known_finding, float(entry["similarity"]))
                    matched = matched or entry["id"]

        rarity = 1.0 - (shape_counts.get(shape, 1) - 1) / total
        changed = len(delta.changed)
        state_novel = min(.98, .30 + .45 * rarity + .05 * min(changed, 4))

        order_sensitive = False
        if engine is not None and len(delta.sequence) > 1:
            reversed_effect = engine.execute_sequence(list(reversed(delta.sequence)))
            if reversed_effect is not None:
                order_sensitive = dict(reversed_effect.deltas) != dict(delta.delta)

        sequence_novel = min(.98, .30 + (.28 if order_sensitive else .0)
                             + .06 * max(0, len(delta.sequence) - 2)
                             + (.14 if known_pattern < .40 else .0))
        invariant_novel = _invariant_asymmetry(delta)
        cross_component = len({name.rsplit(".", 1)[0] for name in delta.sequence}) > 1

        score = NoveltyScore(
            known_pattern, known_finding, state_novel, sequence_novel,
            invariant_novel, .92 if cross_component else .28,
        )
        reason = (
            f"delta shape {'/'.join(shape) or 'empty'} "
            f"({'matches ' + matched if matched else 'no corpus match'})"
            f"{'; ordering changes the outcome' if order_sensitive else ''}"
        )
        out.append(NovelBehavior(
            list(delta.sequence), list(delta.changed), score, reason,
            shape, matched, order_sensitive,
        ))

    return sorted(
        out, key=lambda x: (-x.novelty.overall, -len(x.changed_state), x.sequence)
    )
