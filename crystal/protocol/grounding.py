"""Relations read out of the IR, which is what a protocol claim has to rest on.

Every generator in this package used to answer "is this a price function?" with
`"price" in name.lower()`. On a real multi-contract target that charged 27
invariants for a substring: interface declarations with no body to check, and
setters that write a threshold and read no feed at all. A name is what somebody
called a thing. It is not evidence about what the code does.

The predicates here answer the same questions from the statement IR instead.
Where a name still appears it is a *declared* one — a method on an external
interface, or an ERC signature — never a guess about what a local identifier
means. That distinction is the whole point: `latestRoundData` is a promise
Chainlink makes in an ABI, `getPriceThing` is a developer's spelling.

Everything returned carries the line it was observed on, so a reader can go
look. A predicate that cannot say where it looked does not belong here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..ir import EXTERNAL_CALL_KINDS, VALUE_TRANSFER

# Methods that return a price on a declared external interface. Split because
# a spot source can be moved inside one transaction and a feed cannot, which
# changes what the resulting invariant is worth.
SPOT_PRICE_METHODS = frozenset({
    "getreserves", "slot0", "getamountsout", "getamountsin",
    "getvirtualprice", "price0cumulativelast", "price1cumulativelast",
    "getspotprice", "observe", "price_oracle", "get_dy",
})
FEED_PRICE_METHODS = frozenset({
    "latestrounddata", "latestanswer", "getprice", "getlatestprice",
    "getassetprice", "getusdprice", "getusdprices", "peek", "read", "consult",
    "getrate", "exchangerate", "getrounddata",
})

# Freshness is observable as a comparison against one of these, anywhere on the
# path. Names again — but these are the fields Chainlink's ABI returns.
FRESHNESS_TOKENS = (
    "updatedat", "answeredinround", "roundid", "staleness", "heartbeat",
    "timestamp", "deviation", "twap", "maxage", "freshness", "block.number",
)

INCREASING = {"+="}
DECREASING = {"-="}

# Scaling is multiplicative. Addition accumulates, it does not take a cut.
_MULTIPLICATIVE = re.compile(r"[*/]")
# A trailing `//` comment is part of the source slice and contains slashes.
_LINE_COMMENT = re.compile(r"//.*", re.MULTILINE)
# Numeric by declaration. A `bool` or an `address` scales nothing, whatever
# arithmetic happens to sit near it in the same statement.
_NUMERIC_TYPE = re.compile(r"^u?int\d*$")

# Published library entry points that scale one value by a ratio.
MULDIV_METHODS = frozenset({
    "muldiv", "muldivdown", "muldivup", "muldivroundingup",
    "mulwad", "divwad", "mulwadup", "fullmuldiv",
})

# Names introduced on the left of a declaration: `uint256 expectedBuyAmount =`
# and the entries of a tuple destructuring.
_DECLARED = re.compile(r"[A-Za-z_]\w*")

# A numeral in any of the spellings the front-ends hand back.
_NUMERAL = re.compile(r"^[+-]?(0[xX][0-9a-fA-F_]+|[0-9][0-9_]*)$")


def _carries_quantity(value) -> bool:
    """Whether a written amount is a quantity rather than a fixed step.

    Deliberately not `value.kind == "number_literal"`. That is the tree-sitter
    spelling; the regex front-end says `expression` for `nonce += 1` and
    `literal` for `nonce++`, so a kind check passes on one parser and silently
    lets every counter through on the other — the exact shape of the fallback
    bug that went four builds unnoticed. What both agree on is the source
    slice and the identifiers in it.

    A value Crystal could not resolve at all is not counted. Claiming a ledger
    relation from a write it cannot read would be the guess this package is
    being rebuilt to stop making.
    """
    if value is None:
        return False
    if value.identifiers:
        return True
    return not _NUMERAL.match((value.text or "").strip())


@dataclass(frozen=True)
class StateWrite:
    """One observed write to a contract's own state, with its direction."""

    variable: str
    function: str
    operator: str
    line: int
    # False when the written amount is a fixed step. `nonce++` steps;
    # `totalAssets += amount` carries a quantity. Telling them apart is what
    # separates a ledger from two counters that happen to move together.
    by_quantity: bool = True

    @property
    def direction(self) -> str:
        if self.operator in INCREASING:
            return "up"
        if self.operator in DECREASING:
            return "down"
        return "set"


