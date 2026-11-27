# Norma policy (proposed, 2026-11-27)

The rules the team agrees to treat as blocking for `vet`. Rule ids are copied from Norma's
`get_rulesets` / `get_rules_for_ruleset` output for this repository (status `linked`, repo id 12410).
The org-scoped `Policies` ruleset held 0 rules in the morning; by the afternoon it held the two
organisation policies below.

Scope: all 63 rules of `python-norma-ruleset` (both pages) and all 13 rules of
`secrets-norma-ruleset` were read. Page 2 was reviewed later the same day; the code complied
with every rule added below when they were added (the sqlite `execute()` calls use constant
SQL with bound `?` parameters; the one `subprocess.run` call has `timeout=GIT_TIMEOUT_S`).

## Organisation policies (Norma workspace "Vet", created 27 Nov)
Two policies, which Norma turns into rules in the org-scoped "Policies" ruleset and checks on
every dependency manifest and lockfile:

| Policy | Rule id | Severity | What it says | Why we agree |
|---|---|---|---|---|
| No second HTTP client, no browser automation (Banned packages) | `policy-banned-deps-a5f2-1.0` | HIGH | Bans `requests`, `aiohttp`, `playwright`, `selenium`, directly or as a sub-dependency. | Every network call goes through `vet/fetch.py`, which enforces robots.txt, rate limits and the cache; a second HTTP client would bypass all three. vet is read-only and never renders pages, fills forms or logs in, so browser automation has no place. Banning `requests` and `aiohttp` came from Saurabh's review. |
| Permissive licences only (Licence policy) | `policy-licence-a9e3-1.0` | MEDIUM | Allows MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, PSF-2.0, ISC. | Keeps the dependency tree free of licence obligations we cannot review in a hackathon. Dry run: 0 violations. |

First finding: the banned-packages policy flagged the unused optional extra
`browser = ["playwright>=1.45"]` in pyproject.toml (score 96, 1 High). It was removed the same
day (see findings.md).

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
| `py-sec-dbapi-execute-fstring-1.0` | HIGH | The fetch and LLM caches are sqlite; a key built from vendor URLs must never become SQL. |
| `py-sec-dbapi-execute-percent-1.0` | HIGH | Same reason: bound parameters only. |
| `py-sec-dbapi-execute-concat-1.0` | HIGH | Same reason: bound parameters only. |
| `py-scal-subprocess-no-timeout-1.0` | HIGH | The CLI calls git; a hung subprocess must not hang a live round. |
| `py-scal-requests-no-timeout-1.0` | MEDIUM | Same intent as the HTTP-deadline rule if `requests` is ever added; we use httpx via fetch.py. |

## Advisory (fix when touched, accept with a reason in findings.md)

| Rule id | Severity | Why |
|---|---|---|
| `py-maint-broad-except-1.0` | HIGH | Collectors must never crash a run, so some broad catches are deliberate; each must log. |
| `py-maint-else-after-return-1.0` | HIGH | Readability only; not worth churn in stable code. |
| `py-mng-open-no-encoding-1.0` | LOW | Cheap to fix; we read and write UTF-8 CSV and JSON. |
| `py-maint-legacy-typing-1.0` | MEDIUM | Python 3.12 builtin generics; ruff already enforces most of it. |
| `py-scal-mutable-class-attr-1.0` | HIGH | Shared state across products running in parallel would leak between verdicts. |
| `py-scal-lru-on-self-1.0` | HIGH | Long batch runs; caches must not pin objects. We cache in sqlite instead. |
| `py-perf-double-lookup-1.0` | LOW | Fixed once already (vet/verify.py); cheap to keep clean. |
| `py-perf-string-concat-loop-1.0` | MEDIUM | Flag cells and reports are joined with `" \| ".join(...)`; keep it that way. |
| `py-perf-list-in-agg-1.0` | MEDIUM | Readability; generators in `sum`/`any`/`all`. |
| `py-scal-deepcopy-1.0` | MEDIUM | Pydantic `model_copy` is the norm here; a deepcopy needs a reason. |
