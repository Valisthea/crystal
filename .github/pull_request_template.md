<!--
Crystal's failure mode is a change that looks right and is measured wrong.
Several of its own builds corrected the build before them because a synthetic
test agreed with a broken engine. These questions exist for that reason, not
as ceremony — answer the ones that apply and delete the rest.
-->

## What changes, and why

<!-- One paragraph. What was wrong, or missing, and what this does about it. -->

## Measured

<!--
On a real target where possible, not only on a fixture you wrote to pass. If
you have no real target, say so — that is useful information, not a failing.
-->

| | before | after |
| --- | --- | --- |
| target | | |
| signals (total, and by detector) | | |
| of which you believe real | | |

**Raising the signal count is not progress.** If the count went up, say what
makes the new ones worth an auditor's time.

## The three rules

- [ ] **Zero fabrication** — anything Crystal cannot establish is `UNSUPPORTED`
      with a precise reason, never an approximation that reads like an answer.
- [ ] **Zero confirmed findings** — nothing here can produce `CONFIRMED`, and
      no `HELD` is reachable without an execution witness.
- [ ] **SARIF never escalates** — every result stays `level: note`.

## Both parser paths

- [ ] `pytest -q` passes
- [ ] `CRYSTAL_NO_TREESITTER=1 pytest -q` passes

<!--
The regex fallback was broken for four builds because only one path was ever
run. If a test genuinely needs tree-sitter, skip it with a reason that names
the limitation — never silently.
-->

## What this does not do

<!--
The most valuable section. Name the limitation you know about rather than
leaving it for a reader to discover. A gap that is stated costs a minute; one
that is hidden costs a day.
-->