@dataclass(frozen=True)
class PriceRead:
    """An external call returning a price, and what happens to it afterwards."""

    contract: str
    function: str
    callee: str
    receiver: str
    source: str          # "spot" | "feed"
    line: int
    effect: str          # "state" | "return" | "none"
    freshness_checked: bool


@dataclass(frozen=True)
class ScaledAmount:
    """A state variable multiplying or dividing a value the caller influences.

    This is the shape a fee has, and also a margin, a rate and a discount. The
    shape is what is observable; which of those it is called is not. A protocol
    that takes 30 basis points and one that keeps a 30 basis point margin are
    the same statement about value, and the same thing can go wrong in both.

    Multiplicative on purpose. A first pass accepted any binary expression and
    reported `_cumulativeRevenueUSD + amountUSD_` — an accumulator, where
    nothing is being scaled and no remainder can go missing.
    """

    contract: str
    function: str
    scalar: str          # the state variable doing the scaling
    amount: str          # the non-state value being scaled
    line: int
    text: str
    reaches_effect: bool  # the result reaches state, a transfer, or the return


def has_body(function) -> bool:
    """False for an interface method, an abstract declaration, an unparsed body.

    A declaration cannot be checked for a freshness guard or an ordering, so a
    claim about one is a claim about nothing. This is the single predicate that
    removed most of the noise.
    """
    return function.ir is not None and bool(function.ir.statements)


def analysable_contracts(contracts):
    """Contracts with implementations. Interfaces declare, they do not act."""
    return [c for c in contracts if c.kind not in {"interface"}]


def _state_names(contract) -> set[str]:
    return {variable.name for variable in contract.state_vars}


def configurable_state(contract) -> set[str]:
    """State a deployment or an operator can set. Not the unit denominators.

    `MAX_BASIS_POINTS` is `constant`: it is 10000 in every deployment that will
    ever exist, and an invariant about it is an invariant about the number ten
    thousand. `MARGIN_DIFFERENCE_IN_BASIS_POINTS` is `immutable`, set from a
    constructor argument — the value an operator picks and can pick wrong.

    That distinction is declared in the source, not inferred from spelling,
    and it is the same one Lido's own fuzz harness draws: it varies the margin,
    the tolerance and the improvement cap on every sequence, and never varies
    the basis-point denominator.
    """
    return {
        variable.name for variable in contract.state_vars
        if not variable.constant
        and _NUMERIC_TYPE.match(variable.type_name.strip())
    }


def flowing_values(function) -> set[str]:
    """Identifiers carrying a value through this function: parameters and locals.

    The scaled side of a fee has to be one of these. Without the restriction,
    `block.timestamp / ONE_DAY` reads as a state variable scaling `block`, and
    `Math.mulDiv(...)` as one scaling `Math` — the root of a member access and
    the name of a library, neither of which is an amount.
    """
    names = {parameter.name for parameter in function.params if parameter.name}
    if function.ir is None:
        return names
    for statement in function.ir.walk():
        if statement.kind != "var_decl":
            continue
        text = statement.text or ""
        head = text.split("=", 1)[0] if "=" in text else text
        # The declared name is the last identifier of each comma-separated
        # slot; everything before it is the type. Taking them all made
        # `GPv2Order.Data memory orderData` contribute `Data`, which then read
        # as an amount being scaled.
        for slot in head.split(","):
            found = _DECLARED.findall(slot)
            if found:
                names.add(found[-1])
    return names


def state_writes(contract) -> list[StateWrite]:
    """Every write to the contract's own state, in source order, with direction."""
    names = _state_names(contract)
    out: list[StateWrite] = []
    for function in contract.functions:
        if not has_body(function):
            continue
        for statement in function.ir.walk():
            if statement.kind != "assign" or statement.target is None:
                continue
            base = statement.target.base
            if base in names:
                out.append(StateWrite(
                    base, function.name, statement.operator or "=",
                    statement.line, _carries_quantity(statement.value),
                ))
    return out


