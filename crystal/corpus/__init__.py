"""Reference corpus of known behaviour shapes."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CORPUS_PATH = Path(__file__).with_name("known_patterns.json")


@lru_cache(maxsize=1)
def load() -> dict:
    try:
        return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "0", "delta_shapes": [], "weaknesses": []}


def delta_shapes() -> list[dict]:
    return load().get("delta_shapes", [])


def weaknesses() -> list[dict]:
    return load().get("weaknesses", [])


def weakness_by_detector(detector: str) -> dict | None:
    for entry in weaknesses():
        if entry.get("detector") == detector:
            return entry
    return None
