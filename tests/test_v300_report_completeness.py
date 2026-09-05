"""Report completeness: nothing leaves research without the report saying so.

Measured on a real target, 52 of 69 contracts were excluded from research and
no output format carried a reason for any of them; the Markdown listed forty
names and stopped. A campaign candidate selected by several campaigns named all
of them in the JSON, but the Markdown carried that list only as an evidence
line past its evidence cap. And the coupling grade that sets a detector's
confidence band sat in one evidence line among a dozen, so sorting forty
signals by it meant reading every one. Each was a report that looked complete.

Crystal stays evidence-only throughout: every payload here carries
`confirmed_findings == 0`, and nothing added below is a decision.
"""

import json

import pytest

from crystal.campaigns.result import CampaignCandidate, CampaignResult
from crystal.engine import research
from crystal.report import (
    MARKDOWN_CANDIDATES_PER_CAMPAIGN,
    MARKDOWN_DETECTOR_DETAILS,
    PARSER_FIXTURE_REASON,
    _campaign_markdown,
    _campaign_payload,
    _coupling_grade,
    arcadia,
    markdown,
    payload,
    sarif,
)

REAL = ("pragma solidity ^0.8.20; contract RealPegOut "
        "{ uint256 public x; function poke() external { x += 1; } }")
LEGACY = ("pragma solidity ^0.8.20; contract OldVault "
          "{ uint256 public y; function poke() external { y += 1; } }")
# Under `test/`, so the parser flags it while parsing: the engine never sees
# it and records no reason for it.
HELPER = ("pragma solidity ^0.8.20; contract Helper "
          "{ uint256 public h; function poke() external { h += 1; } }")
# More scaffolding than the old Markdown cap of forty names.
WIDGETS = 45

POOL = (
    "pragma solidity ^0.8.20; contract Pool {"
    " uint256 public totalAssets; uint256 public balances;"
    " function deposit() external payable"
    " { totalAssets += msg.value; balances += msg.value; }"
    " function sync() external { balances = totalAssets; } }"
)

# Two operator campaigns with identical, wide scope: every chain one selects,
# the other selects too. Each carries its own question.
DUP_PACK = "\n".join([
    "from crystal.campaigns.definition import CampaignDefinition, CampaignScope",
    "CAMPAIGNS = [",
    "    CampaignDefinition(campaign_id='dup-a', name='A', description='op',",
    "        pack='dup', questions=['is the pool balance conserved?'],",
    "        scope=CampaignScope(max_sequence_length=3, max_candidates=10)),",
    "    CampaignDefinition(campaign_id='dup-b', name='B', description='op',",
    "        pack='dup', questions=['who may call sync?'],",
    "        scope=CampaignScope(max_sequence_length=3, max_candidates=10)),",
    "]",
    "",
])

