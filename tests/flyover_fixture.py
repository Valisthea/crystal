"""Deployment fixture for the Rootstock Flyover target (`lbc`).

This is the human-written part of the path: what no engine can derive from the
sources without fabricating — how the upgradeable stack is deployed and wired,
who the actors are, and how to build the five calls whose arguments are an
EIP-712 signature by a provider key or a Bitcoin transaction that must parse.
Everything else (handlers for the other 25 entry points, holder tracking,
ledgers, the properties, the vacuity gate) is generated.

Values mirror the target's own test bases (`test/helpers/FlyoverTestBase.sol`).
"""

from __future__ import annotations

from crystal.properties import Actor, Fixture, Instance, Recipe, SetupCall

ERC1967 = "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol"

MEMBERS = r"""
// ── ghost copies of the calldata this harness sent (never read back from storage) ──
mapping(bytes32 => Quotes.PegOutQuote) internal _poQuote;
mapping(bytes32 => uint256) internal _poDepositTs;
mapping(bytes32 => Quotes.PegInQuote) internal _piQuote;
uint256 internal _nonce;
bytes32 internal constant BLOCK_HASH = bytes32(uint256(0xB70C));
// 21-byte testnet P2PKH address: version 0x6f + hash160
bytes internal constant BTC_ADDRESS = hex"6f89abcdefabbaabbaabbaabbaabbaabbaabbaabba";

function _lpOf(uint256 seed) internal view returns (address) {
    uint256 pick = seed % 3;
    return pick == 0 ? lp0 : (pick == 1 ? lp1 : lp2);
}

function _lpKey(address lp) internal view returns (uint256) {
    if (lp == lp0) return lp0Key;
    if (lp == lp1) return lp1Key;
    if (lp == lp2) return lp2Key;
    return 0;
}

function _signPegOut(Quotes.PegOutQuote memory q) internal returns (bytes memory) {
    (uint8 v, bytes32 r, bytes32 s) = _vm.sign(_lpKey(q.lpRskAddress), pegOut.hashPegOutQuoteEIP712(q));
    return abi.encodePacked(r, s, v);
}

function _signPegIn(Quotes.PegInQuote memory q) internal returns (bytes memory) {
    (uint8 v, bytes32 r, bytes32 s) = _vm.sign(_lpKey(q.liquidityProviderRskAddress), pegIn.hashPegInQuoteEIP712(q));
    return abi.encodePacked(r, s, v);
}

/// @dev 80-byte Bitcoin block header carrying `ts` at bytes 68..71 (little-endian).
function _header(uint32 ts) internal pure returns (bytes memory h) {
    h = new bytes(80);
    h[68] = bytes1(uint8(ts));
    h[69] = bytes1(uint8(ts >> 8));
    h[70] = bytes1(uint8(ts >> 16));
    h[71] = bytes1(uint8(ts >> 24));
}

function _le64(uint64 v) internal pure returns (bytes memory out) {
    out = new bytes(8);
    for (uint256 i = 0; i < 8; i++) out[i] = bytes1(uint8(v >> (8 * i)));
}

/// @dev Legacy-serialised Bitcoin transaction with a P2PKH output paying `valueWei`
/// (rounded up to satoshis) to `depositAddress` and an OP_RETURN output carrying
/// `quoteHash`: exactly what `_validatePegOutTransaction` parses (pure-Solidity
/// replica of the repo's `script/helpers/generate-btc-tx.ts`, p2pkh variant).
function _btcTx(bytes memory depositAddress, uint256 valueWei, bytes32 quoteHash) internal pure returns (bytes memory) {
    uint256 sats = valueWei % 1e10 == 0 ? valueWei / 1e10 : valueWei / 1e10 + 1;
    bytes memory hash160 = new bytes(20);
    for (uint256 i = 0; i < 20; i++) hash160[i] = depositAddress[i + 1];
    return abi.encodePacked(
        hex"01000000", hex"01", bytes32(uint256(0x013503c427ba4605)), hex"00000000", hex"00", hex"ffffffff",
        hex"02",
        _le64(uint64(sats)), hex"19", hex"76a914", hash160, hex"88ac",
        bytes8(0), hex"22", hex"6a20", quoteHash,
        hex"00000000"
    );
}
""".strip("\n")

DEPOSIT_PEG_OUT = r"""
address lp = _lpOf(seed);
address user = ((seed >> 8) & 1) == 0 ? user0 : user1;
Quotes.PegOutQuote memory q;
_nonce += 1;
q.chainId = block.chainid;
q.callFee = 1e14;
q.penaltyFee = _crystal_bound(penaltySeed, 0.001 ether, 2 ether);
q.value = _crystal_bound(valueSeed, 0.01 ether, 5 ether);
q.gasFee = 100;
q.lbcAddress = address(pegOut);
q.lpRskAddress = lp;
q.rskRefundAddress = user;
q.nonce = int64(uint64(_nonce));
q.agreementTimestamp = uint32(block.timestamp);
q.depositDateLimit = uint32(block.timestamp + 7200);
q.transferTime = 3600;
q.expireDate = uint32(block.timestamp + 20000);
q.expireBlock = uint32(block.number + 200);
q.depositConfirmations = 10;
q.transferConfirmations = 2;
q.depositAddress = BTC_ADDRESS;
q.btcRefundAddress = BTC_ADDRESS;
q.lpBtcAddress = BTC_ADDRESS;
bytes32 h = pegOut.hashPegOutQuote(q);
_poQuote[h] = q;
_poDepositTs[h] = block.timestamp;
c.caller = user;
c.value = q.value + q.callFee + q.gasFee;
c.data = abi.encodeCall(pegOut.depositPegOut, (q, _signPegOut(q)));
c.key = h;
c.touched = new address[](2);
c.touched[0] = lp;
c.touched[1] = user;
""".strip("\n")

