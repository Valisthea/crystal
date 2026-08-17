"""End-to-end pipeline behaviour and the evidence-only contract."""

import pytest

from crystal.engine import research
from crystal.parsers import rust_ts, treesitter_enabled
from crystal.research.delta_anomalies import detect_delta_anomalies
from crystal.research.novelty import shape_of

VAULT = """
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }
    function donate() external payable { totalAssets += msg.value; }
    function withdraw(uint256 x) external {
        balances[msg.sender] -= x;
        totalAssets -= x;
        totalSupply -= x;
    }
}
"""

PALLET = """
#[frame_support::pallet]
pub mod pallet {
    #[pallet::storage]
    pub type TotalAssets<T: Config> = StorageValue<_, u128, ValueQuery>;
    #[pallet::storage]
    pub type TotalSupply<T: Config> = StorageValue<_, u128, ValueQuery>;
    #[pallet::storage]
    pub type Admin<T: Config> = StorageValue<_, T::AccountId, OptionQuery>;

    #[pallet::call]
    impl<T: Config> Pallet<T> {
        #[pallet::call_index(0)]
        pub fn deposit(origin: OriginFor<T>, amount: u128) -> DispatchResult {
            let who = ensure_signed(origin)?;
            TotalAssets::<T>::mutate(|t| *t += amount);
            TotalSupply::<T>::mutate(|t| *t += amount);
            Ok(())
        }
        #[pallet::call_index(1)]
        pub fn donate(origin: OriginFor<T>, amount: u128) -> DispatchResult {
            TotalAssets::<T>::mutate(|t| *t += amount);
            Ok(())
        }
        #[pallet::call_index(2)]
        pub fn set_admin(origin: OriginFor<T>, who: T::AccountId) -> DispatchResult {
            Admin::<T>::put(who);
            Ok(())
        }
    }
}
"""


@pytest.fixture(scope="module")
def solidity_result(tmp_path_factory):
    target = tmp_path_factory.mktemp("pipeline")
    (target / "Vault.sol").write_text(VAULT, encoding="utf-8")
    return research(target, use_solc=False, use_foundry=False)


def test_pipeline_produces_non_empty_signal_stack(solidity_result):
    for key in ("state_deltas", "delta_anomalies", "novel_behaviors",
                "differential_candidates", "behavior_relations",
                "research_candidates", "evidence_records"):
        assert solidity_result[key], f"{key} is empty"


def test_state_deltas_use_the_symbolic_model(solidity_result):
    deltas = solidity_result["state_deltas"]
    assert all(delta.model == "symbolic-ast" for delta in deltas)
    sequence = next(
        d for d in deltas if list(d.sequence) == ["Vault.deposit", "Vault.donate"]
    )
    assert sequence.delta["totalAssets"] == "ARG:msg.value#1 + ARG:msg.value#2"
    assert sequence.delta["totalSupply"] == "ARG:msg.value#1"


def test_asymmetry_is_detected_and_symmetric_pairs_are_not(solidity_result):
    kinds = {a.kind for a in solidity_result["delta_anomalies"]}
    assert "asset-share-asymmetry" in kinds
    symmetric = [
        a for a in solidity_result["delta_anomalies"]
        if list(a.sequence) == ["Vault.deposit", "Vault.withdraw"]
        and a.kind == "asset-share-asymmetry"
    ]
    assert not symmetric


def test_anomalies_need_both_sides_declared():
    class Delta:
        sequence = ["A.f", "A.g"]
        delta = {"totalAssets": "ARG:x"}
        confidence = 0.5

    assert detect_delta_anomalies([Delta()], None, {"totalAssets"}) == []
    assert detect_delta_anomalies(
        [Delta()], None, {"totalAssets", "totalSupply"}
    )


def test_differential_finds_symbolic_inverse(solidity_result):
    relations = {
        (tuple(c.path_a), tuple(c.path_b)): c
        for c in solidity_result["differential_candidates"]
    }
    pair = relations[(("Vault.deposit",), ("Vault.withdraw",))]
    assert pair.relation == "symbolic-inverse"
    assert pair.confidence >= 0.82
    assert all(value == "0" for value in pair.residual.values())


