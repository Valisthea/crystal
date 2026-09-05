"""A backend that cannot execute a precompile cannot decide a path through it.

Measured on halmos 0.3.3: a settlement path reaching `sha256` ran for 26
minutes and produced no verdict — the log never left compilation output and the
process reached 940 MB. That is neither a failure nor a pass. Left unnamed it
is worse than either, because an operator waits, kills it, and concludes the
backend is broken rather than that the property is undecidable there.

`BackendTraits.unmodelled_precompiles` declares it, and the property is
reported UNSUPPORTED before anything is launched — the same discipline as
`advances_block`, which stops a block-gated property from returning PASS on a
tool that pins `block.number` to 1.
"""

import tempfile
from pathlib import Path

import pytest

from crystal.backends import halmos, medusa, preflight
from crystal.parsers import solidity_ts, treesitter_enabled
from crystal.parsers import parse_project

HASHED = """
pragma solidity ^0.8.20;
contract Settle {
    mapping(bytes32 => uint256) public paid;
    function settle(bytes calldata proof, uint256 amount) external {
        bytes32 id = sha256(proof);
        paid[id] += amount;
    }
}
"""

PLAIN = """
pragma solidity ^0.8.20;
contract Settle {
    mapping(bytes32 => uint256) public paid;
    function settle(bytes32 id, uint256 amount) external {
        paid[id] += amount;
    }
}
"""

REACHED_THROUGH_A_HELPER = """
pragma solidity ^0.8.20;
contract Settle {
    mapping(bytes32 => uint256) public paid;
    function settle(bytes calldata proof, uint256 amount) external {
        bytes32 id = _identify(proof);
        paid[id] += amount;
    }
    function _identify(bytes calldata proof) internal pure returns (bytes32) {
        return sha256(proof);
    }
}
"""


class _Property:
    """The duck-typed shape the backends consume."""

    name = "conservation"
    subject = ()
    expression = "paid is conserved"
    filename = "CrystalHalmos_conservation.t.sol"
    source = ""
    invariant = None


def _contracts(source):
    root = Path(tempfile.mkdtemp())
    (root / "P.sol").write_text(source, encoding="utf-8")
    return parse_project([root / "P.sol"]).contracts


def _issues(source, traits, backend):
    contracts = _contracts(source)
    return preflight.precompile_issues(
        backend, traits, contracts[0], [_Property()], contracts
    )


def test_a_backend_that_cannot_run_the_precompile_says_so(tmp_path):
    (issue,) = _issues(HASHED, halmos.TRAITS, "halmos")
    assert issue.severity == preflight.UNSUPPORTED
    assert issue.kind == "unmodelled-precompile"
    assert "sha256" in issue.message
    assert "settle" in issue.message
    assert "no verdict rather than an answer" in issue.message


def test_a_backend_that_can_run_it_stays_silent(tmp_path):
    assert _issues(HASHED, medusa.TRAITS, "medusa") == []


def test_a_path_without_the_precompile_is_not_flagged(tmp_path):
    assert _issues(PLAIN, halmos.TRAITS, "halmos") == []


@pytest.mark.skipif(
    not treesitter_enabled() or not solidity_ts.available(),
    reason="the regex front-end extracts no call from a `return` statement, so "
           "`return sha256(x)` inside the helper is invisible to it — a "
           "declared limitation of that path, not of this check. See "
           "crystal.parsers.SOLIDITY_REGEX_LIMITATIONS.",
)
def test_the_precompile_is_found_through_an_internal_helper(tmp_path):
    """The call is one hop away, which is where it actually sits in practice."""
    (issue,) = _issues(REACHED_THROUGH_A_HELPER, halmos.TRAITS, "halmos")
    assert "sha256" in issue.message


CALL_INSIDE_AN_INDEX = """
pragma solidity ^0.8.20;
contract Settle {
    mapping(bytes32 => uint256) public paid;
    function settle(bytes calldata proof, uint256 amount) external {
        paid[_identify(proof)] += amount;
    }
    function _identify(bytes calldata proof) internal pure returns (bytes32) {
        return sha256(proof);
    }
}
"""


@pytest.mark.xfail(
    strict=True,
    reason="the IR records no call for `paid[_identify(x)] += y`: a call inside "
           "an index expression on the left-hand side is dropped by both "
           "front-ends, so nothing downstream can follow it. A parser gap, not "
           "a gap in this check — pinned here so it is visible rather than "
           "silently narrowing every traversal built on `ir.calls()`.",
)
def test_a_call_inside_an_index_expression_is_reachable(tmp_path):
    assert _issues(CALL_INSIDE_AN_INDEX, halmos.TRAITS, "halmos")


def test_an_unresolved_subject_widens_the_check_rather_than_skipping_it(tmp_path):
    """Skipping on an unresolved subject would pass through the one case the
    gate cannot reason about, which is exactly the case it exists for."""
    contracts = _contracts(HASHED)
    empty_subject = _Property()
    assert empty_subject.subject == ()
    assert preflight.precompile_issues(
        "halmos", halmos.TRAITS, contracts[0], [empty_subject], contracts
    )


def test_halmos_declares_what_it_cannot_execute():
    assert "sha256" in halmos.TRAITS.unmodelled_precompiles
    assert not getattr(medusa.TRAITS, "unmodelled_precompiles", ())
