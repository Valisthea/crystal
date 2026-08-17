import pytest

from crystal.parsers import move_ts, parse_project, parser_report, solidity_regex
from crystal.parsers import rust_ts, solidity_ts, vyper_ts
from crystal.parsers.base import detect_language, split_arguments, strip_comments

needs_solidity_ts = pytest.mark.skipif(
    not solidity_ts.available(), reason="tree-sitter-solidity not installed"
)
needs_rust_ts = pytest.mark.skipif(
    not rust_ts.available(), reason="tree-sitter-rust not installed"
)

SOLIDITY = """
pragma solidity ^0.8.20;
error Unauthorized(address who);

interface IOracle { function latestRoundData() external view returns (uint256); }

contract Base { uint256 internal _seed; }

contract Vault is Base {
    using SafeMath for uint256;
    event Deposited(address indexed who, uint256 amount);
    enum Status { Open, Closed }
    struct Position { uint256 size; }

    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public totalAssets;
    uint256 private constant CAP = 10 ether;
    address public immutable owner;

    modifier onlyOwner() { require(msg.sender == owner); _; }

    constructor(address _owner) { owner = _owner; }
    receive() external payable {}

    function deposit(uint256 assets, address to) external payable onlyOwner returns (uint256 shares) {
        require(assets > 0, "zero");
        if (totalAssets == 0) { shares = assets; } else { shares = assets * 2; }
        totalAssets += assets;
        allowance[msg.sender][to] = shares;
        (bool ok, ) = to.call{value: msg.value}("");
        require(ok);
        return shares;
    }

    function raw() external { assembly { sstore(0, 1) } }
}
"""


def test_language_detection():
    assert detect_language("a/b/Vault.sol") == "solidity"
    assert detect_language("pallets/src/lib.rs") == "rust"
    assert detect_language("sources/vault.move") == "move"
    assert detect_language("contracts/vault.vy") == "vyper"
    assert detect_language("README.md") is None


def test_parser_report_shape():
    report = parser_report()
    for language in ("solidity", "rust", "move", "vyper"):
        assert "backend" in report[language]
        assert "status" in report[language]
    assert isinstance(report["treesitter_enabled"], bool)


def test_strip_comments_preserves_length_and_strings():
    source = 'a; // note\nb; /* x */ c; d = "//not a comment";'
    stripped = strip_comments(source)
    assert len(stripped) == len(source)
    assert "note" not in stripped
    assert "//not a comment" in stripped


def test_split_arguments_respects_nesting():
    assert split_arguments("uint256 a, mapping(address => uint256) b, bool c") == [
        "uint256 a", "mapping(address => uint256) b", "bool c",
    ]


def test_regex_parser_still_works_and_builds_ir():
    contracts = solidity_regex.parse_text(SOLIDITY, "Vault.sol")
    vault = next(c for c in contracts if c.name == "Vault")
    assert {f.name for f in vault.functions} >= {"deposit", "raw"}
    deposit = next(f for f in vault.functions if f.name == "deposit")
    assert deposit.ir is not None and deposit.ir.statements
    assert [p.type_name for p in deposit.params] == ["uint256", "address"]


@needs_solidity_ts
def test_treesitter_extracts_full_structure():
    contracts = solidity_ts.parse_text(SOLIDITY, "Vault.sol")
    names = {c.name: c for c in contracts}
    assert "IOracle" in names and names["IOracle"].kind == "interface"
    vault = names["Vault"]

    assert vault.bases == ["Base"]
    assert [e.name for e in vault.events] == ["Deposited"]
    assert {t.name for t in vault.types} == {"Status", "Position"}
    assert [m.name for m in vault.modifier_definitions] == ["onlyOwner"]
    assert vault.using_for

    allowance = next(v for v in vault.state_vars if v.name == "allowance")
    assert allowance.visibility == "public"
    assert allowance.key_types == ["address", "address"]
    assert allowance.value_type == "uint256"
    assert next(v for v in vault.state_vars if v.name == "CAP").constant
    assert next(v for v in vault.state_vars if v.name == "owner").immutable

    kinds = {f.kind for f in vault.functions}
    assert {"constructor", "receive", "function"} <= kinds

    deposit = next(f for f in vault.functions if f.name == "deposit")
    assert deposit.signature == "deposit(uint256,address)"
    assert deposit.modifiers == ["onlyOwner"]
    assert deposit.payable
    assert "<low-level-call>" in deposit.calls
    assert {"totalAssets", "allowance"} <= deposit.writes

    raw = next(f for f in vault.functions if f.name == "raw")
    assert raw.ir.has_assembly
    assert raw.ir.unsupported


