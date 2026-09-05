"""Go front-end: what a contract, an entry point and state are in a language
that has none of them, and whether the existing detectors fire on the result.

Every fixture is synthetic and shares no vocabulary with the engagement target
(`rsksmart/liquidity-provider-server`); the one test that reads the target is
skipped when it is not on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crystal import ir as I
from crystal.detectors.asymmetric_side_effect import EFFECT_COUPLED, detect as detect_guard
from crystal.detectors.unbounded_input import detect as detect_unbounded
from crystal.discovery import discover, profile
from crystal.parsers import GO_REGEX_LIMITATIONS, go_regex, go_ts, parse_project
from crystal.parsers import parser_report, treesitter_enabled
from crystal.parsers.base import detect_language
from crystal.symbolic import SymbolicEngine

needs_go_treesitter = pytest.mark.skipif(
    not treesitter_enabled() or not go_ts.available(),
    reason="needs tree-sitter-go (regex fallback covered separately)",
)

TARGET = Path(os.environ.get(
    "CRYSTAL_GO_TARGET", r"C:/Users/admin/Desktop/Vercel Sandbox/rootstock/lps"
))


# A service with an interface-typed collaborator, two exported methods reaching
# the same callee — one behind a revocable condition, one bare — a handler
# factory decoding a request body, a package-level variable and a DTO.
SERVICE = '''
package desk

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"sync"

	"github.com/gorilla/mux"
)

var registry = map[string]bool{}

const Limit = 10

type Ledger interface {
	Settle(ctx context.Context, who string, amount uint64) error
	Held(ctx context.Context, who string) (uint64, error)
}

type memLedger struct {
	stored map[string]uint64
}

func (l *memLedger) Settle(ctx context.Context, who string, amount uint64) error {
	taken := amount
	if l.stored[who] < amount {
		taken = l.stored[who]
	}
	l.stored[who] -= taken
	return nil
}

func (l *memLedger) Held(ctx context.Context, who string) (uint64, error) {
	return l.stored[who], nil
}

type Desk struct {
	ledger Ledger
	mu     sync.Mutex
	done   map[string]bool
	paused bool
}

func NewDesk(ledger Ledger) *Desk {
	return &Desk{ledger: ledger, done: map[string]bool{}}
}

func (d *Desk) CloseByOwner(ctx context.Context, id string, who string, amount uint64) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.done[id] = true
	stored, err := d.ledger.Held(ctx, who)
	if err != nil {
		return err
	}
	if stored < amount {
		return errors.New("too little")
	}
	return d.ledger.Settle(ctx, who, amount)
}

func (d *Desk) CloseByAnyone(ctx context.Context, id string, who string, amount uint64) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.done[id] = true
	return d.ledger.Settle(ctx, who, amount)
}

func (d *Desk) helper(x int) int {
	registry["x"] = true
	return x + 1
}

type closeRequest struct {
	Who    string `json:"who"`
	Amount uint64 `json:"amount"`
}

func NewCloseHandler(desk *Desk) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body closeRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		id := mux.Vars(r)["id"]
		if err := desk.CloseByAnyone(r.Context(), id, body.Who, body.Amount); err != nil {
			panic(err)
		}
		w.WriteHeader(http.StatusOK)
	}
}
'''

# A caller-chosen amount combined into a larger total reaching a value sink,
# next to a sibling that caps the same total.
TREASURY = '''
package treasury

import "context"

type Wallet interface {
	Transfer(to string, amount uint64) error
}

type Treasury struct {
	wallet Wallet
	max    uint64
}

func (t *Treasury) Pay(ctx context.Context, to string, amount uint64, tip uint64) error {
	return t.wallet.Transfer(to, amount + tip)
}

func (t *Treasury) PayCapped(ctx context.Context, to string, amount uint64, tip uint64) error {
	if amount + tip <= t.max {
		return t.wallet.Transfer(to, amount + tip)
	}
	return nil
}
'''

# The same cap in Go's own idiom: reject on excess and return early.
TREASURY_EARLY_RETURN = '''
package treasury

import "context"

type Wallet interface {
	Transfer(to string, amount uint64) error
}

type Treasury struct {
	wallet Wallet
	max    uint64
}

func (t *Treasury) PayCapped(ctx context.Context, to string, amount uint64, tip uint64) error {
	if amount + tip > t.max {
		return nil
	}
	return t.wallet.Transfer(to, amount + tip)
}
'''


def _by_name(contracts):
    return {contract.name: contract for contract in contracts}


def _function(contract, name):
    return next(f for f in contract.functions if f.name == name)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_go_is_a_registered_language(tmp_path):
    assert detect_language("internal/usecases/pegin/accept.go") == "go"
    (tmp_path / "a.go").write_text("package a\n\nfunc F() {}\n")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "dep.go").write_text("package dep\n")
    (tmp_path / "go.mod").write_text(
        "module example.com/svc\n\nrequire github.com/gorilla/mux v1.8.1\n"
    )
    found = discover(tmp_path)
    assert [p.name for p in found] == ["a.go"], "vendored code is not the target"
    summary = profile(tmp_path, found)
    assert summary.languages == {"go": 1}
    assert {"go-module", "gorilla-mux"} <= set(summary.frameworks)


def test_parser_report_names_the_go_backend_and_its_limitations(monkeypatch):
    monkeypatch.delenv("CRYSTAL_NO_TREESITTER", raising=False)
    report = parser_report()["go"]
    assert report["fallback"] == "regex"
    assert report["backend"] in {"tree-sitter", "regex"}
    assert report["reduced_fidelity"] == (report["backend"] == "regex")

    monkeypatch.setenv("CRYSTAL_NO_TREESITTER", "1")
    degraded = parser_report()["go"]
    assert degraded["backend"] == "regex"
    assert degraded["reduced_fidelity"] is True
    assert degraded["limitations"] == list(GO_REGEX_LIMITATIONS)
    assert any("interface" in item for item in degraded["limitations"])


def test_parse_project_routes_go_and_degrades_to_regex(tmp_path, monkeypatch):
    (tmp_path / "desk.go").write_text(SERVICE)
    monkeypatch.setenv("CRYSTAL_NO_TREESITTER", "1")
    parsed = parse_project(discover(tmp_path))
    assert parsed.parser == "go:regex"
    assert not parsed.diagnostics
    assert {c.parser for c in parsed.contracts} == {"regex"}
    assert "Desk" in _by_name(parsed.contracts)


# ---------------------------------------------------------------------------
# The mapping, on the tree-sitter front-end
# ---------------------------------------------------------------------------

@needs_go_treesitter
def test_struct_package_and_interface_become_contracts():
    contracts = _by_name(go_ts.parse_text(SERVICE, "svc/desk.go"))

    package = contracts["desk"]
    assert package.kind == "package"
    assert {v.name for v in package.state_vars} == {"registry", "Limit"}
    assert next(v for v in package.state_vars if v.name == "Limit").constant
    assert next(v for v in package.state_vars if v.name == "registry").key_types == ["string"]
    # A method-less DTO is a type of the package, not a contract.
    assert "closeRequest" in {t.name for t in package.types}
    assert "closeRequest" not in contracts

    desk = contracts["Desk"]
    assert desk.kind == "struct"
    assert [v.name for v in desk.state_vars] == ["ledger", "mu", "done", "paused"]
    assert all(v.visibility == "field" for v in desk.state_vars)

    ledger = contracts["Ledger"]
    assert ledger.kind == "interface"
    assert {f.name for f in ledger.functions} == {"Settle", "Held"}
    assert all(f.ir is None for f in ledger.functions)

    # Structural satisfaction, decided by signature: the implementation is
    # linked to the interface it satisfies, which is how a call through the
    # interface-typed field resolves to the code that runs.
    assert contracts["memLedger"].bases == ["Ledger"]
    assert desk.bases == []


@needs_go_treesitter
def test_entry_points_follow_export_and_handler_shape():
    contracts = _by_name(go_ts.parse_text(SERVICE, "svc/desk.go"))
    desk = contracts["Desk"]

    owner = _function(desk, "CloseByOwner")
    assert owner.visibility == "public" and owner.kind == "method"
    assert owner.is_entry_point
    assert owner.user_inputs == ["id", "who", "amount"], "the context is framework-supplied"

    helper = _function(desk, "helper")
    assert helper.visibility == "internal" and not helper.is_entry_point

    # Go has no constructor: a `NewX` factory is an ordinary function, and one
    # that only builds a value is `view` once purity has been propagated.
    factory = _function(contracts["desk"], "NewDesk")
    assert factory.kind == "function" and factory.visibility == "public"
    assert factory.mutability == "view"

    handler = _function(contracts["desk"], "NewCloseHandler")
    assert handler.kind == "handler" and handler.visibility == "external"
    assert handler.is_entry_point
    assert [p.name for p in handler.params] == ["desk", "w", "r"]
    assert handler.user_inputs == ["r"], "only the request is chosen by the sender"


@needs_go_treesitter
def test_state_is_reached_through_the_receiver_only():
    contracts = _by_name(go_ts.parse_text(SERVICE, "svc/desk.go"))
    desk = contracts["Desk"]
    owner = _function(desk, "CloseByOwner")
    assert owner.writes == {"done"}
    assert {"done", "ledger"} <= owner.reads
    assert owner.mutability == "stateful"

    settle = _function(contracts["memLedger"], "Settle")
    assert settle.writes == {"stored"}
    clamp = next(s for s in settle.ir.statements if s.kind == I.IF)
    assert clamp.condition.text == "self.stored[who] < amount"
    assert clamp.reads == ("stored",)
    assert clamp.body[0].kind == I.ASSIGN and clamp.body[0].target.text == "taken"
    debit = next(s for s in settle.ir.statements if s.kind == I.ASSIGN and s.writes)
    assert debit.operator == "-=" and debit.writes == ("stored",)

    helper = _function(desk, "helper")
    assert helper.writes == {"registry"}, "package-level state is written by name"


@needs_go_treesitter
def test_calls_are_classified_by_where_control_goes():
    contracts = _by_name(go_ts.parse_text(SERVICE, "svc/desk.go"))
    owner = _function(contracts["Desk"], "CloseByOwner")
    calls = {(c.callee, c.kind, c.receiver) for c in owner.ir.calls()}
    assert ("Settle", I.EXTERNAL_CALL, "ledger") in calls, "dispatch through a field"
    assert ("Held", I.EXTERNAL_CALL, "ledger") in calls
    assert ("Lock", I.BUILTIN_CALL, "mu") in calls, "a lock runs no user code"
    notes = [s.note for s in owner.ir.statements if s.call is not None and s.call.callee == "Unlock"]
    assert notes == ["defer unlock"]

    handler = _function(contracts["desk"], "NewCloseHandler")
    handler_calls = {(c.callee, c.kind, c.receiver) for c in handler.ir.calls()}
    # A parameter receiver is spelled with its declared type, so two handlers
    # calling `x.Run(..)` on differently typed `x` are not sibling sites.
    assert ("CloseByAnyone", I.EXTERNAL_CALL, "Desk(desk)") in handler_calls
    assert ("Vars", I.EXTERNAL_CALL, "mux") in handler_calls, "a third-party router"
    assert ("Decode", I.BUILTIN_CALL, "json.NewDecoder(r.Body)") in handler_calls, \
        "encoding/json is data plumbing"
    assert ("WriteHeader", I.BUILTIN_CALL, "w") in handler_calls, \
        "the response is the handler's output, not state or control"
    reverts = [s for s in handler.ir.walk() if s.kind == I.REVERT]
    assert reverts and reverts[0].note == "panic"


@needs_go_treesitter
def test_request_derived_locals_are_lowered_into_assignments():
    contracts = _by_name(go_ts.parse_text(SERVICE, "svc/desk.go"))
    handler = _function(contracts["desk"], "NewCloseHandler")
    derived = {(s.target.text, s.value.text, s.note.split(":")[0])
               for s in handler.ir.walk()
               if s.kind == I.ASSIGN and s.note and ("decoded" in s.note or "derived" in s.note)}
    assert ("body", "r", "decoded-from-request") in derived
    assert ("id", "r", "request-derived") in derived

    # The symbolic engine therefore sees the decoded body as attacker-chosen
    # at the call that consumes it.
    engine = SymbolicEngine(list(contracts.values()))
    records = [r for r in engine.call_records(handler) if r.call.callee == "CloseByAnyone"]
    assert records
    rendered = [a.render() for a in records[0].arguments]
    assert any("ARG:r" in text for text in rendered[1:]), rendered


@needs_go_treesitter
def test_if_initializers_and_switches_keep_the_guard_order():
    source = '''
package flow

import "errors"

type S struct{ n int }

func (s *S) Run(x int) error {
	if err := s.check(x); err != nil {
		return err
	}
	switch {
	case x > 3:
		s.n = x
	default:
		return errors.New("no")
	}
	s.n++
	return nil
}

func (s *S) check(x int) error { return nil }
'''
    contracts = _by_name(go_ts.parse_text(source, "flow/s.go"))
    run = _function(contracts["S"], "Run")
    kinds = [(s.kind, s.text) for s in run.ir.statements]
    assert kinds[0][0] == I.VAR_DECL and "s.check" in kinds[0][1].replace("self", "s")
    assert kinds[1][0] == I.IF and kinds[1][1] == "if err != nil"
    assert kinds[2][0] == I.IF and kinds[2][1] == "case x > 3"
    assert kinds[2] and run.ir.statements[2].orelse[0].kind == I.RETURN
    assert kinds[3] == (I.ASSIGN, "self.n++")
    assert run.writes == {"n"}
    assert run.ir.statements[0].call.callee == "check"
    assert run.ir.statements[0].call.kind == I.INTERNAL_CALL
    assert run.ir.statements[0].call.receiver is None, "a method on the receiver is internal"


@needs_go_treesitter
def test_test_files_and_generated_shapes_are_classified(tmp_path):
    contracts = go_ts.parse_text(
        "package desk\n\nfunc TestX(t *testing.T) {}\n", "svc/desk_test.go"
    )
    assert contracts and all(c.is_test for c in contracts)
    assert contracts[0].name == "desk_test"
    assert all(f.is_test for c in contracts for f in c.functions)

    from crystal.discovery import excluded_dir_reason
    generated = tmp_path / "bindings.go"
    generated.write_text("// Code generated - DO NOT EDIT.\n// This file is a generated binding\n\npackage b\n")
    assert excluded_dir_reason(generated) == "generated code (DO NOT EDIT header)"
    written = tmp_path / "service.go"
    written.write_text("package b\n\n// Do not edit lightly.\nfunc F() {}\n")
    assert excluded_dir_reason(written) is None


@needs_go_treesitter
def test_select_arms_are_blocks_not_guards():
    source = '''
package w

type W struct {
	n      int
	sink   chan int
	stop   chan bool
	client Client
}

type Client interface{ Send(x int) error }

func (w *W) Run() {
	for {
		select {
		case x := <-w.sink:
			w.client.Send(x)
			w.n = x
		case <-w.stop:
			return
		}
	}
}

func (w *W) Push(x int) error {
	return w.client.Send(x)
}
'''
    contracts = go_ts.parse_text(source, "w/w.go")
    run = _function(_by_name(contracts)["W"], "Run")
    loop = run.ir.statements[0]
    assert loop.kind == I.LOOP
    arms = [s for s in loop.body]
    assert [s.kind for s in arms] == [I.BLOCK, I.BLOCK]
    assert arms[0].note == "select-case" and arms[0].text.startswith("select case")
    assert arms[0].body[0].kind == I.VAR_DECL and arms[0].body[0].target.text == "x"
    # No guard-asymmetry between the arm and `Push`: a message arriving is
    # not a revocable condition the other path forgot.
    assert detect_guard(contracts) == []


@needs_go_treesitter
def test_packages_are_merged_across_files_and_names_disambiguated(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/m\n")
    for directory, package, body in (
        ("a", "a", "type Runner interface { Run(x int) int }\n"),
        ("b", "b", "type Job struct{ k int }\n\nfunc (j *Job) Run(x int) int { j.k = x; return x }\n"),
        ("c", "c", "type Job struct{}\n\nfunc (j *Job) Run(x string) int { return 0 }\n"),
        ("d/utils", "utils", "func Helper() int { return other() }\n"),
        ("e/utils", "utils", "func Other() int { return 1 }\n"),
    ):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
        (tmp_path / directory / "file.go").write_text(f"package {package}\n\n{body}")
    (tmp_path / "d" / "utils" / "second.go").write_text("package utils\n\nfunc other() int { return 2 }\n")

    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "job.go").write_text(
        "package f\n\ntype Job interface { Run(x int) int }\n\nfunc Use(j Job) int { return j.Run(1) }\n"
    )
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "only_test.go").write_text("package b\n\nfunc TestB(t *testing.T) {}\n")

    parsed = parse_project(discover(tmp_path))
    names = _by_name(parsed.contracts)
    assert "Job@b" in names and "Job@c" in names, "package-scoped names must not merge"
    assert names["Job"].kind == "interface", "the one interface keeps the bare name"
    assert "Runner" in names["Job@b"].bases, "satisfied by signature"
    assert "Job" in names["Job@b"].bases, "and the consumer-side interface too"
    assert "Runner" not in names["Job@c"].bases, "a different signature does not satisfy"
    # A test-only directory declaring `package b` is `b_test`; it does not
    # force the production `b` to be qualified (its struct is still `Job@b`).
    assert names["b_test"].is_test and "b_test_test" not in names
    # Two `utils` packages get distinct, readable labels; each holds every
    # function of its directory whichever file declared it.
    labels = {name for name in names if name.endswith("utils")}
    assert labels == {"d_utils", "e_utils"}
    assert {f.name for f in names["d_utils"].functions} == {"Helper", "other"}
    helper = _function(names["d_utils"], "Helper")
    assert helper.ir.statements[0].call.kind == I.INTERNAL_CALL


# ---------------------------------------------------------------------------
# Which detectors fire on the Go model
# ---------------------------------------------------------------------------

@needs_go_treesitter
def test_guard_asymmetry_fires_on_two_go_entry_points():
    contracts = go_ts.parse_text(SERVICE, "svc/desk.go")
    signals = detect_guard(contracts)
    assert len(signals) == 1, [s.title for s in signals]
    found = signals[0]
    assert found.contract == "Desk" and found.function == "CloseByAnyone"
    assert "Settle" in found.title and "CloseByOwner" in found.title
    assert found.language == "go"
    assert any(f"coupling [{EFFECT_COUPLED}]" in line for line in found.evidence), found.evidence
    assert any("stored < amount" in line for line in found.evidence)
    assert found.confidence >= 0.74


@needs_go_treesitter
def test_unbounded_input_fires_on_a_composite_caller_amount():
    contracts = go_ts.parse_text(TREASURY, "treasury/t.go")
    signals = detect_unbounded(contracts, SymbolicEngine(contracts))
    assert [s.function for s in signals] == ["Pay"], [s.title for s in signals]
    assert "Transfer" in signals[0].title
    assert any("ARG:amount" in line and "ARG:tip" in line for line in signals[0].evidence)
    assert any("no upper-bound guard observed" in line for line in signals[0].evidence)


@needs_go_treesitter
@pytest.mark.xfail(
    strict=True,
    reason=(
        "detector gap, not a front-end one: `unbounded-input-in-value-op` "
        "recognises a bound by `<=`, `.min(`, `Max*`; Go's idiom rejects on "
        "excess and returns early (`if x > max { return }`), which the IR "
        "carries faithfully as an `if` with a terminating body but the "
        "detector's bound vocabulary does not read. Solidity's "
        "`if (x > cap) revert()` has the same blind spot."
    ),
)
def test_unbounded_input_reads_an_early_return_cap():
    contracts = go_ts.parse_text(TREASURY_EARLY_RETURN, "treasury/t.go")
    signals = detect_unbounded(contracts, SymbolicEngine(contracts))
    assert signals == []


# ---------------------------------------------------------------------------
# The regex fallback: same records, declared limits
# ---------------------------------------------------------------------------

def test_regex_fallback_recovers_the_skeleton():
    contracts = _by_name(go_regex.parse_text(SERVICE, "svc/desk.go"))
    assert {"desk", "Desk", "memLedger", "Ledger"} <= set(contracts)
    desk = contracts["Desk"]
    assert [v.name for v in desk.state_vars] == ["ledger", "mu", "done", "paused"]
    owner = _function(desk, "CloseByOwner")
    assert owner.visibility == "public" and owner.is_entry_point
    assert owner.writes == {"done"}
    assert _function(desk, "helper").visibility == "internal"
    guards = [s for s in owner.ir.statements if s.kind == I.IF]
    assert [g.condition.text for g in guards] == ["err != nil", "stored < amount"]
    assert all(any(x.kind == I.RETURN for x in g.body) for g in guards)
    settle = [c for c in owner.ir.calls() if c.callee == "Settle"]
    assert settle and settle[0].receiver == "ledger" and settle[0].kind == I.EXTERNAL_CALL

    handler = _function(contracts["desk"], "NewCloseHandler")
    assert handler.kind == "handler" and handler.visibility == "external"
    assert any(s.note and s.note.startswith("decoded-from-request")
               for s in handler.ir.walk())

    # Declared, not pretended: no interface satisfaction on the fallback.
    assert contracts["memLedger"].bases == []
    assert contracts["Ledger"].kind == "interface"
    assert go_regex.available() and "reduced fidelity" in go_regex.status()


def test_regex_fallback_statement_splitter_follows_semicolon_insertion():
    body = 'x := f(a,\n\tb)\nif x > 1 {\n\treturn x\n}\ny++\n'
    ranges = go_regex.go_statement_ranges(body, 0, len(body))
    pieces = [body[s:e].strip() for s, e in ranges]
    assert pieces == ["x := f(a,\n\tb)", "if x > 1 {\n\treturn x\n}", "y++"]


def test_regex_fallback_guard_asymmetry_still_reaches_the_detector():
    contracts = go_regex.parse_text(SERVICE, "svc/desk.go")
    signals = detect_guard(contracts)
    assert [(s.contract, s.function) for s in signals] == [("Desk", "CloseByAnyone")]
    assert any("stored < amount" in line for line in signals[0].evidence)
    # `Settle` has one body in the file, so the detector attributes it by
    # name alone; the fallback's limit is a callee name two types share,
    # which no `bases` can disambiguate here.
    ambiguous = go_regex.parse_text(
        SERVICE + "\n\ntype other struct{ n int }\n\n"
        "func (o *other) Settle(ctx context.Context, who string, amount uint64) error { o.n = 1; return nil }\n",
        "svc/desk.go",
    )
    signals = detect_guard(ambiguous)
    assert [(s.contract, s.function) for s in signals] == [("Desk", "CloseByAnyone")]
    assert not any(f"coupling [{EFFECT_COUPLED}]" in line for line in signals[0].evidence)


# ---------------------------------------------------------------------------
# The real target
# ---------------------------------------------------------------------------

@needs_go_treesitter
@pytest.mark.skipif(not TARGET.is_dir(), reason="engagement target not on disk")
def test_real_target_parses_with_the_documented_model():
    files = [
        path for path in discover(TARGET)
        if any(part in {"handlers", "pegin", "quote", "rest"} for part in path.parts)
    ]
    assert len(files) > 40
    parsed = parse_project(files)
    assert not parsed.diagnostics, parsed.diagnostics[:3]
    names = _by_name(parsed.contracts)
    handlers = [f for c in parsed.contracts for f in c.functions if f.kind == "handler"]
    assert len(handlers) >= 20
    assert all(f.visibility == "external" for f in handlers)
    # Only the request is chosen by the sender, whatever the author called it.
    assert all(f.user_inputs in (["req"], ["r"]) for f in handlers), [
        (f.name, f.user_inputs) for f in handlers if f.user_inputs not in (["req"], ["r"])
    ]
    # The use case satisfies the interface the handler package declares for it
    # under the same name; the interface keeps the bare name, the structs
    # (pegin's and pegout's) are qualified by package.
    assert names["AcceptQuoteUseCase"].kind == "interface"
    use_case = names["AcceptQuoteUseCase@pegin"]
    assert "AcceptQuoteUseCase" in use_case.bases
    run = _function(use_case, "Run")
    assert run.is_entry_point and run.user_inputs == ["quoteHash", "signature"]
    assert {"quoteRepository", "contracts"} <= run.reads
