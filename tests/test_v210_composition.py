"""Signals 2 and 3 of the F5 benchmark: cross-module composition and refunds.

Signal 2 needs two modules that are each correct alone. Signal 3 needs a
settlement frame that is handed an outcome and drops it. Both shapes are
reproduced here at the size that carries the mechanism.
"""

from crystal.detectors import run_detectors
from crystal.detectors.ignored_outcome import DETECTOR as OUTCOME_DETECTOR
from crystal.detectors.pipeline_bypass import DETECTOR as PIPELINE_DETECTOR
from crystal.parsers import rust_ts
from crystal.semantics.modules import build_module_graph
from crystal.symbolic import SymbolicEngine

RUNTIME = """
pub type TxExtension = (
    frame_system::CheckNonZeroSender<Runtime>,
    frame_system::CheckSpecVersion<Runtime>,
    frame_system::CheckTxVersion<Runtime>,
    frame_system::CheckGenesis<Runtime>,
    frame_system::CheckEra<Runtime>,
    frame_system::CheckNonce<Runtime>,
    frame_system::CheckWeight<Runtime>,
    transaction_extensions::ReversibleTransactionExtension<Runtime>,
    // post-dispatch hooks execute left-to-right, so this must land first
    transaction_extensions::WormholeProofRecorderExtension<Runtime>,
    pallet_transaction_payment::ChargeTransactionPayment<Runtime>,
    frame_metadata_hash_extension::CheckMetadataHash<Runtime>,
);
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

PAYMENT_EXTENSION = """
pub struct ChargeTransactionPayment<T: Config>(#[codec(compact)] BalanceOf<T>);

impl<T: Config> TransactionExtension<RuntimeCall> for ChargeTransactionPayment<T> {
    fn prepare(self, val: Self::Val) -> Result<Self::Pre, TransactionValidityError> {
        let tip = self.0;
        <<T as Config>::OnChargeTransaction as OnChargeTransaction<T>>::withdraw_fee(
            who, call, info, fee_with_tip, tip,
        )
    }

    fn post_dispatch_details(
        pre: Self::Pre,
        info: &DispatchInfoOf<RuntimeCall>,
        post_info: &PostDispatchInfoOf<RuntimeCall>,
        len: usize,
        _result: &DispatchResult,
    ) -> Result<Weight, TransactionValidityError> {
        let actual_fee_with_tip =
            Pallet::<T>::compute_actual_fee(len as u32, info, &post_info, tip);
        T::OnChargeTransaction::correct_and_deposit_fee(
            &who, info, &post_info, actual_fee_with_tip, tip, liquidity_info,
        )?;
        Ok(Weight::zero())
    }
}
"""

# Same frame, but the outcome is actually consulted and the tip refunded.
REFUNDING_PAYMENT = PAYMENT_EXTENSION.replace(
    "        _result: &DispatchResult,\n", "        result: &DispatchResult,\n"
).replace(
    "        let actual_fee_with_tip =",
    "        if result.is_err() {\n"
    "            T::OnChargeTransaction::refund(&who, tip)?;\n"
    "            return Ok(Weight::zero());\n"
    "        }\n"
    "        let actual_fee_with_tip =",
)


def parse_all(*sources):
    contracts = []
    wirings = []
    for index, source in enumerate(sources):
        found, found_wirings = rust_ts.parse_text_detailed(source, f"m{index}.rs")
        contracts.extend(found)
        wirings.extend(found_wirings)
    return contracts, wirings


def signals(*sources):
    contracts, wirings = parse_all(*sources)
    return run_detectors(contracts, SymbolicEngine(contracts), wirings=wirings)


# -- pipeline extraction -----------------------------------------------------

def test_pipeline_members_and_order_are_extracted():
    _, wirings = parse_all(RUNTIME)
    pipeline = next(w for w in wirings if w.name == "TxExtension")
    assert pipeline.kind == "extension-pipeline"
    assert pipeline.index_of("ReversibleTransactionExtension") == 7
    assert pipeline.index_of("ChargeTransactionPayment") == 9


def test_comments_do_not_shift_pipeline_indices():
    """A comment between entries must not be counted as a stage."""
    _, wirings = parse_all(RUNTIME)
    members = next(w for w in wirings if w.name == "TxExtension").members
    assert not any(m.startswith("//") for m in members)
    assert len(members) == 11


# -- module graph ------------------------------------------------------------

def test_module_graph_classifies_guard_and_mover():
    contracts, wirings = parse_all(RUNTIME, GUARD_EXTENSION, PAYMENT_EXTENSION)
    graph = build_module_graph(contracts, wirings)
    pipeline = graph.pipelines[0]
    guard = next(s for s in pipeline.stages
                 if s.name == "ReversibleTransactionExtension")
    mover = next(s for s in pipeline.stages
                 if s.name == "ChargeTransactionPayment")
    assert guard.is_guard, guard.guard_evidence
    assert mover.moves_value, mover.value_evidence
    assert not mover.is_guard
    assert guard.index < mover.index


def test_unresolved_stages_are_marked_not_guessed():
    contracts, wirings = parse_all(RUNTIME)
    graph = build_module_graph(contracts, wirings)
    pipeline = graph.pipelines[0]
    assert pipeline.stages
    assert all(not stage.resolved for stage in pipeline.stages)
    assert not pipeline.guards and not pipeline.movers


# -- Signal 2 ----------------------------------------------------------------

def test_signal_2_pipeline_bypass():
    found = [s for s in signals(RUNTIME, GUARD_EXTENSION, PAYMENT_EXTENSION)
             if s.detector == PIPELINE_DETECTOR]
    assert found, "no pipeline-bypass signal"
    best = max(found, key=lambda s: s.confidence)
    assert best.contract == "ChargeTransactionPayment"
    assert best.status == "RESEARCH"
    joined = " ".join(best.evidence)
    assert "ReversibleTransactionExtension" in joined
    assert "is_call_allowed" in joined or "highsecurity" in joined.lower()
    trace = " ".join(best.ordered_trace)
    assert "[7] ReversibleTransactionExtension GUARD" in trace
    assert "[9] ChargeTransactionPayment MOVES-VALUE" in trace


def test_no_bypass_signal_without_a_guard_stage():
    found = [s for s in signals(RUNTIME, PAYMENT_EXTENSION)
             if s.detector == PIPELINE_DETECTOR]
    assert not found, "a pipeline with no guard stage must not report a bypass"


def test_no_bypass_signal_without_a_value_stage():
    found = [s for s in signals(RUNTIME, GUARD_EXTENSION)
             if s.detector == PIPELINE_DETECTOR]
    assert not found


# -- Signal 3 ----------------------------------------------------------------

def test_signal_3_ignored_outcome():
    found = [s for s in signals(PAYMENT_EXTENSION) if s.detector == OUTCOME_DETECTOR]
    assert found, "no ignored-outcome signal"
    best = max(found, key=lambda s: s.confidence)
    assert best.function == "post_dispatch_details"
    assert best.confidence >= 0.6
    joined = " ".join(best.evidence)
    assert "_result" in joined
    assert "correct_and_deposit_fee" in joined
    assert best.status == "RESEARCH"
    assert best.falsification


def test_consulting_the_outcome_clears_the_signal():
    """React to the code reading the result, not to the function's name."""
    found = [s for s in signals(REFUNDING_PAYMENT) if s.detector == OUTCOME_DETECTOR]
    assert not found, "reading the outcome and refunding must clear the signal"


def test_function_without_an_outcome_parameter_is_not_flagged():
    source = """
    pub struct Plain;
    impl Plain {
        pub fn settle(&self, who: &AccountId, amount: u64) -> Result<(), E> {
            transfer(who, amount)
        }
    }
    """
    found = [s for s in signals(source) if s.detector == OUTCOME_DETECTOR]
    assert not found, "a frame never handed the outcome is not ignoring it"


def test_signals_stay_research_only():
    for item in signals(RUNTIME, GUARD_EXTENSION, PAYMENT_EXTENSION):
        assert item.status == "RESEARCH"
        assert item.validation_required
        assert item.falsification
