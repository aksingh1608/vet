# Findings log

## Norma findings

Only issues Norma actually reported, through `live_check` (MCP) or the Norma GitHub App's pull request check.
Nothing else goes in this section. DEFENCE.md cites only this section.

| Date | Branch / PR | Source | Rule | File | Severity | Status | Why / fix |
|---|---|---|---|---|---|---|---|
| 2026-11-25 | feat/phase-2-first-verdict (PR #2) | `live_check` (python, secrets, fastapi, sqlalchemy rulesets) | — | 29 `.py` source and test files | — | clean | 0 issues. `.env.example` and `.gitignore` returned `not_checkable` (no rules for these file types). Outcome registered with `register_applied_actions` (11 verified compliant, 3 prevented). |
| 2026-11-25 | feat/phase-2-first-verdict (PR #2) | Norma GitHub App PR check | — | files changed by PR #2 | — | clean | "No issues introduced by your changes." [Report](https://norma.qualityclouds.com/projects/12410) |
| 2026-11-26 | feat/phase-3-signals | `live_check` (python, secrets, fastapi, sqlalchemy rulesets) | py-mng-open-no-encoding | vet/report.py:52, :54 | LOW | fixed | `read_text("utf-8")` passed the encoding positionally; changed to `read_text(encoding="utf-8")`. Re-check clean. |
| 2026-11-26 | feat/phase-3-signals | `live_check` | py-perf-double-lookup | vet/verify.py:450 | LOW | fixed | `if fid in by_id: pool[fid] = by_id[fid]` looked the key up twice; now one `by_id.get(fid)`. Re-check clean. |
| 2026-11-26 | feat/phase-3-signals | `live_check` | — | 24 changed `vet/` source files | — | clean | After the two fixes above, 0 issues. The 16 changed test files were not sent to `live_check` (free plan, scans used sparingly); the Norma GitHub App check on PR #3 covers them. Templates (`.j2`) and prompts (`.md`) have no applicable rules. |
| 2026-11-26 | feat/phase-3-signals (PR #3) | Norma GitHub App PR check | — | files changed by PR #3 (incl. the 16 test files not sent to `live_check`) | — | clean | "No issues introduced by your changes." [Report](https://norma.qualityclouds.com/projects/12410) |
| 2026-11-26 | feat/phase-4-eval | `live_check` (fastapi ruleset) | fa-mnt-no-typing-param | vet/config.py:130 | MEDIUM | fixed | Raised on the pydantic `field_validator` for `output_format` (untyped `cls`; not a FastAPI route). Replaced the validator with a `Literal["json", "csv"]` field type, which pydantic validates itself; the env value is lower-cased in `from_env`. Same behaviour, re-check clean. |
| 2026-11-26 | feat/phase-4-eval | `live_check` | — | 13 changed source files (vet/, eval/run_eval.py) | — | clean | After the fix above, 0 issues. `pyproject.toml`: `not_checkable`. `uv.lock` (only change: pyyaml 6.0.4 added) and the changed test files were not sent (free plan); the Norma GitHub App check on PR #4 covers them. |
| 2026-11-26 | feat/phase-4-eval (PR #4) | Norma GitHub App PR check | — | files changed by PR #4 | — | clean | "No issues introduced by your changes." [Report](https://norma.qualityclouds.com/projects/12410) |
| 2026-11-26 | feat/phase-4b-hollow | `live_check` | — | vet/judge.py, vet/verify.py, vet/cli.py | — | clean | 0 issues. New tests left to the Norma GitHub App check on PR #5. |
| 2026-11-26 | feat/phase-4b-hollow | `live_check` | py-maint-else-after-return | vet/collectors/classify.py:75 | HIGH | fixed | Redundant `else` after `raise` in the chunk loop; restructured with an early `continue`. Re-check clean. |
| 2026-11-26 | feat/phase-4b-hollow | `live_check` | — | vet/llm.py, vet/collectors/footprint.py, vet/collectors/risk.py | — | clean | 0 issues (rehearsal fixes: retry cut-off structured output; classify search results in chunks). |

## Self-review notes (not Norma findings)

Our own review of the code against the Norma rulesets (python-norma-ruleset, secrets-norma-ruleset), written
while Norma could not yet see the repo. Norma did not raise any of these. They are kept as design notes only.

| Date | Branch | Rule we reviewed against | File | Note |
|---|---|---|---|---|
| 2026-11-25 | feat/phase-1-skeleton | py-mng-ip-literal-1.0 | vet/config.py | `DEFAULT_HOST_INTERVALS` names API hosts (web.archive.org, crt.sh, …) as rate-limit defaults. They can be overridden with `VET_HOST_INTERVALS`; the hosts are part of the spec, not deployment config. |
| 2026-11-25 | feat/phase-2-first-verdict | py-maint-broad-except-1.0 | vet/collectors/base.py, vet/orchestrator.py | `except Exception` around each collector is the spec's graceful-failure rule. The error is logged with `logger.warning(..., exc_info=True)` and recorded in the trace and product record; it is not swallowed. |
| 2026-11-25 | feat/phase-2-first-verdict | py-mng-ip-literal-1.0 / py-maint-hardcoded-url-default-1.0 | vet/collectors/*.py, vet/tavily.py | API endpoints (RDAP, crt.sh, Wayback CDX, Companies House, Tavily) are module constants named by the spec, not deployment config. No URL is an `os.getenv` default. |
| 2026-11-25 | feat/phase-2-first-verdict | py-perf-blocking-call-in-async-1.0 | vet/llm.py `load_prompt` | Reads a <2 KB prompt file from the package inside async callers; negligible. SQLite and trafilatura calls already run in `asyncio.to_thread`. |
| 2026-11-25 | feat/phase-2-first-verdict | py-sec-hardcoded-secret-1.0 | tests/conftest.py | `FAKE_TAVILY_TOKEN` / `FAKE_CH_TOKEN` are placeholder strings for offline tests, never sent anywhere; tests assert they stay out of URLs, bodies and the cache. |
| 2026-11-25 | feat/phase-2-first-verdict | py-sec-hardcoded-secret-1.0, sec-* | vet/*.py | Keys come only from `.env` via `SecretStr` and are sent only in headers (Tavily Bearer, Companies House Basic); never in URLs, request bodies, cache keys, cache rows, logs or traces. |
| 2026-11-25 | feat/phase-2-first-verdict | py-scal-http-no-deadline-1.0, py-mng-subprocess-no-check-1.0, py-scal-subprocess-no-timeout-1.0 | vet/fetch.py, vet/llm.py, vet/cli.py | Every HTTP client has a deadline (15 s, crt.sh 20 s, LLM 90 s); `git rev-parse` uses `check=True` and a timeout. |