@needs_solidity_ts
def test_treesitter_preserves_statement_order():
    vault = next(
        c for c in solidity_ts.parse_text(SOLIDITY, "Vault.sol") if c.name == "Vault"
    )
    deposit = next(f for f in vault.functions if f.name == "deposit")
    lines = [s.line for s in deposit.ir.statements]
    assert lines == sorted(lines)
    external = deposit.ir.external_calls()
    assert external and external[0].value_attached


@needs_rust_ts
def test_rust_substrate_pallet():
    source = """
    #[frame_support::pallet]
    pub mod pallet {
        #[pallet::storage]
        pub type TotalSupply<T: Config> = StorageValue<_, u128, ValueQuery>;
        #[pallet::storage]
        pub type Balances<T: Config> = StorageMap<_, Blake2_128Concat, T::AccountId, u128, ValueQuery>;
        #[pallet::event]
        pub enum Event<T: Config> { Deposited(T::AccountId, u128) }
        #[pallet::error]
        pub enum Error<T> { Bad }
        #[pallet::call]
        impl<T: Config> Pallet<T> {
            #[pallet::call_index(0)]
            pub fn deposit(origin: OriginFor<T>, amount: u128) -> DispatchResult {
                let who = ensure_signed(origin)?;
                ensure!(amount > 0, Error::<T>::Bad);
                Balances::<T>::mutate(&who, |b| *b += amount);
                TotalSupply::<T>::mutate(|t| *t += amount);
                Ok(())
            }
        }
    }
    """
    contracts = rust_ts.parse_text(source, "pallets/vault/src/lib.rs")
    pallet = contracts[0]
    assert pallet.kind == "pallet"
    assert pallet.name == "vault"
    assert {v.name for v in pallet.state_vars} == {"TotalSupply", "Balances"}
    balances = next(v for v in pallet.state_vars if v.name == "Balances")
    assert balances.key_types == ["T::AccountId"]
    assert [e.name for e in pallet.events] == ["Deposited"]
    assert [e.name for e in pallet.errors] == ["Bad"]

    deposit = next(f for f in pallet.functions if f.name == "deposit")
    assert deposit.kind == "extrinsic"
    assert deposit.is_entry_point
    assert deposit.writes == {"Balances", "TotalSupply"}


@needs_rust_ts
def test_rust_plain_struct_impl():
    source = """
    struct Counter { count: u64 }
    impl Counter {
        pub fn increment(&mut self, by: u64) { self.count += by; }
        pub fn read(&self) -> u64 { self.count }
    }
    """
    counter = rust_ts.parse_text(source, "counter.rs")[0]
    assert counter.kind == "struct"
    assert {v.name for v in counter.state_vars} == {"count"}
    increment = next(f for f in counter.functions if f.name == "increment")
    assert increment.writes == {"count"}


def test_move_module():
    source = """
    module 0xCAFE::vault {
        struct Pool has key { total: u64, shares: u64 }
        public entry fun deposit(account: &signer, amount: u64) acquires Pool {
            assert!(amount > 0, 1);
            let pool = borrow_global_mut<Pool>(@0xCAFE);
            pool.total = pool.total + amount;
        }
    }
    """
    module = move_ts.parse_text(source, "vault.move")[0]
    assert module.kind == "module"
    assert {v.name for v in module.state_vars} == {"total", "shares"}
    deposit = next(f for f in module.functions if f.name == "deposit")
    assert deposit.visibility == "external"
    assert "total" in deposit.writes


def test_vyper_contract():
    source = """
owner: public(address)
balances: public(HashMap[address, uint256])
totalAssets: uint256

@external
@payable
def deposit():
    self.balances[msg.sender] += msg.value
    self.totalAssets += msg.value

@external
def withdraw(amount: uint256):
    assert self.balances[msg.sender] >= amount
    raw_call(msg.sender, b"", value=amount)
    self.balances[msg.sender] -= amount
"""
    contract = vyper_ts.parse_text(source, "vault.vy")[0]
    assert {v.name for v in contract.state_vars} == {"owner", "balances", "totalAssets"}
    balances = next(v for v in contract.state_vars if v.name == "balances")
    assert balances.key_types == ["address"]
    withdraw = next(f for f in contract.functions if f.name == "withdraw")
    assert withdraw.visibility == "external"
    assert "balances" in withdraw.writes
    assert withdraw.ir.external_calls()


def test_parse_project_routes_by_language(tmp_path):
    (tmp_path / "A.sol").write_text("contract A { uint256 public x; }")
    (tmp_path / "b.vy").write_text("x: public(uint256)\n")
    (tmp_path / "c.move").write_text(
        "module 0x1::m { struct S has key { a: u64 } }"
    )
    from crystal.discovery import discover

    parsed = parse_project(discover(tmp_path))
    languages = {c.language for c in parsed.contracts}
    assert "solidity" in languages
    assert "vyper" in languages
    assert "move" in languages
