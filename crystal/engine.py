"""Crystal research pipeline: discover -> parse -> analyze -> graphs -> research."""

from __future__ import annotations

from .analysis import analyze
from .campaigns import discover_packs, run_campaigns
from .compiler.solc import compile_standard
from .composition import build_composition
from .detectors import run_detectors
from .discovery import discover, profile
from .graphs.callgraph import build_call_graph
from .graphs.cfg import build_cfg
from .graphs.program import build_graph
from .graphs.state import build_state_graph, candidate_sequences
from .graphs.storage import build_storage_graph
from .hypotheses import generate
from .invariants import discover_invariants
from .parsers import parse_project, parser_report
from .protocol.invariants import derive_protocol_invariants
from .protocol.model import build_protocol_model
from .quality.triage import build_candidates
from .quality.validation import validate
from .ranking import rank
from .research.engine import run_research
from .semantics.dataflow import build_dataflow
from .semantics.inheritance import build_inheritance_graph, link_inheritance
from .semantics.modifiers import build_modifier_graph, modifier_definitions
from .semantics.modules import build_module_graph
from .semantics.proxy import detect_proxy_signals
from .semantics.proxy import report as proxy_report
from .semantics.solc_ast import parse_solc_ast
from .sequences import generate_sequences
from .symbolic import SymbolicEngine


UNBOUNDED_DETECTOR = "unbounded-input-in-value-op"


def _unbounded_by_contract(signals) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for item in signals:
        if item.detector != UNBOUNDED_DETECTOR:
            continue
        found.setdefault(item.contract, []).append(
            f"{item.function}:{item.line} " + (item.evidence[0] if item.evidence else "")
        )
    return found


def research(project, use_solc=True, languages=None, run_detector_pass=True,
             use_foundry=True, detectors=None, include_tests=False, packs=()):
    sources = discover(project, languages=languages)
    parsed = parse_project(sources)
    all_contracts = parsed.contracts
    # Fixtures are parsed and reported, but kept out of research. A mock runtime
    # produces state deltas and unguarded writes by design; leaving it in means
    # the only deltas Crystal reports are the test builder's.
    test_contracts = [c for c in all_contracts if c.is_test]
    contracts = all_contracts if include_tests else [
        c for c in all_contracts if not c.is_test
    ]
    # Base-contract state must be attributed to derived contracts before any
    # analysis runs, otherwise every inherited variable looks untouched.
    inheritance = link_inheritance(contracts)

    observations = analyze(contracts)
    hypotheses = rank(generate(observations))

    symbolic_engine = SymbolicEngine(contracts)
    detector_signals = run_detectors(
        contracts, symbolic_engine, detectors, include_tests=include_tests,
        wirings=parsed.wirings, bindings=parsed.bindings,
        root=project, sources=sources,
    ) if run_detector_pass else []
    # Rebuild the composition with the unbounded-input findings so the reported
    # model carries the same confidence as the signal derived from it.
    composition = build_composition(
        contracts, parsed.wirings, parsed.bindings, project, sources,
        _unbounded_by_contract(detector_signals),
    )

    program_graph = build_graph(contracts)
    state_graph = build_state_graph(contracts)
    cfgs = [build_cfg(f) for c in contracts for f in c.functions]
    call_graph = build_call_graph(contracts)
    storage_graph = build_storage_graph(contracts)
    invariants = discover_invariants(contracts)
    sequences = generate_sequences(candidate_sequences(state_graph), invariants,
                                    state_graph=state_graph)

    compiler = compile_standard(project) if use_solc else {"available": False}
    compiler_model = parse_solc_ast(project) if use_solc else None

    protocol_model = build_protocol_model(contracts)
    protocol_invariants = derive_protocol_invariants(protocol_model)

    result = {
        "sources": sources,
        "contracts": contracts,
        "observations": observations,
        "hypotheses": hypotheses,
        "program_graph": program_graph,
        "state_graph": state_graph,
        "cfgs": cfgs,
        "call_graph": call_graph,
        "storage_graph": storage_graph,
        "invariant_candidates": invariants,
        "sequence_hypotheses": sequences,
        "compiler": compiler,
        "compiler_model": compiler_model,
        "inheritance_graph": build_inheritance_graph(contracts),
        "inheritance_resolution": inheritance,
        "modifier_graph": build_modifier_graph(contracts),
        "modifier_definitions": modifier_definitions(contracts),
        "proxy_signals": detect_proxy_signals(contracts),
        "proxy_report": proxy_report(contracts),
        "dataflow": build_dataflow(contracts, symbolic_engine),
        "protocol_model": protocol_model,
        "protocol_invariants": protocol_invariants,
        # v2 additions.
        "symbolic_engine": symbolic_engine,
        "project_profile": profile(project, sources),
        "parsers": parser_report(),
        "parser_backends": parsed.parser,
        "parse_diagnostics": parsed.diagnostics,
        "detectors": detector_signals,
        "runtime_wirings": parsed.wirings,
        "config_bindings": parsed.bindings,
        "module_graph": build_module_graph(contracts, parsed.wirings,
                                           parsed.bindings, project, sources),
        "composition": composition,
        "use_foundry": use_foundry,
        "all_contracts": all_contracts,
        "test_contracts": test_contracts,
        "include_tests": include_tests,
    }
    result = run_research(result)
    result["project"] = str(project)
    result["research_candidates"] = build_candidates(result)

    # Campaign system: run enabled campaigns against the completed result.
    # `packs` carries operator-written packs, by dotted module or by file path.
    registry = discover_packs(packs=packs)
    result["campaign_registry"] = registry
    result["campaign_packs"] = dict(registry.load_report)
    result["campaign_results"] = run_campaigns(
        registry.enabled(), result, top_global=3,
    )

    result["quality_report"] = validate(result)
    return result
