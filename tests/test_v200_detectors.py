from crystal.detectors import DETECTORS, detector_names, run_detectors
from crystal.parsers import solidity_regex
from crystal.symbolic import SymbolicEngine

REENTRANT = """
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
    }
    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
    }
    function safeWithdraw(uint256 amount) external {
        balances[msg.sender] -= amount;
        totalAssets -= amount;
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
    }
}
"""

GUARDED = """
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public balances;
    modifier nonReentrant() { _; }
    function withdraw(uint256 amount) external nonReentrant {
        (bool ok,) = msg.sender.call{value: amount}("");
        balances[msg.sender] -= amount;
    }
}
"""

ACCESS = """
pragma solidity ^0.8.20;
contract Config {
    address public owner;
    address public oracle;
    uint256 public feeRate;
    bool public paused;
    mapping(address => uint256) public balances;

    modifier onlyOwner() { require(msg.sender == owner); _; }

    function setOracleUnsafe(address x) external { oracle = x; }
    function setFee(uint256 x) external onlyOwner { feeRate = x; }
    function setPausedChecked(bool x) external { require(msg.sender == owner); paused = x; }
    function selfDeposit() external payable { balances[msg.sender] += msg.value; }
}
"""

VAULT4626 = """
pragma solidity ^0.8.20;
contract Vault4626 {
    uint256 public totalAssets;
    uint256 public totalSupply;

    function deposit(uint256 assets) external returns (uint256 shares) {
        if (totalSupply == 0) { shares = assets; } else { shares = assets * totalSupply / totalAssets; }
        totalSupply += shares;
        totalAssets += assets;
    }
    function donate(uint256 amount) external { totalAssets += amount; }
}
"""

ORACLE = """
pragma solidity ^0.8.20;
contract Pricer {
    uint256 public price;
    uint256 public checkedPrice;

    function poke() external {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        price = uint256(r0) / uint256(r1);
    }
    function pokeChecked() external {
        (, int256 answer,, uint256 updatedAt,) = feed.latestRoundData();
        require(block.timestamp - updatedAt < 3600);
        checkedPrice = uint256(answer);
    }
}
"""


def analyze(source, name="T.sol", only=None):
    contracts = solidity_regex.parse_text(source, name)
    return run_detectors(contracts, SymbolicEngine(contracts), only)


def test_detector_registry():
    assert detector_names() == [
        "access-control", "first-depositor", "oracle-manipulation", "reentrancy",
        "unbounded-input",
    ]
    assert set(DETECTORS) == set(detector_names())


def test_every_signal_is_research_only():
    for source in (REENTRANT, ACCESS, VAULT4626, ORACLE):
        for signal in analyze(source):
            assert signal.status == "RESEARCH"
            assert signal.validation_required
            assert 0.0 <= signal.confidence <= 1.0
            assert signal.falsification


def test_reentrancy_flags_call_before_write():
    signals = analyze(REENTRANT, only=["reentrancy"])
    flagged = {s.function for s in signals}
    assert "withdraw" in flagged
    assert "safeWithdraw" not in flagged

    signal = next(s for s in signals if s.function == "withdraw")
    assert signal.confidence > 0.8
    assert signal.line == 13
    assert signal.ordered_trace[0].startswith("L13 external_call")
    assert any("state_write[balances]" in step for step in signal.ordered_trace)
    assert any("read before the call" in item for item in signal.evidence)


def test_reentrancy_guard_lowers_confidence():
    guarded = analyze(GUARDED, only=["reentrancy"])
    assert guarded
    assert guarded[0].confidence < 0.5
    assert any("guard modifier" in item for item in guarded[0].evidence)


def test_access_control_flags_only_unguarded_privileged_writes():
    flagged = {s.function for s in analyze(ACCESS, only=["access-control"])}
    assert "setOracleUnsafe" in flagged
    assert "setFee" not in flagged
    assert "setPausedChecked" not in flagged
    assert "selfDeposit" not in flagged


def test_first_depositor_needs_branch_and_division():
    signals = analyze(VAULT4626, only=["first-depositor"])
    assert signals
    signal = signals[0]
    assert signal.function == "deposit"
    assert any("zero-supply branch" in item for item in signal.evidence)
    assert any("divides by protocol state" in item for item in signal.evidence)
    assert any("without minting shares" in item for item in signal.evidence)


def test_first_depositor_silent_on_plain_vault():
    plain = """
    pragma solidity ^0.8.20;
    contract Plain {
        uint256 public totalAssets;
        uint256 public totalSupply;
        function deposit(uint256 a) external { totalAssets += a; totalSupply += a; }
    }
    """
    assert not analyze(plain, only=["first-depositor"])


def test_oracle_detector_weights_freshness_check():
    signals = {s.function: s for s in analyze(ORACLE, only=["oracle-manipulation"])}
    assert "poke" in signals and "pokeChecked" in signals
    assert signals["poke"].confidence > signals["pokeChecked"].confidence
    assert any("no freshness" in item for item in signals["poke"].evidence)
    assert any("freshness/bounds check observed" in item
               for item in signals["pokeChecked"].evidence)


def test_signal_ids_are_stable_and_unique():
    first = analyze(REENTRANT)
    second = analyze(REENTRANT)
    assert [s.id for s in first] == [s.id for s in second]
    assert len({s.id for s in first}) == len(first)


def test_selection_filters_detectors():
    assert all(s.detector == "reentrancy-ordering"
               for s in analyze(REENTRANT, only=["reentrancy"]))
    assert analyze(REENTRANT, only=["access-control"]) == []