def quantity_variables(contract) -> set[str]:
    """State variables written by something other than a literal, at least once.

    A variable only ever stepped by a constant is a counter. It can co-move
    with a total on one path and mean nothing by it, which is how `nonce`
    arrived in a conservation relation with `totalAssets`.
    """
    return {w.variable for w in state_writes(contract) if w.by_quantity}


def monotonic_variables(contract) -> dict[str, list[StateWrite]]:
    """State variables every observed write to which increases them.

    Monotonicity earned this way is a property of the writes, not of the word
    `nonce`. A variable assigned with a bare `=` anywhere is excluded even
    where it is in fact monotone: Crystal cannot see it from the operator, so
    it does not claim it.
    """
    grouped: dict[str, list[StateWrite]] = {}
    for write in state_writes(contract):
        grouped.setdefault(write.variable, []).append(write)
    return {
        name: writes for name, writes in grouped.items()
        if writes and all(write.direction == "up" for write in writes)
    }


def co_movements(contract) -> list[tuple[str, str, list[StateWrite]]]:
    """Pairs of state variables written in the same direction by the same function.

    Two totals that always move together are a ledger. That is observable, and
    it is the reason the invariant holds — not the fact that one is spelled
    `totalAssets` and the other `totalSupply`.

    Two conditions. The pair must never move in opposite directions inside one
    function, and both sides must carry a quantity somewhere — a variable only
    ever stepped by a literal is a counter, and `nonce` moving with a total in
    `deposit` is not a ledger.

    Deliberately *not* a condition: that every function writing one writes the
    other. A first attempt required it and dropped the pair as soon as some
    path moved one alone — which is the donation that inflates a share price,
    the case the relation exists to expose. `delta_anomalies` reads these pairs
    to raise `asset-share-asymmetry`; a rule that hides the asymmetric path
    hides the finding.

    Mappings are excluded. `balances[msg.sender] += amount` moves one holder's
    entry, and pairing that with a global total states sum-of-balances against
    the total — a real invariant, and one `derive_properties` already refuses
    because it needs an enumerable holder set. Raising it here under a name
    that suggests two scalars would be the approximation that refusal exists
    to avoid.
    """
    scalars = {
        variable.name for variable in contract.state_vars
        if not variable.is_mapping
    }
    quantities = quantity_variables(contract) & scalars
    per_function: dict[str, list[StateWrite]] = {}
    for write in state_writes(contract):
        per_function.setdefault(write.function, []).append(write)

    agreeing: dict[tuple[str, str], list[StateWrite]] = {}
    conflicting: set[tuple[str, str]] = set()
    for writes in per_function.values():
        for i, left in enumerate(writes):
            for right in writes[i + 1:]:
                if left.variable == right.variable:
                    continue
                pair = tuple(sorted((left.variable, right.variable)))
                if left.direction == right.direction and left.direction != "set":
                    agreeing.setdefault(pair, []).extend((left, right))
                else:
                    conflicting.add(pair)

    return [
        (pair[0], pair[1], writes)
        for pair, writes in sorted(agreeing.items())
        if pair not in conflicting and quantities.issuperset(pair)
    ]


def _freshness_checked(function) -> bool:
    for statement in function.ir.walk():
        if statement.kind not in {"require", "if", "revert"}:
            continue
        text = (statement.text or "").lower()
        if any(token in text for token in FRESHNESS_TOKENS):
            return True
    return False


def price_reads(contract) -> list[PriceRead]:
    """External calls that return a price, and whether the value goes anywhere.

    A read whose result reaches neither state nor a return value is inert: the
    call happened, nothing downstream can depend on it. Reporting one is how
    the old generator produced invariants for `IOracleRouter.getUsdPrices`, a
    signature with no body.
    """
    out: list[PriceRead] = []
    for function in contract.functions:
        if not has_body(function):
            continue
        fresh = _freshness_checked(function)
        writes = bool(function.writes)
        returns = bool(function.returns)
        for statement in function.ir.walk():
            call = statement.call
            if call is None or call.kind not in EXTERNAL_CALL_KINDS:
                continue
            callee = (call.callee or "").rsplit(".", 1)[-1].lower()
            if callee in SPOT_PRICE_METHODS:
                source = "spot"
            elif callee in FEED_PRICE_METHODS:
                source = "feed"
            else:
                continue
            effect = "state" if writes else ("return" if returns else "none")
            out.append(PriceRead(
                contract.name, function.name, call.callee or "",
                call.receiver or "", source, call.line, effect, fresh,
            ))
    return out


