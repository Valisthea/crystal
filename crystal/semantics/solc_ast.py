import json
import shutil
from pathlib import Path

from .. import process
from .ast import CompilerModel, ASTContract, ASTFunction, ASTStateVariable, SourceLocation


def _source_location(node, filename, text):
    raw = node.get("src")
    if not raw:
        return SourceLocation(filename)
    try:
        start, _, _ = raw.split(":", 2)
        offset = int(start)
        before = text[:offset]
        line = before.count("\n") + 1
        last_nl = before.rfind("\n")
        column = offset + 1 if last_nl < 0 else offset - last_nl
        return SourceLocation(filename, line, column)
    except (ValueError, TypeError):
        return SourceLocation(filename)


def _function_name(node):
    kind = node.get("kind")
    if node.get("name"):
        return node["name"]
    return {
        "constructor": "constructor",
        "fallback": "fallback",
        "receive": "receive",
    }.get(kind, "<anonymous>")


def parse_solc_ast(project):
    if not shutil.which("solc"):
        return CompilerModel(available=False)

    root = Path(project).resolve()
    sources = {}
    texts = {}
    for p in root.rglob("*.sol"):
        if any(x in p.parts for x in {".git", "node_modules", "lib", "out", "cache", "artifacts"}):
            continue
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="ignore")
        texts[rel] = text
        sources[rel] = {"content": text}

    if not sources:
        return CompilerModel(available=True)

    payload = {
        "language": "Solidity",
        "sources": sources,
        "settings": {"outputSelection": {"*": {"": ["ast"]}}},
    }

    proc = process.run(
        ["solc", "--standard-json"], stdin=json.dumps(payload), timeout=180
    )
    if not proc.ok:
        return CompilerModel(
            available=True,
            raw_errors=[{"severity": "error", "message": proc.error}],
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return CompilerModel(
            available=True,
            raw_errors=[{"severity": "error", "message": (proc.stderr or proc.stdout)[:4000]}],
        )

    model = CompilerModel(
        available=True,
        raw_errors=[x for x in data.get("errors", []) if x.get("severity") == "error"],
    )

    for filename, result in data.get("sources", {}).items():
        ast = result.get("ast") or {}
        text = texts.get(filename, "")
        for node in ast.get("nodes", []):
            if node.get("nodeType") != "ContractDefinition":
                continue
            contract = ASTContract(
                name=node.get("name", "<anonymous>"),
                node_id=node.get("id"),
                bases=[
                    (b.get("baseName") or {}).get("name", "")
                    for b in node.get("baseContracts", [])
                    if (b.get("baseName") or {}).get("name")
                ],
            )
            for child in node.get("nodes", []):
                kind = child.get("nodeType")
                if kind == "VariableDeclaration" and child.get("stateVariable"):
                    contract.state_variables.append(ASTStateVariable(
                        contract=contract.name,
                        name=child.get("name", ""),
                        node_id=child.get("id"),
                        type_name=(child.get("typeDescriptions") or {}).get("typeString"),
                        visibility=child.get("visibility"),
                        location=_source_location(child, filename, text),
                    ))
                elif kind == "FunctionDefinition":
                    modifiers = []
                    for m in child.get("modifiers", []):
                        name = (m.get("modifierName") or {}).get("name")
                        if name:
                            modifiers.append(name)
                    contract.functions.append(ASTFunction(
                        contract=contract.name,
                        name=_function_name(child),
                        node_id=child.get("id"),
                        visibility=child.get("visibility"),
                        mutability=child.get("stateMutability"),
                        modifiers=modifiers,
                        location=_source_location(child, filename, text),
                    ))
            model.contracts.append(contract)
    return model
