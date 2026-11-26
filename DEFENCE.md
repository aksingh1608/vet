# Defence

## Fixed
Rule: `py-maint-else-after-return` (HIGH)
File: vet/collectors/classify.py
Why it mattered: This loop classifies search results in chunks, and so decides which results count
as evidence. A redundant `else` after a `raise` put its normal path one level deeper than it needed
to be. The loop was rewritten with an early `continue`, and `live_check` is clean again.

## Accepted
Rule: TODO: pending the Norma dashboard scan
File: TODO: pending the Norma dashboard scan
Why we left it: TODO: pending the Norma dashboard scan

## Conflict between authors
Authors: Aryan Kumar Singh (with Claude Code) and Saurabh Kumar (with Claude Code)
What clashed: `vet run` checks every verdict file it writes (`validate_verdict_file`). Saurabh's
`vet demo` (PR #10) wrote and rendered the run without that check, so a demo run with an invalid
verdict file showed its verdict panel with no warning.
How we resolved it: PR #10 was merged as written. In PR #11, one shared function (`_save_run` in
vet/cli.py) writes, validates and renders the run for both commands. A test checks that `vet demo`
reports the same validation problems as `vet run`.

## Other fixed findings
All three were raised by `live_check` before commit and are logged in [findings.md](findings.md).

- `py-mng-open-no-encoding` (LOW), vet/report.py: `read_text("utf-8")` passed the encoding as a
  positional argument. Changed to `read_text(encoding="utf-8")`.
- `py-perf-double-lookup` (LOW), vet/verify.py: `if fid in by_id: pool[fid] = by_id[fid]` looked
  the key up twice. Now a single `by_id.get(fid)`.
- `fa-mnt-no-typing-param` (MEDIUM), vet/config.py: an untyped `cls` on the pydantic validator for
  `output_format`. The validator was replaced by a `Literal["json", "csv"]` field type, with the same
  behaviour.
