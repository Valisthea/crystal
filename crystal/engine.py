"""Crystal research pipeline: discover -> parse -> analyze -> graphs -> research."""

from __future__ import annotations

from .analysis import analyze
from .compiler.solc import compile_standard
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
from .semantics.proxy import detect_proxy_signals
from .semantics.proxy import report as proxy_report
from .semantics.solc_ast import parse_solc_ast
from .sequences import generate_sequences
from .symbolic import SymbolicEngine


def research(project, use_solc=True, languages=None, run_detector_pass=True,
             use_foundry=True, detectors=None):
    sources = discover(project, languages=languages)
    parsed = parse_project(sources)
    contracts = parsed.contracts
    # Base-contract state must be attributed to derived contracts before any
    # analysis runs, otherwise every inherited variable looks untouched.
    inheritance = link_inheritance(contracts)

    observations = analyze(contracts)
    hypotheses = rank(generate(observations))

    symbolic_engine = SymbolicEngine(contracts)

    program_graph = build_graph(contracts)
    state_graph = build_state_graph(contracts)
    cfgs = [build_cfg(f) for c in contracts for f in c.functions]
    call_graph = build_call_graph(contracts)
    storage_graph = build_storage_graph(contracts)
    invariants = discover_invariants(contracts)
    sequences = generate_sequences(candidate_sequences(state_graph), invariants)

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
        "detectors": run_detectors(contracts, symbolic_engine, detectors)
        if run_detector_pass else [],
        "use_foundry": use_foundry,
    }
    result = run_research(result)
    result["project"] = str(project)
    result["research_candidates"] = build_candidates(result)
    result["quality_report"] = validate(result)
    return result
