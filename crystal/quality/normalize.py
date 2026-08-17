import hashlib
import re

def normalize_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.:/-]+", " ", value or "")
    return " ".join(value.lower().split())

def stable_id(*parts: str) -> str:
    material = "|".join(normalize_name(p) for p in parts)
    return hashlib.sha256(material.encode()).hexdigest()[:16]
