"""The seven success criteria for cross-pallet composition.

The load-bearing test is `test_format_checks_are_not_guards`: `CheckNonce` and
`CheckWeight` reject transactions constantly and guard nothing. If they classify
as guards, every pipeline reports a bypass and the real one is buried.
"""

from crystal.composition import (
    CHECKS_ONLY,
    GUARD,
    MOVES_VALUE,
    OBSERVES,
    build_composition,
)
from crystal.composition.runtime_wiring import extract_modules, profile_workspace
from crystal.detectors import run_detectors
from crystal.detectors.pipeline_guard_bypass import DETECTOR
from crystal.parsers import rust_ts
from crystal.symbolic import SymbolicEngine

TX_EXTENSION = """
pub type TxExtension = (
    frame_system::CheckNonZeroSender<Runtime>,
    frame_system::CheckSpecVersion<Runtime>,
    frame_system::CheckTxVersion<Runtime>,
    frame_system::CheckGenesis<Runtime>,
    frame_system::CheckEra<Runtime>,
    frame_system::CheckNonce<Runtime>,
    frame_system::CheckWeight<Runtime>,
    transaction_extensions::ReversibleTransactionExtension<Runtime>,
    transaction_extensions::WormholeProofRecorderExtension<Runtime>,
    pallet_transaction_payment::ChargeTransactionPayment<Runtime>,
    frame_metadata_hash_extension::CheckMetadataHash<Runtime>,
    frame_system::WeightReclaim<Runtime>,
);
"""

MODERN_RUNTIME = """
#[frame_support::runtime]
mod runtime {
    #[runtime::runtime]
    #[runtime::derive(RuntimeCall, RuntimeEvent)]
    pub struct Runtime;

    #[runtime::pallet_index(0)]
    pub type System = frame_system;

    #[runtime::pallet_index(2)]
    pub type Balances = pallet_balances;

    #[runtime::pallet_index(3)]
    pub type TransactionPayment = pallet_transaction_payment;

    #[runtime::pallet_index(11)]
    pub type ReversibleTransfers = pallet_reversible_transfers;
}
"""

LEGACY_RUNTIME = """
construct_runtime!(
    pub enum Runtime where
        Block = Block,
        NodeBlock = opaque::Block,
        UncheckedExtrinsic = UncheckedExtrinsic
    {
        System: frame_system::{Pallet, Call, Config, Storage, Event<T>},
        Balances: pallet_balances::{Pallet, Call, Storage, Event<T>},
        TransactionPayment: pallet_transaction_payment::{Pallet, Storage, Event<T>},
    }
);
"""

# Protocol-metadata checks. They reject constantly and guard nothing.
FORMAT_CHECKS = """
pub struct CheckNonce<T: Config>(pub T::Nonce);

impl<T: Config> TransactionExtension<RuntimeCall> for CheckNonce<T> {
    fn validate(&self, origin: Origin, call: &RuntimeCall) -> ValidateResult {
        let who = ensure_signed(origin.clone())?;
        let mut account = frame_system::Account::<T>::get(&who);
        if self.0 < account.nonce {
            return Err(InvalidTransaction::Stale.into());
        }
        Ok((ValidTransaction::default(), (), origin))
    }
}

pub struct CheckWeight<T: Config>(PhantomData<T>);

impl<T: Config> TransactionExtension<RuntimeCall> for CheckWeight<T> {
    fn validate(&self, origin: Origin, info: &DispatchInfo) -> ValidateResult {
        let next_weight = Self::check_block_weight(info)?;
        if next_weight.any_gt(maximum_weight) {
            return Err(InvalidTransaction::ExhaustsResources.into());
        }
        Ok((ValidTransaction::default(), (), origin))
    }
}
"""

GUARD_EXTENSION = """
pub struct ReversibleTransactionExtension<T: Config>(PhantomData<T>);

impl<T: Config> TransactionExtension<RuntimeCall> for ReversibleTransactionExtension<T> {
    fn validate(&self, origin: Origin, call: &RuntimeCall) -> ValidateResult {
        let who = ensure_signed(origin.clone())?;
        if !HighSecurityConfig::is_call_allowed(&who, call) {
            return Err(TransactionValidityError::Invalid(InvalidTransaction::Custom(1)));
        }
        Ok((ValidTransaction::default(), (), origin))
    }
}
"""

OBSERVER_EXTENSION = """
pub struct WormholeProofRecorderExtension<T: Config>(PhantomData<T>);

impl<T: Config> TransactionExtension<RuntimeCall> for WormholeProofRecorderExtension<T> {
    fn post_dispatch_details(pre: Self::Pre, result: &DispatchResult) -> Result<Weight, E> {
        let count = Self::count_transfers(call);
        Self::record_proof(count);
        Ok(Weight::zero())
    }
}
"""