def test_differential_residual_exposes_partial_conservation(solidity_result):
    pair = next(
        c for c in solidity_result["differential_candidates"]
        if tuple(c.path_a) == ("Vault.donate",) and tuple(c.path_b) == ("Vault.withdraw",)
    )
    assert pair.relation in {"partial-inverse", "producer-consumer", "shared-state"}
    assert pair.residual


def test_novelty_is_structural_not_name_based(solidity_result):
    behaviors = solidity_result["novel_behaviors"]
    assert all(behavior.shape for behavior in behaviors)
    assert any(behavior.matched_pattern for behavior in behaviors)
    for behavior in behaviors:
        assert 0.0 <= behavior.novelty.overall <= 1.0


def test_shape_normalisation_ignores_symbol_names():
    assert shape_of("ARG:msg.value#1 - ARG:x#2") == shape_of("ARG:a - ARG:b")
    assert shape_of("0") == "0"


def test_finding_gate_never_confirms(solidity_result):
    gate = solidity_result["finding_gate"]
    assert gate["policy"] == "zero-false-positive-confirmed"
    assert gate["concrete_validation_is_not_confirmation"] is True
    assert gate["decisions"] == []
    report = gate["report"]
    assert set(report["human_only_gates"]) == {"economic_impact", "minimal_trace"}
    for assessment in report["assessments"]:
        assert assessment["status"] != "CONFIRMED"
        assert assessment["proof"]["economic_impact"] is False
        assert assessment["proof"]["minimal_trace"] is False


def test_quality_report_passes(solidity_result):
    quality = solidity_result["quality_report"]
    assert quality.passed
    assert not quality.violations
    assert len(quality.rules) >= 5


def test_detectors_are_evidence_only(solidity_result):
    for signal in solidity_result["detectors"]:
        assert signal.status == "RESEARCH"
        assert signal.validation_required


def test_foundry_disabled_still_generates_harnesses(solidity_result):
    statuses = {x.status for x in solidity_result["foundry_validation"]}
    assert statuses <= {"HARNESS_ONLY", "UNSUPPORTED"}
    for execution in solidity_result["foundry_validation"]:
        assert not execution.reproducible
        if execution.status == "HARNESS_ONLY":
            assert execution.harness and "CRYSTAL_STEP" in execution.harness


def test_concrete_validation_is_deterministic(solidity_result):
    first = solidity_result["concrete_validation"]
    assert all(x.status in {"NO_COUNTEREXAMPLE", "COUNTEREXAMPLE", "UNSUPPORTED"}
               for x in first)
    for result in first:
        if result.counterexamples:
            assert result.counterexamples[0].assumptions


def test_project_profile_and_parsers_are_reported(solidity_result):
    profile = solidity_result["project_profile"]
    assert profile.languages == {"solidity": 1}
    assert solidity_result["parser_backends"].startswith("solidity:")


@pytest.mark.skipif(
    not (treesitter_enabled() and rust_ts.available()),
    reason="Rust needs tree-sitter; there is no regex fallback for it",
)
def test_substrate_pallet_end_to_end(tmp_path):
    source = tmp_path / "pallets" / "vault" / "src"
    source.mkdir(parents=True)
    (source / "lib.rs").write_text(PALLET, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "v"\n[dependencies]\nframe-support = "4"\n',
        encoding="utf-8",
    )

    result = research(tmp_path, use_solc=False, use_foundry=False)
    assert result["project_profile"].languages == {"rust": 1}
    assert "substrate" in result["project_profile"].frameworks

    pallet = result["contracts"][0]
    assert pallet.kind == "pallet"
    assert pallet.name == "vault"
    extrinsics = {f.name for f in pallet.functions if f.kind == "extrinsic"}
    assert extrinsics == {"deposit", "donate", "set_admin"}

    assert result["state_deltas"]
    sequence = next(
        d for d in result["state_deltas"]
        if list(d.sequence) == ["vault.deposit", "vault.donate"]
    )
    assert sequence.delta["TotalAssets"] == "ARG:amount#1 + ARG:amount#2"
    assert sequence.delta["TotalSupply"] == "ARG:amount#1"

    assert any(a.kind == "asset-share-asymmetry" for a in result["delta_anomalies"])
    assert any(
        s.detector == "missing-access-control" and s.function == "set_admin"
        for s in result["detectors"]
    )
    assert result["evidence_records"]
