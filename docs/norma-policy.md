# Norma policy (proposed, 2026-11-27)

The rules the team agrees to treat as blocking for `vet`. Rule ids are copied from Norma's
`get_rulesets` / `get_rules_for_ruleset` output for this repository (status `linked`, repo id 12410).
The org-scoped `Policies` ruleset currently holds 0 rules; this file is the proposal for it.

Scope: page 1 of `python-norma-ruleset` (50 of 63 rules) and all 13 rules of `secrets-norma-ruleset`
were read. Page 2 of the Python ruleset was not reviewed today (5-minute timebox).

## Blocking (fix before merge)

| Rule id | Severity | Why we agree |
|---|---|---|
| `py-sec-hardcoded-secret-1.0` | MEDIUM | AGENTS.md: secrets only from `.env`, never in code. |
| `sec-ai-provider-key-1.0` | HIGH | We hold an Anthropic key; a leaked key spends our budget. |
| `sec-hardcoded-bearer-token-1.0` | HIGH | Tavily and Companies House keys go in headers; they must come from settings. |
| `py-scal-http-no-deadline-1.0` | HIGH | Every network call has a deadline (15 s default) via `vet/fetch.py`. |
| `py-sec-tls-verify-false-1.0` | HIGH | We judge vendors' sites; we never weaken our own TLS checks. |
| `py-perf-blocking-call-in-async-1.0` | HIGH | The pipeline is async with 4 products in parallel; one blocking call stalls all. |
| `py-perf-async-sleep-1.0` | HIGH | Same reason: rate limiting must use `asyncio.sleep`. |
| `py-sec-eval-exec-1.0` | HIGH | Page text is untrusted data and must never become code. |
| `py-sec-xml-xxe-1.0` | HIGH | Sitemaps and feeds from vendor sites are untrusted XML. |
| `py-maint-no-print-1.0` | MEDIUM | AGENTS.md: `logging`, not `print()` (CLI output via the console writer is accepted case by case). |
| `py-maint-logger-fstring-1.0` | MEDIUM | AGENTS.md: lazy `%s` formatting in log calls. |
| `py-maint-naive-datetime-1.0` | MEDIUM | AGENTS.md: timezone-aware datetimes; domain-age maths depends on it. |
| `py-mng-magic-timeout-1.0` | LOW | AGENTS.md: named constants for timeouts. |

## Advisory (fix when touched, accept with a reason in findings.md)

| Rule id | Severity | Why |
|---|---|---|
| `py-maint-broad-except-1.0` | HIGH | Collectors must never crash a run, so some broad catches are deliberate; each must log. |
| `py-maint-else-after-return-1.0` | HIGH | Readability only; not worth churn in stable code. |
| `py-mng-open-no-encoding-1.0` | LOW | Cheap to fix; we read and write UTF-8 CSV and JSON. |
| `py-maint-legacy-typing-1.0` | MEDIUM | Python 3.12 builtin generics; ruff already enforces most of it. |