# The effect-coupled shape from the coupling-grade tests: the differentiating
# guard reads state the shared callee writes and constrains an argument it
# consumes, so `asymmetric-side-effect` grades it `effect-coupled`.
EFFECT = """
pragma solidity ^0.8.20;
interface ILedger {
    function held(address who) external view returns (uint256);
    function settle(address who, uint256 amount) external;
}
contract Ledger is ILedger {
    mapping(address => uint256) private _stored;
    function held(address who) external view override returns (uint256) {
        return _stored[who];
    }
    function settle(address who, uint256 amount) external override {
        uint256 taken = amount < _stored[who] ? amount : _stored[who];
        _stored[who] -= taken;
    }
}
contract Desk {
    error TooLittle(uint256 held);
    ILedger private _ledger;
    mapping(bytes32 => bool) private _done;
    function closeByOwner(bytes32 id, address who, uint256 amount) external {
        _done[id] = true;
        uint256 stored = _ledger.held(who);
        if (stored < amount) {
            revert TooLittle(stored);
        }
        _ledger.settle(who, amount);
    }
    function closeByAnyone(bytes32 id, address who, uint256 amount) external {
        _done[id] = true;
        _ledger.settle(who, amount);
    }
}
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _widget(n):
    return (f"pragma solidity ^0.8.20; contract Widget{n} "
            f"{{ uint256 public z; function poke() external {{ z += 1; }} }}")


def _section(text, heading):
    """The body of one `## heading`, up to the next `## `."""
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _candidate(campaign_id, sequence, score=0.5, **extra):
    return CampaignCandidate(
        candidate_id=f"{campaign_id}:{'>'.join(sequence)}",
        campaign_id=campaign_id, category="balance", target_contract="Pool",
        target_functions=tuple(sequence),
        hypothesis="value transfer may create accounting asymmetry",
        state_sequence=tuple(sequence), score=score, **extra,
    )


@pytest.fixture(scope="module")
def excluded(tmp_path_factory):
    project = tmp_path_factory.mktemp("excluded") / "lbc"
    src = project / "src"
    _write(src / "RealPegOut.sol", REAL)
    _write(src / "legacy" / "OldVault.sol", LEGACY)
    _write(src / "test" / "Helper.sol", HELPER)
    for n in range(WIDGETS):
        _write(src / "test-contracts" / f"Widget{n}.sol", _widget(n))
    result = research(str(project), use_solc=False, use_foundry=False)
    return result, payload(result)


@pytest.fixture(scope="module")
def graded(tmp_path_factory):
    project = tmp_path_factory.mktemp("graded")
    _write(project / "Desk.sol", EFFECT)
    return payload(research(str(project), use_solc=False, use_foundry=False))


@pytest.fixture(scope="module")
def duplicated(tmp_path_factory):
    root = tmp_path_factory.mktemp("dup")
    project = root / "proj"
    _write(project / "src" / "Pool.sol", POOL)
    pack = _write(root / "dup.py", DUP_PACK)
    return payload(research(str(project), use_solc=False, use_foundry=False,
                            packs=[str(pack)]))


# -- Gap 1: every excluded contract, with its reason ------------------------

def test_every_excluded_contract_carries_a_reason_in_the_payload(excluded):
    result, data = excluded
    assert [c.name for c in result["contracts"]] == ["RealPegOut"]
    widgets = {f"Widget{n}" for n in range(WIDGETS)}

    # The engine's own list, verbatim: directory exclusions only.
    scaffolding = {e["contract"]: e["reason"] for e in data["excluded_scaffolding"]}
    assert scaffolding["OldVault"] == "superseded/legacy directory"
    assert all(scaffolding[w] == "test-scaffolding directory" for w in widgets)
    assert "Helper" not in scaffolding, "the parser flagged it; the engine has no reason"
    assert data["summary"]["scaffolding_excluded"] == len(scaffolding) == WIDGETS + 1

    # The joined list: a reason on every row, including the parser's.
    rows = {r["contract"]: r for r in data["excluded_contracts"]}
    assert set(rows) == widgets | {"OldVault", "Helper"}
    assert all(r["reason"] and r["path"] for r in rows.values())
    assert rows["Helper"]["reason"] == PARSER_FIXTURE_REASON
    assert rows["OldVault"]["reason"] == "superseded/legacy directory"
    assert rows["OldVault"]["path"].endswith("OldVault.sol")

    # It covers the whole excluded bucket, and the v2 list is untouched.
    assert len(rows) == data["summary"]["test_contracts_excluded"] == WIDGETS + 2
    assert len(data["excluded_test_contracts"]) == len(rows)
    assert data["excluded_test_contracts"] == sorted(data["excluded_test_contracts"])
    assert "OldVault (OldVault.sol)" in data["excluded_test_contracts"]

    # It survives JSON, and it reaches the Arcadia hand-off.
    round_trip = json.loads(json.dumps(data, default=list))
    assert round_trip["excluded_contracts"] == data["excluded_contracts"]
    assert arcadia(data)["target"]["excluded_contracts"] == data["excluded_contracts"]


def test_every_excluded_contract_is_rendered_with_its_reason(excluded):
    _, data = excluded
    section = _section(markdown(data), "Excluded fixtures")
    rows = {r["contract"]: r for r in data["excluded_contracts"]}

    table = {}
    for line in section.splitlines():
        if line.startswith("| `"):
            table[line.split("`")[1]] = line
    # Uncapped: forty-seven rows, where the old list stopped at forty names.
    assert set(table) == set(rows)
    for name, line in table.items():
        assert rows[name]["reason"] in line, f"{name} rendered without its reason"
    # Paths are relative to the scanned root, so the row is readable.
    assert "| `src/legacy/OldVault.sol` |" in table["OldVault"]
    assert "| `src/test/Helper.sol` |" in table["Helper"]

    assert f"{WIDGETS + 2} type(s) were parsed but kept out of research" in section
    assert "1 classified as a test fixture by the parser" in section
    assert f"{WIDGETS + 1} by directory rule" in section
    assert "--include-tests" in section


def test_include_tests_leaves_the_excluded_bucket_empty(tmp_path):
    project = tmp_path / "lbc"
    _write(project / "src" / "RealPegOut.sol", REAL)
    _write(project / "src" / "legacy" / "OldVault.sol", LEGACY)
    _write(project / "src" / "test" / "Helper.sol", HELPER)
    data = payload(research(str(project), use_solc=False, use_foundry=False,
                            include_tests=True))
    assert data["excluded_scaffolding"] == []
    assert data["excluded_contracts"] == []
    assert data["excluded_test_contracts"] == []
    assert data["summary"]["scaffolding_excluded"] == 0
    assert data["summary"]["test_contracts_excluded"] == 0
    assert "## Excluded fixtures" not in markdown(data)


def test_a_payload_without_reasons_still_renders_the_names(excluded):
    """A JSON file written before the reasons existed still renders."""
    _, data = excluded
    older = dict(data)
    older.pop("excluded_contracts")
    section = _section(markdown(older), "Excluded fixtures")
    assert "- `OldVault (OldVault.sol)`" in section


# -- Gap 2: campaign fields reach both formats, one chain rendered once ------

def test_campaign_fields_reach_the_payload(duplicated):
    campaigns = duplicated["campaign_results"]
    assert campaigns
    for campaign in campaigns:
        assert isinstance(campaign["pruned_by"], dict)
        assert isinstance(campaign["deferred"], list)
        for candidate in campaign["candidates"]:
            assert "selected_by_campaigns" in candidate
            assert "questions" in candidate
    chains = [tuple(c["state_sequence"]) for cr in campaigns for c in cr["candidates"]]
    assert chains and len(chains) == len(set(chains))
    json.dumps(duplicated, default=list)


def test_a_multi_campaign_candidate_is_rendered_once_naming_every_campaign(duplicated):
    text = markdown(duplicated)
    lines = text.splitlines()
    checked = 0
    for campaign in duplicated["campaign_results"]:
        for candidate in campaign["candidates"][:MARKDOWN_CANDIDATES_PER_CAMPAIGN]:
            chain = " -> ".join(candidate["state_sequence"])
            assert text.count(f"**{chain}**") == 1, f"{chain} rendered more than once"
            if not {"dup-a", "dup-b"} <= set(candidate["selected_by_campaigns"]):
                continue
            checked += 1
            start = next(i for i, line in enumerate(lines) if f"**{chain}**" in line)
            block = []
            for line in lines[start + 1:]:
                if not line.startswith("  - "):
                    break
                block.append(line)
            # Built-in campaigns select this chain too; every one of them is
            # named, not just the two operator campaigns, and the count agrees.
            expected = set(candidate["selected_by_campaigns"]) | {campaign["campaign_id"]}
            selected = [line for line in block if line.startswith("  - selected by ")]
            assert len(selected) == 1, f"{chain} names its campaigns {len(selected)} times"
            count, _, named = selected[0].removeprefix("  - selected by ").partition(" campaigns: ")
            assert {name.strip("`") for name in named.split(", ")} == expected
            assert int(count) == len(expected)
            questions = [line for line in block if "open question: " in line]
            assert questions, f"{chain} rendered without its questions"
            assert all(
                q in ("  - open question: is the pool balance conserved?",
                      "  - open question: who may call sync?")
                for q in questions
            )
            head = lines[start]
            assert "confidence " in head and "validation `" in head
    assert checked, "no aggregated candidate reached the Markdown"
    # Every campaign that ran is in the table, whether or not it produced.
    for campaign in duplicated["campaign_results"]:
        assert f"| `{campaign['campaign_id']}` |" in text


def test_a_duplicate_chain_is_rendered_once_even_without_the_runner_pass():
    """A payload built without the runner's dedup still renders a chain once."""
    chain = ("Pool.deposit", "Pool.sync")
    data = {"campaign_results": _campaign_payload({"campaign_results": [
        CampaignResult("one", "One", candidates=[_candidate("one", chain, 0.7)]),
        CampaignResult("two", "Two", candidates=[_candidate("two", chain, 0.6)]),
    ]})}
    text = "\n".join(_campaign_markdown(data))
    assert text.count("**Pool.deposit -> Pool.sync**") == 1
    assert "  - selected by 2 campaigns: `one`, `two`" in text
    assert "1 candidate(s) already rendered under another campaign above." in text


