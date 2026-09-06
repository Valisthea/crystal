"""Regression tests for the F5 benchmark.

F5 (`pallet-transaction-payment`, Quantus) is a confirmed Critical that Crystal
v1 missed entirely: an uncapped transaction tip drains an account past the
guardian. The pallet is reduced here to the shape that carries the mechanism, so
the tests stay readable and run without the target checked out.
"""

from crystal.detectors import run_detectors
from crystal.detectors.unbounded_input import DETECTOR
from crystal.parsers import rust_ts
from crystal.symbolic import SymbolicEngine
import pytest

# Rust is the one front-end with no regex fallback: without the tree-sitter
# grammar `parser_report()` says `backend: unavailable`, and these fixtures
# parse to nothing. Skipping is the honest outcome — a Substrate assertion
# against zero contracts would pass while testing nothing.
pytestmark = pytest.mark.skipif(
    not rust_ts.available(),
    reason="needs the tree-sitter Rust grammar; Rust has no regex fallback",
)


pytest_plugins = ()

CHARGE_EXTENSION = """
pub struct ChargeTransactionPayment<T: Config>(#[codec(compact)] BalanceOf<T>);

impl<T: Config> ChargeTransactionPayment<T> {
    pub fn tip(&self) -> BalanceOf<T> {
        self.0
    }

    fn withdraw_fee(
        &self,
        who: &T::AccountId,
        call: &T::RuntimeCall,
        info: &DispatchInfoOf<T::RuntimeCall>,
        fee_with_tip: BalanceOf<T>,
    ) -> Result<(BalanceOf<T>, LiquidityInfo), TransactionValidityError> {
        let tip = self.0;
        <<T as Config>::OnChargeTransaction as OnChargeTransaction<T>>::withdraw_fee(
            who, call, info, fee_with_tip, tip,
        )
        .map(|liquidity_info| (fee_with_tip, liquidity_info))
    }

    fn can_withdraw_fee(
        &self,
        who: &T::AccountId,
        call: &T::RuntimeCall,
        info: &DispatchInfoOf<T::RuntimeCall>,
        len: usize,
    ) -> Result<BalanceOf<T>, TransactionValidityError> {
        let tip = self.0;
        let fee_with_tip = Pallet::<T>::compute_fee(len as u32, info, tip);
        <<T as Config>::OnChargeTransaction as OnChargeTransaction<T>>::can_withdraw_fee(
            who, call, info, fee_with_tip, tip,
        )?;
        Ok(fee_with_tip)
    }
}

impl<T: Config> TransactionExtension<T::RuntimeCall> for ChargeTransactionPayment<T> {
    fn validate(&self, origin: Origin, call: &T::RuntimeCall) -> ValidateResult {
        let fee_with_tip = self.can_withdraw_fee(&who, call, info, len)?;
        Ok(fee_with_tip)
    }
}
"""

BOUNDED_EXTENSION = CHARGE_EXTENSION.replace(
    "let tip = self.0;\n        let fee_with_tip",
    "let tip = self.0;\n        ensure!(tip <= T::MaxTip::get(), Error::<T>::TipTooLarge);\n        let fee_with_tip",
)

TEST_FIXTURE = """
#[cfg(test)]
mod tests {
    pub struct ExtBuilder {
        balance_factor: u64,
    }

    impl ExtBuilder {
        pub fn balance_factor(mut self, factor: u64) -> Self {
            self.balance_factor = factor;
            self
        }
    }
}
"""


def parse(source, name="lib.rs"):
    return rust_ts.parse_text(source, name)


def signals(source, name="lib.rs"):
    contracts = parse(source, name)
    return run_detectors(contracts, SymbolicEngine(contracts))


def test_tuple_struct_field_is_extracted():
    contract = next(c for c in parse(CHARGE_EXTENSION)
                    if c.name == "ChargeTransactionPayment")
    assert [v.name for v in contract.state_vars] == ["0"]
    assert "BalanceOf" in contract.state_vars[0].type_name


def test_transaction_extension_marks_fields_user_decoded():
    contract = next(c for c in parse(CHARGE_EXTENSION)
                    if c.name == "ChargeTransactionPayment")
    assert contract.user_decoded
    assert any("TransactionExtension" in trait for trait in contract.traits)


def test_decoded_field_resolves_to_an_attacker_symbol():
    contracts = parse(CHARGE_EXTENSION)
    engine = SymbolicEngine(contracts)
    contract = next(c for c in contracts if c.name == "ChargeTransactionPayment")
    function = next(f for f in contract.functions if f.name == "withdraw_fee")
    records = engine.call_records(function)
    withdraw = next(r for r in records if r.call.callee == "withdraw_fee")
    rendered = [argument.render() for argument in withdraw.arguments]
    assert "ARG:self.0" in rendered, rendered