def scaled_amounts(contract) -> list[ScaledAmount]:
    """State variables that multiply or divide a value the caller influences.

    Read `amount * feeBps / 10000` and the relation is there in the expression:
    a state variable, something that is not one, and multiplicative arithmetic
    between them. What makes it worth an invariant is the last clause — the
    result has to reach state, a transfer or the return value, or nobody is
    paying anything.

    The scaled side is anything that is not the contract's own state, not
    specifically a parameter. Lido's margin is applied as
    `(expectedBuyAmount * MARGIN_DIFFERENCE_IN_BASIS_POINTS) / MAX_BASIS_POINTS`,
    where the amount is a local holding the result of an earlier call. Demanding
    a parameter by name missed it, which is the whole formula the protocol's own
    fuzz model reimplements by hand.
    """
    names = configurable_state(contract)
    out: list[ScaledAmount] = []
    for function in contract.functions:
        if not has_body(function):
            continue
        reaches_effect = (
            bool(function.writes)
            or bool(function.returns)
            or any(call.kind == VALUE_TRANSFER for call in function.ir.calls())
        )
        carriers = flowing_values(function)
        seen: set[tuple[str, str]] = set()
        for statement in function.ir.walk():
            for scalar, amount, text in _scalings(statement, names, carriers):
                # The same scaling applied twice in one function is one
                # relation. Reporting it per occurrence inflates the count
                # without telling a reader anything new.
                if (scalar, amount) in seen:
                    continue
                seen.add((scalar, amount))
                out.append(ScaledAmount(
                    contract.name, function.name, scalar, amount,
                    statement.line, text, reaches_effect,
                ))
    return out


def _scalings(statement, names, carriers):
    """Every (state scalar, scaled value) pair visible in one statement."""
    found = []

    value = statement.value
    if value is not None and _MULTIPLICATIVE.search(
        _LINE_COMMENT.sub("", value.text or "")
    ):
        found.extend(_pair(value.identifiers, names, carriers,
                           (value.text or "").strip()))

    # `Math.mulDiv(amount, numerator, denominator)` is the same relation
    # written as a call. The name is a published library entry point, not a
    # guess about a local: OpenZeppelin and Uniswap both export it, and a
    # protocol that avoids `*` for overflow reasons writes its scalings here.
    call = statement.call
    if call is not None and (call.callee or "").rsplit(".", 1)[-1].lower() in MULDIV_METHODS:
        identifiers = tuple(
            name for argument in call.arguments for name in argument.identifiers
        )
        found.extend(_pair(identifiers, names, carriers, (call.text or "").strip()))
    elif value is not None and any(
        name.lower() in MULDIV_METHODS for name in value.identifiers
    ):
        # The regex front-end does not attach a call to a `return` statement,
        # so `return Math.mulDiv(...)` arrives as an expression whose
        # identifiers include the method. Reading them here keeps both parser
        # paths finding the same relation instead of one quietly finding less.
        found.extend(_pair(value.identifiers, names, carriers,
                           (value.text or "").strip()))

    return found


def _pair(identifiers, names, carriers, text):
    """One (scalar, amount) pair, or nothing when either side is missing.

    Both sides have to be present. State times state is protocol arithmetic
    with nothing flowing through it; a local times a local involves no
    configured scalar to get wrong.
    """
    seen = set(identifiers)
    scalars = sorted(seen & names)
    amounts = sorted((seen & carriers) - names)
    if not scalars or not amounts:
        return []
    return [(scalars[0], amounts[0], text)]


def matches_signature(function, signatures) -> str | None:
    """The declared signature this function implements, or None.

    `transfer(address,uint256)` is a promise in a published ABI; a function
    called `transfer` that takes one argument is something else with the same
    spelling. Matching the signature is reading a standard. Matching the name
    alone is guessing.
    """
    return signatures.get(function.signature.replace(" ", ""))