PAYMENT_EXTENSION = """
pub struct ChargeTransactionPayment<T: Config>(#[codec(compact)] BalanceOf<T>);

impl<T: Config> TransactionExtension<RuntimeCall> for ChargeTransactionPayment<T> {
    fn prepare(self, val: Self::Val) -> Result<Self::Pre, TransactionValidityError> {
        let tip = self.0;
        <<T as Config>::OnChargeTransaction as OnChargeTransaction<T>>::withdraw_fee(
            who, call, info, fee_with_tip, tip,
        )
    }
}
"""

RUNTIME_CONFIG = """
impl pallet_transaction_payment::Config for Runtime {
    type OnChargeTransaction =
        FungibleAdapter<Balances, TransactionFeesCollector<Runtime>>;
    type WeightInfo = ();
}
"""

FUNGIBLE_ADAPTER = """
pub struct FungibleAdapter<F, OU>(PhantomData<(F, OU)>);

impl<F, OU> OnChargeTransaction for FungibleAdapter<F, OU> {
    fn withdraw_fee(who: &AccountId, fee: Balance, tip: Balance) -> Result<(), E> {
        F::withdraw(who, fee)
    }
}
"""

# A guard that covers the very mechanism the mover uses: the negative case.
COVERING_GUARD = GUARD_EXTENSION.replace(
    "if !HighSecurityConfig::is_call_allowed(&who, call) {",
    "let fee = charge_fee(&who);\n"
    "        if !HighSecurityConfig::is_call_allowed(&who, call) {",
)

FULL = (TX_EXTENSION, MODERN_RUNTIME, RUNTIME_CONFIG, FORMAT_CHECKS,
        GUARD_EXTENSION, OBSERVER_EXTENSION, PAYMENT_EXTENSION, FUNGIBLE_ADAPTER)


def parse_all(*sources):
    contracts, wirings, bindings = [], [], []
    for index, source in enumerate(sources):
        found, found_wirings, found_bindings = rust_ts.parse_text_detailed(
            source, f"runtime{index}.rs"
        )
        contracts.extend(found)
        wirings.extend(found_wirings)
        bindings.extend(found_bindings)
    return contracts, wirings, bindings


def compose(*sources, root="."):
    contracts, wirings, bindings = parse_all(*sources)
    return build_composition(contracts, wirings, bindings, root)


def signals(*sources):
    contracts, wirings, bindings = parse_all(*sources)
    return run_detectors(contracts, SymbolicEngine(contracts),
                         wirings=wirings, bindings=bindings)


def stage(model, name):
    return next(s for s in model.pipelines[0].stages if s.name == name)


# -- criterion 1: the pipeline is extracted with indices ---------------------

def test_pipeline_has_twelve_indexed_stages():
    model = compose(*FULL)
    pipeline = model.pipelines[0]
    assert pipeline.name == "TxExtension"
    assert len(pipeline.stages) == 12
    assert [s.index for s in pipeline.stages] == list(range(12))
    assert stage(model, "ReversibleTransactionExtension").index == 7
    assert stage(model, "ChargeTransactionPayment").index == 9


# -- criterion 2: roles ------------------------------------------------------

def test_guard_and_mover_are_classified():
    model = compose(*FULL)
    assert stage(model, "ReversibleTransactionExtension").role == GUARD
    assert stage(model, "ChargeTransactionPayment").role == MOVES_VALUE


def test_observer_is_not_a_mover():
    """`count_transfers` counts; it does not transfer."""
    model = compose(*FULL)
    assert stage(model, "WormholeProofRecorderExtension").role == OBSERVES


# -- criterion 5: format checks are NOT guards -------------------------------

def test_format_checks_are_not_guards():
    model = compose(*FULL)
    for name in ("CheckNonce", "CheckWeight"):
        role = stage(model, name)
        assert role.resolved, f"{name} should be parsed in this fixture"
        assert role.role == CHECKS_ONLY, (name, role.role, role.guard_evidence)
        assert role.metadata_checks


def test_no_crossing_is_reported_from_a_format_check():
    model = compose(*FULL)
    for crossing in model.crossings:
        assert crossing.guard.name not in {"CheckNonce", "CheckWeight"}, crossing.boundary


# -- criteria 3 and 4: resolution and the signal -----------------------------

def test_associated_type_resolves_to_the_adapter():
    model = compose(*FULL)
    assert model.associated_types["OnChargeTransaction"] == "FungibleAdapter"


def test_unit_type_binding_is_not_a_route():
    """`type WeightInfo = ()` is a real binding and a useless route."""
    model = compose(*FULL)
    payment = stage(model, "ChargeTransactionPayment")
    assert not any("()" in route for route in payment.routes)