def test_combinator_chain_does_not_hide_the_operation():
    """`withdraw_fee(..).map(..)` must report the withdrawal, not the map."""
    contracts = parse(CHARGE_EXTENSION)
    engine = SymbolicEngine(contracts)
    contract = next(c for c in contracts if c.name == "ChargeTransactionPayment")
    function = next(f for f in contract.functions if f.name == "withdraw_fee")
    callees = {record.call.callee for record in engine.call_records(function)}
    assert "withdraw_fee" in callees
    assert "map" not in callees


def test_try_operator_does_not_hide_the_operation():
    contracts = parse(CHARGE_EXTENSION)
    engine = SymbolicEngine(contracts)
    contract = next(c for c in contracts if c.name == "ChargeTransactionPayment")
    function = next(f for f in contract.functions if f.name == "can_withdraw_fee")
    callees = {record.call.callee for record in engine.call_records(function)}
    assert "can_withdraw_fee" in callees


def test_cast_does_not_truncate_the_argument_list():
    """`compute_fee(len as u32, info, tip)` keeps all three arguments."""
    contracts = parse(CHARGE_EXTENSION)
    engine = SymbolicEngine(contracts)
    contract = next(c for c in contracts if c.name == "ChargeTransactionPayment")
    function = next(f for f in contract.functions if f.name == "can_withdraw_fee")
    compute = next(r for r in engine.call_records(function)
                   if r.call.callee == "compute_fee")
    assert len(compute.arguments) == 3
    assert any("ARG:self.0" in a.render() for a in compute.arguments)


def test_f5_signal_is_produced():
    """The success criterion: Signal 1 as an evidence-grade research record."""
    found = [s for s in signals(CHARGE_EXTENSION) if s.detector == DETECTOR]
    assert found, "no unbounded-input signal on the F5 shape"
    withdraw = [s for s in found if s.function in {"withdraw_fee", "can_withdraw_fee"}]
    assert withdraw
    best = max(withdraw, key=lambda s: s.confidence)
    assert best.confidence >= 0.6
    assert best.status == "RESEARCH"
    assert best.validation_required
    joined = " ".join(best.evidence)
    assert "ARG:self.0" in joined
    assert "decoded from the transaction" in joined
    assert best.falsification


def test_an_observed_cap_suppresses_the_signal():
    """The detector must react to the guard, not to the function name."""
    unbounded = [s for s in signals(CHARGE_EXTENSION) if s.detector == DETECTOR]
    bounded = [s for s in signals(BOUNDED_EXTENSION) if s.detector == DETECTOR]
    unbounded_lines = {s.line for s in unbounded if s.function == "can_withdraw_fee"}
    bounded_lines = {s.line for s in bounded if s.function == "can_withdraw_fee"}
    assert unbounded_lines
    assert not bounded_lines, "an explicit `ensure!(tip <= Max)` must clear the signal"


def test_test_fixtures_are_classified_and_excluded():
    contracts = parse(TEST_FIXTURE)
    builder = next((c for c in contracts if c.name == "ExtBuilder"), None)
    if builder is not None:
        assert builder.is_test
    assert not run_detectors(contracts, SymbolicEngine(contracts))


def test_test_file_stem_marks_every_contract():
    contracts = parse(TEST_FIXTURE.replace("#[cfg(test)]\n", ""), name="tests.rs")
    assert contracts
    assert all(c.is_test for c in contracts)
    assert all(f.is_test for c in contracts for f in c.functions)


def test_production_adapter_is_not_marked_test():
    source = """
    pub struct FungibleAdapter<F, OU>(PhantomData<(F, OU)>);

    impl<F, OU> OnChargeTransaction for FungibleAdapter<F, OU> {
        #[cfg(feature = "runtime-benchmarks")]
        fn bench_only(&self) -> u32 { 1 }

        fn withdraw_fee(&self, who: &AccountId, fee: Balance) -> Result<(), E> {
            F::withdraw(who, fee)
        }
    }
    """
    contract = next(c for c in parse(source, "payment.rs")
                    if c.name == "FungibleAdapter")
    assert not contract.is_test, "a benchmark-gated method must not mark the type"


def test_plain_transfer_is_not_reported():
    """A single caller amount debited from the caller is ordinary, not a signal."""
    source = """
    pub struct Token { balances: u64 }

    impl Token {
        pub fn send(&mut self, to: AccountId, amount: u64) -> Result<(), E> {
            transfer(to, amount)
        }
    }
    """
    found = [s for s in signals(source, "token.rs") if s.detector == DETECTOR]
    assert not found