REFUND_PEG_OUT = r"""
bytes32 h = _crystal_pickKey_PegOutContract___pegOutQuotes(seed);
if (h == bytes32(0)) { c.skip = true; return c; }
Quotes.PegOutQuote memory q = _poQuote[h];
// first-confirmation header timestamp: late (penalty path) or on time, by seed bit
uint256 expected = _poDepositTs[h] + q.transferTime + pegOut.btcBlockTime();
uint256 jitter = (lateSeed >> 1) % 7200;
uint32 ts = (lateSeed & 1) == 1
    ? uint32(expected + 1 + jitter)
    : uint32(expected > jitter ? expected - jitter : 0);
bridge.setHeaderByHash(BLOCK_HASH, _header(ts));
bridge.setConfirmations(int256(uint256(q.transferConfirmations)));
bytes32[] memory merkle = new bytes32[](1);
merkle[0] = bytes32(uint256(1));
c.caller = q.lpRskAddress;
c.data = abi.encodeCall(pegOut.refundPegOut, (h, _btcTx(q.depositAddress, q.value, h), BLOCK_HASH, 0, merkle));
c.key = h;
c.touched = new address[](1);
c.touched[0] = q.lpRskAddress;
""".strip("\n")

REFUND_USER_PEG_OUT = r"""
bytes32 h = _crystal_pickKey_PegOutContract___pegOutQuotes(seed);
if (h == bytes32(0)) { c.skip = true; return c; }
c.caller = _crystal_actor(seed >> 8);
c.data = abi.encodeCall(pegOut.refundUserPegOut, (h));
c.key = h;
c.touched = new address[](1);
c.touched[0] = _poQuote[h].rskRefundAddress;
""".strip("\n")

CALL_FOR_USER = r"""
Quotes.PegInQuote memory q;
bytes32 h;
bytes32 known = _crystal_pickKey_PegInContract___processedQuotes(seed >> 4);
if ((seed & 1) == 1 && known != bytes32(0)) {
    // replay an existing quote
    h = known;
    q = _piQuote[h];
} else {
    _nonce += 1;
    q.chainId = block.chainid;
    q.callFee = 1e14;
    q.penaltyFee = _crystal_bound(seed >> 16, 0.001 ether, 1 ether);
    q.value = _crystal_bound(valueSeed, 0.5 ether, 3 ether);
    q.gasFee = 100;
    q.lbcAddress = address(pegIn);
    q.liquidityProviderRskAddress = _lpOf(seed >> 2);
    q.contractAddress = user1;
    q.rskRefundAddress = payable(user0);
    q.nonce = int64(uint64(_nonce));
    q.gasLimit = 21000;
    q.agreementTimestamp = uint32(block.timestamp);
    q.timeForDeposit = 3600;
    q.callTime = 7200;
    q.depositConfirmations = 2;
    q.callOnRegister = false;
    q.btcRefundAddress = BTC_ADDRESS;
    q.liquidityProviderBtcAddress = BTC_ADDRESS;
    q.data = hex"";
    h = pegIn.hashPegInQuote(q);
    _piQuote[h] = q;
}
c.caller = q.liquidityProviderRskAddress;
c.value = q.value;
c.data = abi.encodeCall(pegIn.callForUser, (q));
c.key = h;
c.touched = new address[](3);
c.touched[0] = q.liquidityProviderRskAddress;
c.touched[1] = user1;
c.touched[2] = user0;
""".strip("\n")

REGISTER_PEG_IN = r"""
bytes32 h = _crystal_pickKey_PegInContract___processedQuotes(seed);
if (h == bytes32(0)) { c.skip = true; return c; }
Quotes.PegInQuote memory q = _piQuote[h];
uint256 total = q.value + q.callFee + q.gasFee;
uint256 amount = _crystal_bound(amountSeed, total, total + 1 ether);
uint256 height = _crystal_bound(heightSeed, 100, 100000);
bridge.setPegin{value: amount}(h);
bridge.setHeader(height, _header(uint32(block.timestamp)));
bridge.setHeader(height + q.depositConfirmations - 1, _header(uint32(block.timestamp)));
c.caller = _crystal_actor(seed >> 8);
c.data = abi.encodeCall(pegIn.registerPegIn, (q, _signPegIn(q), hex"", hex"", height));
c.key = h;
c.touched = new address[](2);
c.touched[0] = q.liquidityProviderRskAddress;
c.touched[1] = q.rskRefundAddress;
""".strip("\n")

