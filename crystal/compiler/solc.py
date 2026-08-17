import json
import shutil
from pathlib import Path

from .. import process

def available():
    return shutil.which("solc") is not None

def compile_standard(project):
    if not available():
        return {"available": False, "contracts": {}}

    root = Path(project).resolve()
    sources = {}
    for p in root.rglob("*.sol"):
        if any(x in p.parts for x in {".git","node_modules","lib","out","cache"}):
            continue
        rel = p.relative_to(root).as_posix()
        sources[rel] = {"content": p.read_text(encoding="utf-8", errors="ignore")}

    payload = {
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "optimizer": {"enabled": False},
            "outputSelection": {
                "*": {"*": ["abi", "metadata", "storageLayout"],
                      "": ["ast"]}
            }
        }
    }

    proc = process.run(
        ["solc", "--standard-json"], stdin=json.dumps(payload), timeout=180
    )
    if not proc.ok:
        return {"available": True, "error": proc.error, "contracts": {}}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"available": True, "error": proc.stderr, "contracts": {}}

    return {
        "available": True,
        "errors": data.get("errors", []),
        "contracts": data.get("contracts", {}),
    }