def test_campaign_warnings_deferred_and_caps_are_visible():
    busy = [
        _candidate("busy", ("Pool.deposit", f"Pool.f{n}"), score=1.0 - n / 10,
                   questions=("does f conserve value?",))
        for n in range(MARKDOWN_CANDIDATES_PER_CAMPAIGN + 2)
    ]
    data = {"campaign_results": _campaign_payload({"campaign_results": [
        CampaignResult("quiet", "Quiet", warning="no contracts matched the campaign scope"),
        CampaignResult("busy", "Busy", candidates=busy,
                       deferred=["abc: Pool.deposit -> Pool.z"],
                       total_sequences_explored=9, total_sequences_pruned=3,
                       pruned_by={"allowed_categories": 3}),
    ]})}
    text = "\n".join(_campaign_markdown(data))
    # A warning on a campaign that produced nothing is no longer dropped.
    assert "- `quiet` — no contracts matched the campaign scope" in text
    assert f"| `busy` | {len(busy)} | 1 | 9 | 3 | allowed_categories=3 |" in text
    assert "| `quiet` | 0 | 0 | 0 | 0 | — |" in text
    assert text.count("**Pool.deposit -> ") == MARKDOWN_CANDIDATES_PER_CAMPAIGN
    assert "- 2 more candidate(s) not detailed here" in text
    assert "- 1 lower-scored candidate(s) deferred past the campaign's `max_candidates`" in text
    assert text.count("  - open question: does f conserve value?") == \
        MARKDOWN_CANDIDATES_PER_CAMPAIGN