HALMOS_PRELUDE = r"""
// halmos links libraries from `out/` by file basename, and this repo has two Quotes.sol /
// SignatureValidator.sol (src/libraries and src/legacy); put the imported ones behind the
// linked addresses so every Quotes.* delegatecall reaches the live library.
_vm.etch(address(Quotes), type(Quotes).runtimeCode);
_vm.etch(address(SignatureValidator), type(SignatureValidator).runtimeCode);
""".strip("\n")


def flyover_fixture(project_root: str) -> Fixture:
    return Fixture(
        project_root=project_root,
        name="flyover",
        instances=[
            Instance("PauseRegistry", "pauseRegistry", "src/PauseRegistry.sol",
                     proxy="ERC1967Proxy", proxy_path=ERC1967,
                     init="initialize", init_args=("0", "admin")),
            Instance("CollateralManagementContract", "collateral", "src/CollateralManagement.sol",
                     proxy="ERC1967Proxy", proxy_path=ERC1967, init="initialize",
                     init_args=("admin", "30", "0.6 ether", "500", "1000", "pauseRegistry")),
            Instance("BridgeMock", "bridge", "src/test-contracts/BridgeMock.sol"),
            Instance("PegOutContract", "pegOut", "src/PegOutContract.sol",
                     proxy="ERC1967Proxy", proxy_path=ERC1967, init="initialize",
                     init_args=("admin", "payable(address(bridge))", "1e11", "address(collateral)",
                                "false", "3600", "pauseRegistry")),
            Instance("PegInContract", "pegIn", "src/PegInContract.sol",
                     proxy="ERC1967Proxy", proxy_path=ERC1967, init="initialize",
                     init_args=("admin", "payable(address(bridge))", "2300 * 65164000", "0.5 ether",
                                "address(collateral)", "false", "pauseRegistry")),
        ],
        actors=[
            Actor("admin", 0xAD01), Actor("adder", 0xADD1), Actor("slasher", 0x5A51),
            Actor("lp0", 0xA0), Actor("lp1", 0xA1), Actor("lp2", 0xA2),
            Actor("user0", 0xB0), Actor("user1", 0xB1), Actor("punisher", 0xC0),
        ],
        setup=[
            SetupCall("_vm.deal(address(this), 1000000 ether);"),
            SetupCall("bytes32 adderRole = collateral.COLLATERAL_ADDER();"),
            SetupCall("bytes32 slasherRole = collateral.COLLATERAL_SLASHER();"),
            SetupCall("collateral.grantRole(adderRole, adder);", caller="admin"),
            SetupCall("collateral.grantRole(slasherRole, slasher);", caller="admin"),
            SetupCall("collateral.grantRole(slasherRole, address(pegOut));", caller="admin"),
            SetupCall("collateral.grantRole(slasherRole, address(pegIn));", caller="admin"),
            SetupCall("collateral.addPegOutCollateralTo{value: 0.6 ether}(lp0);", caller="adder"),
            SetupCall("collateral.addPegInCollateralTo{value: 0.6 ether}(lp0);", caller="adder"),
            SetupCall("collateral.addPegOutCollateralTo{value: 0.6 ether}(lp1);", caller="adder"),
            SetupCall("collateral.addPegInCollateralTo{value: 0.6 ether}(lp2);", caller="adder"),
        ],
        recipes={
            "PegOutContract.depositPegOut": Recipe(
                "PegOutContract.depositPegOut", DEPOSIT_PEG_OUT,
                params=("uint256 valueSeed", "uint256 penaltySeed"),
                feeds="PegOutContract::_pegOutQuotes",
                note="a signed peg-out quote from a registered provider, escrowed by a user",
            ),
            "PegOutContract.refundPegOut": Recipe(
                "PegOutContract.refundPegOut", REFUND_PEG_OUT,
                params=("uint256 lateSeed",),
                note="the provider proves a Bitcoin payment that passes every validation; late or on time by seed",
            ),
            "PegOutContract.refundUserPegOut": Recipe(
                "PegOutContract.refundUserPegOut", REFUND_USER_PEG_OUT,
                note="anyone settles an expired quote in the user's favour",
            ),
            "PegInContract.callForUser": Recipe(
                "PegInContract.callForUser", CALL_FOR_USER,
                params=("uint256 valueSeed",),
                feeds="PegInContract::_processedQuotes",
                note="a provider performs the call for a fresh quote, or replays a known one",
            ),
            "PegInContract.registerPegIn": Recipe(
                "PegInContract.registerPegIn", REGISTER_PEG_IN,
                params=("uint256 amountSeed", "uint256 heightSeed"),
                feeds="PegInContract::_processedQuotes",
                note="registers a known quote with the bridge mock funded for it",
            ),
        },
        members=MEMBERS,
        imports={
            "Quotes": "src/libraries/Quotes.sol",
            "SignatureValidator": "src/libraries/SignatureValidator.sol",
        },
        prelude={"halmos": HALMOS_PRELUDE},
    )
