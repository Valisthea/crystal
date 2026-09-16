"""The old entry point, written down as a question.

`research(project, use_solc=..., packs=...)` keeps working and is not going
anywhere until something measured replaces it (§33). This translates one of
those calls into the contract so the two can be compared, and so a caller can
migrate by reading what their existing invocation actually asks for.

The translation is where the legacy API's implicit assumptions become visible,
and both of them are the ones this contract exists to refuse:

* it has **no snapshot** — it scans whatever is on disk at the moment it runs.
  Written down, that is `AT_EXECUTION`, stated rather than inferred;
* it has **no surface** — it looks at everything it discovers. Written down,
  that is `discover_surface=True`, not an empty surface meaning "the project".

Neither is invented here. Both are what the call already did, said out loud.
"""

from __future__ import annotations

from pathlib import Path

from .model import AT_EXECUTION, ResearchQuestion, SourceSnapshot, Target

# What the legacy call asks for, at the depth it asks for it. `use_foundry`
# decides whether external execution is on the table, which is exactly the
# shallow/standard/deep distinction.
LEGACY_QUESTION = (
    "What security-relevant behaviour can Crystal establish across this "
    "project's contracts, without a specific claim to test?"
)


def from_legacy_call(project, *, use_solc=True, use_foundry=True,
                     languages=None, detectors=None, include_tests=False,
                     packs=(), target_id=None) -> ResearchQuestion:
    """The `ResearchQuestion` a legacy `research(...)` call is really asking.

    `target_id` is the caller's stable handle. Absent, the resolved project
    path is used and recorded as a weak identity: it is stable on one machine
    and meaningless on another, which is why §9 forbids deriving an identity
    from a basename and why this says which kind it produced.
    """
    resolved = Path(project).resolve()
    explicit = bool(target_id)

    capabilities = ["static", "dataflow", "symbolic", "differential", "composition"]
    if use_foundry:
        capabilities += ["fuzzing", "runtime"]

    constraints = {}
    if not include_tests:
        # What the engine already does; stated so the question is honest about
        # the scope it is asking for rather than leaving it to a default.
        constraints["excluded_paths"] = ["test", "tests", "mock", "mocks"]

    return ResearchQuestion(
        question=LEGACY_QUESTION,
        target=Target(
            target_id=target_id or str(resolved),
            project=str(resolved),
            path=str(resolved),
        ),
        # The legacy call reads the tree as it finds it. Said plainly.
        source_snapshot=SourceSnapshot(revision=AT_EXECUTION),
        # It looks at everything it discovers. Also said plainly.
        discover_surface=True,
        constraints=constraints,
        required_capabilities=tuple(capabilities),
        depth="deep" if use_foundry else "standard",
        expected_output=("candidate", "evidence"),
        provenance={
            "asked_by": "crystal.question.legacy.from_legacy_call",
            "origin": "legacy research() call",
            "target_identity": "caller-supplied" if explicit else "resolved-path",
            "configuration": {
                "use_solc": use_solc,
                "use_foundry": use_foundry,
                "languages": list(languages) if languages else None,
                "detectors": list(detectors) if detectors else None,
                "include_tests": include_tests,
                "packs": [str(pack) for pack in packs or ()],
            },
        },
    )


def to_legacy_kwargs(question: ResearchQuestion) -> dict:
    """The `research(...)` keywords this question maps onto today.

    A lossy direction, and it says so: the question's surface, prior evidence,
    budget and expected output have no legacy equivalent. `lost` names them
    rather than letting a caller believe the round trip was faithful.
    """
    configuration = dict(question.provenance.get("configuration") or {})
    lost = []
    if question.affected_surface:
        lost.append("affected_surface")
    if question.prior_evidence:
        lost.append("prior_evidence")
    if question.budget:
        lost.append("budget")
    if question.expected_output:
        lost.append("expected_output")
    if question.hypothesis:
        lost.append("hypothesis")
    if not question.source_snapshot.resolve_at_execution:
        lost.append("source_snapshot")

    return {
        "kwargs": {
            "use_solc": configuration.get("use_solc", True),
            "use_foundry": "runtime" in question.required_capabilities,
            "languages": configuration.get("languages"),
            "detectors": configuration.get("detectors"),
            "include_tests": configuration.get("include_tests", False),
            "packs": tuple(configuration.get("packs") or ()),
        },
        "project": question.target.project or question.target.path,
        "lost": lost,
    }
