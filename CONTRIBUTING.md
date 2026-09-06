# Contributing to Crystal

Crystal is in beta and open to contributions. The most valuable thing you can
send is **a target it gets wrong**: a signal that is false, or a defect it
stayed silent on. Both are bugs, and the second is the worse one.

## Three rules, enforced by tests rather than by review

A change that ignores these will fail the suite, not the review.

**1. Zero fabrication.** Missing an ABI type, a constructor argument, a
deployment context or a state variable means `UNSUPPORTED` with the precise
reason — never a guess that looks like an answer. A refusal that says why is a
correct result here. Approximating is not.

**2. Zero confirmed findings.** The proof checklist has eight gates. Two of
them, `economic_impact` and `minimal_trace`, are never set automatically
because they require protocol interpretation. `CONFIRMED` is therefore
unreachable by machine, and `confirmed_findings` is structurally 0. The same
discipline now governs backend verdicts: `HELD` raises `WitnessRequired`
without an execution witness, so it cannot be constructed.

**3. SARIF severity never escalates.** Every result is `level: note`,
`kind: review`. Crystal does not tell your CI that something is broken.

## What a good change looks like

* **Measure before and after, on a real target.** Not on a fixture you wrote to
  pass. Several of Crystal's own builds were corrected because a synthetic test
  agreed with a broken engine.
* **Raising the signal count is not progress.** On one reference protocol,
  build 009 produced 9 signals of which zero were real; build 014 produces 3 of
  which one is. That is the direction. A detector that fires more is a
  regression unless the ratio improved.
* **A detector's premise decides where it runs.** If your reasoning depends on
  an execution model, declare `LANGUAGES` on the module. `reentrancy-ordering`
  produced 51 signals on a Go service — every one true of the syntax and about
  nothing, because Go has no re-entrant dispatch.
* **Name what you cannot do.** A limitation that is stated costs a reader a
  minute; one that is hidden costs them a day. `parser_report()` carries
  per-language limitations for exactly this reason.
* **Match the surrounding code.** Comment density, naming and idiom included.

## Running the suite

```bash
pip install -e ".[dev]"
pytest -q                          # full suite
CRYSTAL_NO_TREESITTER=1 pytest -q  # the regex fallback path — must stay green
CRYSTAL_NO_FOUNDRY=1 pytest -q     # skip real-EVM execution
```

**Both parser paths must pass.** The fallback was broken for four builds
because only one of them was being run; tests that genuinely require
tree-sitter are skipped with an explicit reason, never silently.

Two documentation checks are part of the suite: the README test-count badge
must match what pytest collects, and every changelog entry ships with its
README update in the same commit. They exist because both drifted.

## What happens to your pull request

`main` is protected. Seven checks must pass before merge, they must have run
against an up-to-date `main`, and one review is required — Crystal has already
shipped a verification that passed only because it ran against a stale base.

| check | what it can catch |
| --- | --- |
| `invariants` | `CONFIRMED` made reachable, `HELD` constructable without an execution witness, or SARIF escalating above `note` |
| `tree-sitter` / `regex` × py3.10, py3.13, + one Windows run | a change that only works on the path you happened to run |
| `zero-dependency core` | an import that quietly adds a dependency the README says does not exist |

The pull request template asks for a before/after table on a real target, and
for the limitation you already know about. Neither is ceremony: the first is
how the three builds above caught themselves, and the second is the difference
between a reader losing a minute and losing a day.

## Reporting a target Crystal gets wrong

Open an issue with:

* the repository and commit, or a reduced source file that reproduces it;
* the command you ran;
* what Crystal said, and what you expected instead;
* whether tree-sitter was installed (`crystal doctor` prints this, along with
  whether the running build matches the checkout — worth pasting).

A reduced reproduction is welcome but not required. A real target that behaves
badly is more useful than a tidy fixture, because the fixture is what fooled us
last time.

## Licence

By contributing you agree that your contribution is licensed under the MIT
Licence, as in [LICENSE](LICENSE).
