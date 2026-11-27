# Defence

## Policy and consistency
The policy: [docs/norma-policy.md](docs/norma-policy.md), the rules we treat as blocking (13) and
advisory (4), each a rule id copied from Norma's `get_rules_for_ruleset` for this repository
(python-norma-ruleset, secrets-norma-ruleset; the org-scoped "Policies" ruleset held 0 rules).
Every agent that opens the repo is connected to Norma through the project-scoped `.mcp.json`
(server URL only, no token).

How both authors' pull requests were checked on 27 Nov:
- Aryan Kumar Singh (with Claude Code): PR #13 and PR #16 were checked with `live_check` on the
  changed source files before commit (all clean; what was not re-sent after the last edit is
  listed in findings.md), and both had a clean Norma GitHub App PR check before merge.
- Saurabh Kumar (with Claude Code): PR #14 (identity audit) and PR #15 (archive check) each had a
  clean Norma GitHub App PR check, and were reviewed against their brief before merge (ruff and
  pytest run on his branch: 297 and 307 passed).
- One Norma AI finding on main after PR #13 (`error-handling-layer-1.0`) is accepted, see below.

Where the authors differed (recorded, not changed):
- Two-axis judge exposure. `corporate` facts (Aryan) have weight 0 and never reach the judge;
  `archive` facts (Saurabh, #15) are NEUTRAL with weight 0 because a positive non-vendor fact
  counts toward the judge's trust floor at any weight; `identity` facts (Saurabh, #14) carry
  weights 0.15–0.3 and do reach the judge.
- Both of Saurabh's collectors and the `corporate` collector registered in the same lines of
  `AFTER_SITE`; the two merge conflicts are logged in findings.md under "Author conflicts".

## Fixed
Rule: `py-maint-else-after-return` (HIGH)
File: vet/collectors/classify.py
Why it mattered: This loop classifies search results in chunks, and so decides which results count
as evidence. A redundant `else` after a `raise` put its normal path one level deeper than it needed
to be. The loop was rewritten with an early `continue`, and `live_check` is clean again.

## Accepted
Rule: `error-handling-layer-1.0` (High), "A failure is caught and nobody ever finds out". Raised
by the Norma AI scan of `main` after PR #13.
File: `vet/orchestrator.py`, `run_product_safe` (the `except Exception` around one product's run).
Why we left it: the catch is the batch-survival design. One product that raises must never stop
the batch: before PR #13 one API error on one product could end the whole run with nothing
saved. We keep the catch and make sure the failure is never silent. A failed product is
surfaced in five places, each checked by `tests/test_buyer_view.py::test_a_failed_product_is_visible_everywhere`:
1. `logger.exception` at ERROR level, naming the product and the exception type, with the
   traceback;
2. a `product_failed` event with the error in the run's `trace.jsonl`;
3. the record's `errors` list (`run: <ExceptionType>: <message>`);
4. the buyer-view row: Yellow, reasoning starting "Yellow: run failed: <ExceptionType>: ..." and
   ending "Not checked: all sources (the run failed).";
5. the `vet run` / `vet eval` summary, which prints "N of M products failed" and lists each one.
Cancellation is not caught (`asyncio.CancelledError` is not an `Exception`), so stopping a run
still stops it.

## Conflict between authors
Authors: Aryan Kumar Singh (with Claude Code) and Saurabh Kumar (with Claude Code)
What clashed: `vet run` checks every verdict file it writes (`validate_verdict_file`). Saurabh's
`vet demo` (PR #10) wrote and rendered the run without that check, so a demo run with an invalid
verdict file showed its verdict panel with no warning.
How we resolved it: PR #10 was merged as written. In PR #11, one shared function (`_save_run` in
vet/cli.py) writes, validates and renders the run for both commands. A test checks that `vet demo`
reports the same validation problems as `vet run`.

A second clash, 27 Nov: Aryan's `corporate` collector and Saurabh's `identity` (PR #14) and
`archive` (PR #15) collectors were registered in the same two lines of vet/orchestrator.py (the
collector import and `AFTER_SITE`), which gave a git conflict at each merge. Resolved by keeping
all three collectors; logged in findings.md under "Author conflicts".

## Other fixed findings
All three were raised by `live_check` before commit and are logged in [findings.md](findings.md).

- `py-mng-open-no-encoding` (LOW), vet/report.py: `read_text("utf-8")` passed the encoding as a
  positional argument. Changed to `read_text(encoding="utf-8")`.
- `py-perf-double-lookup` (LOW), vet/verify.py: `if fid in by_id: pool[fid] = by_id[fid]` looked
  the key up twice. Now a single `by_id.get(fid)`.
- `fa-mnt-no-typing-param` (MEDIUM), vet/config.py: an untyped `cls` on the pydantic validator for
  `output_format`. The validator was replaced by a `Literal["json", "csv"]` field type, with the same
  behaviour.