# -- Gap 3: the coupling grade, beside the confidence it governs ------------

def test_coupling_grade_is_read_only_from_a_grade_line():
    assert _coupling_grade((
        "shared transition: _ledger.settle(who, amount)",
        "coupling [effect-coupled]: `stored` is read from `_ledger`",
    )) == "effect-coupled"
    assert _coupling_grade(("coupling [partially-coupled]: x",)) == "partially-coupled"
    assert _coupling_grade(("coupling [uncoupled]: y",)) == "uncoupled"
    # Nothing is inferred: no grade line, no grade.
    assert _coupling_grade(()) is None
    assert _coupling_grade(("the asymmetry is real",)) is None
    assert _coupling_grade(("  coupling [x]: not at line start",)) is None
    assert _coupling_grade(("coupling []: empty",)) is None


def test_coupling_grade_reaches_payload_and_markdown(graded):
    signals = graded["detectors"]
    assert all("coupling" in s for s in signals)
    asymmetric = [s for s in signals if s["detector"] == "asymmetric-side-effect"]
    assert asymmetric, "the effect-coupled shape produced no asymmetry signal"
    signal = asymmetric[0]
    assert signal["coupling"] == "effect-coupled"
    assert signal["status"] == "RESEARCH" and graded["summary"]["confirmed_findings"] == 0

    section = _section(markdown(graded), "Detector signals")
    table_rows = [line for line in section.splitlines()
                  if line.startswith("| ") and "`asymmetric-side-effect`" in line]
    assert table_rows and all("| effect-coupled |" in row for row in table_rows)
    assert (
        f"- Detector: `asymmetric-side-effect` — confidence {signal['confidence']}"
        " — coupling `effect-coupled` — status `RESEARCH`"
    ) in section
    # A detector that does not grade shows no grade rather than a guess.
    for other in (s for s in signals if s["coupling"] is None):
        assert f"| `{other['contract']}.{other['function']}` | {other['confidence']} | — |" in section

    # SARIF and Arcadia still consume the widened record.
    assert sarif(graded)["runs"][0]["results"]
    assert arcadia(graded)["signals"]["detectors"][0]["coupling"] in (
        "effect-coupled", "partially-coupled", "uncoupled", None,
    )


def test_detector_table_lists_every_signal_and_announces_the_detail_cap(graded):
    template = graded["detectors"][0]
    many = dict(graded)
    many["detectors"] = [
        dict(template, id=f"sig-{n}", function=f"fn{n}", line=n + 1)
        for n in range(MARKDOWN_DETECTOR_DETAILS + 3)
    ]
    section = _section(markdown(many), "Detector signals")
    numbered = [line for line in section.splitlines()
                if line.startswith("| ") and line.split("|")[1].strip().isdigit()]
    assert len(numbered) == MARKDOWN_DETECTOR_DETAILS + 3
    assert section.count("\n### ") == MARKDOWN_DETECTOR_DETAILS
    assert (f"The first {MARKDOWN_DETECTOR_DETAILS} of {MARKDOWN_DETECTOR_DETAILS + 3}"
            " are detailed below") in section


# -- The contract that must not move ----------------------------------------

def test_additions_leave_the_evidence_only_contract_untouched(excluded, graded):
    for data in (excluded[1], graded):
        assert {"tool", "version", "status", "summary", "finding_gate",
                "evidence_records", "output_contract"} <= set(data)
        assert data["summary"]["confirmed_findings"] == 0
        assert data["summary"]["output_role"] == "evidence-only"
        assert data["output_contract"]["final_finding_decision"] is False
        text = markdown(data)
        assert "## Confirmed findings" in text
        assert "0 — deciding whether a signal is a vulnerability" in text
