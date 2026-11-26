# DEFENCE

What Norma found in this repository and what we did about it, plus the one conflict between the two
authors. Every entry cites a row in [findings.md](findings.md); nothing here comes from anywhere else.

TODO: check the section layout against Norma's own DEFENCE template; it was not available in the repo
when this was written, so the headings below are the three the team asked for.

Authors: Aryan Kumar Singh (with Claude Code) and Saurabh Kumar (with Claude Code).

## Fixed

| Rule | Severity | File | What Norma found | Fix |
|---|---|---|---|---|
| `py-mng-open-no-encoding` | LOW | vet/report.py | `read_text("utf-8")` passed the encoding positionally. | `read_text(encoding="utf-8")`. Re-check clean. |
| `py-perf-double-lookup` | LOW | vet/verify.py | `if fid in by_id: pool[fid] = by_id[fid]` looked the key up twice. | One `by_id.get(fid)`. Re-check clean. |
| `fa-mnt-no-typing-param` | MEDIUM | vet/config.py | Untyped `cls` on the pydantic `field_validator` for `output_format`. | Validator replaced by a `Literal["json", "csv"]` field type, which pydantic validates itself. Same behaviour, re-check clean. |
| `py-maint-else-after-return` | HIGH | vet/collectors/classify.py | Redundant `else` after `raise` in the chunk loop. | Restructured with an early `continue`. Re-check clean. |

All four were raised by `live_check` before the code was committed.

## Accepted

None. Norma has not raised a finding we chose to keep.

- The `live_check` sweep of all of `main` after PR #10 (57 Python files, four rulesets) found 0 issues.
- The Norma GitHub App check found no issues on any PR from #2 to #10.
- TODO: the Norma dashboard full scan of `main`, which includes the LLM-based rules, has not been read
  yet. Any finding it raises will be fixed, or listed here with the reason it is kept.

Broad exception handling (`except Exception` around each collector, the risk sources and the trace
listener) is deliberate: one failed source or display hook must not cost a verdict in the live round.
Norma has not flagged it, so it is not listed as an accepted finding. Our reasoning is in findings.md under
"Self-review notes".

## Conflict between authors

| Authors | Rule / file | What clashed | Resolution |
|---|---|---|---|
| Aryan Kumar Singh / Claude Code vs Saurabh Kumar / Claude Code | Project rule, not a Norma rule: every run's verdict file is validated (`vet run` → `validate_verdict_file`). Files: vet/cli.py, vet/demo.py | `vet demo` (PR #10) wrote and rendered the run but never validated the verdict file, so an invalid file went unreported during a demo run. | PR #10 merged as written. In the follow-up (branch `chore/final-pass`), `_save_run` in vet/cli.py writes, validates and renders for both commands, and a test checks that `vet demo` reports the same validation problems as `vet run`. |

Process note: Saurabh's own `live_check` on PR #10 was refused (`remote_not_matched`, his Norma account
is not linked to this repository). His files were checked from Aryan's session afterwards: all clean.