def test_signal_is_emitted_with_expected_confidence():
    found = [s for s in signals(*FULL) if s.detector == DETECTOR]
    assert found, "no pipeline-guard-bypass signal"
    best = max(found, key=lambda s: s.confidence)
    assert best.confidence >= 0.75, best.confidence
    assert best.contract == "ChargeTransactionPayment"
    assert best.status == "RESEARCH"
    assert best.falsification
    joined = " ".join(best.evidence)
    assert "ReversibleTransactionExtension" in joined
    assert "is_call_allowed" in joined
    assert "FungibleAdapter" in joined
    assert "declared composition" in joined
    trace = " ".join(best.ordered_trace)
    assert "[7] ReversibleTransactionExtension GUARD" in trace
    assert "[9] ChargeTransactionPayment MOVES-VALUE" in trace
    assert "CheckNonce CHECKS-ONLY" in trace


def test_exactly_one_crossing_on_this_pipeline():
    model = compose(*FULL)
    assert len(model.crossings) == 1
    crossing = model.crossings[0]
    assert crossing.guard.index == 7
    assert crossing.mover.index == 9


# -- negative case: a guard that covers the mechanism ------------------------

def test_guard_covering_the_same_mechanism_emits_nothing():
    model = compose(TX_EXTENSION, MODERN_RUNTIME, RUNTIME_CONFIG, FORMAT_CHECKS,
                    COVERING_GUARD, OBSERVER_EXTENSION, PAYMENT_EXTENSION,
                    FUNGIBLE_ADAPTER)
    assert not model.crossings, [c.boundary for c in model.crossings]


def test_generic_runtime_without_a_value_stage_is_silent():
    model = compose(TX_EXTENSION, MODERN_RUNTIME, FORMAT_CHECKS, GUARD_EXTENSION)
    assert model.pipelines
    assert not model.crossings


# -- silence must be distinguishable from a clean result ---------------------

def test_unread_stages_are_named_when_nothing_crosses():
    """Regression: an incomplete scan root reported zero and said nothing.

    A stage is classified from its own body, so scanning `pallets/` without the
    runtime — or the runtime without `pallets/` — leaves stages UNRESOLVED and
    no crossing can be built from them. On the real target that produced
    `detector_signals=0` for a pipeline that does contain a live crossing,
    which reads exactly like a clean result.
    """
    model = compose(TX_EXTENSION, MODERN_RUNTIME, FORMAT_CHECKS, GUARD_EXTENSION)
    assert not model.crossings
    assert "TxExtension" in model.warning
    assert "never read" in model.warning
    assert "ChargeTransactionPayment" in model.warning


def test_complete_scan_is_not_accused_of_unread_stages():
    """The counterweight: a scan that DID cross must not carry the note."""
    model = compose(*FULL)
    assert model.crossings
    assert "never read" not in model.warning


# -- criterion 1 (topology) and 7 (warning) ----------------------------------

def test_modern_runtime_macro_is_parsed():
    modules = extract_modules_from(MODERN_RUNTIME, "lib.rs")
    aliases = {m.alias: m for m in modules}
    assert aliases["TransactionPayment"].crate == "pallet_transaction_payment"
    assert aliases["TransactionPayment"].index == 3
    assert aliases["ReversibleTransfers"].index == 11


def test_legacy_construct_runtime_is_parsed():
    modules = extract_modules_from(LEGACY_RUNTIME, "lib.rs")
    aliases = {m.alias: m.crate for m in modules}
    assert aliases["TransactionPayment"] == "pallet_transaction_payment"
    assert aliases["Balances"] == "pallet_balances"


def test_mock_runtimes_are_excluded_from_the_topology():
    """A test runtime declares its own indices and would double every pallet."""
    assert extract_modules_from(MODERN_RUNTIME, "mock.rs") == []


def test_single_pallet_scan_warns(tmp_path):
    (tmp_path / "lib.rs").write_text(PAYMENT_EXTENSION, encoding="utf-8")
    profile = profile_workspace(tmp_path, [tmp_path / "lib.rs"])
    assert profile.substrate
    assert not profile.has_runtime
    assert "Cross-module composition requires" in profile.warning()


def test_runtime_scan_does_not_warn(tmp_path):
    (tmp_path / "lib.rs").write_text(MODERN_RUNTIME, encoding="utf-8")
    profile = profile_workspace(tmp_path, [tmp_path / "lib.rs"])
    assert profile.has_runtime
    assert profile.warning() == ""


def test_declared_only_limit_is_always_stated():
    model = compose(*FULL)
    assert "declared composition" in model.limits
    assert "not evidence of absence" in model.limits


def extract_modules_from(source, filename):
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp(prefix="crystal_rt_"))
    path = directory / filename
    path.write_text(source, encoding="utf-8")
    return extract_modules([path])
