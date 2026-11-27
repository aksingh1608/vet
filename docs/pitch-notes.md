# Pitch notes

Sources: the repo, the tests, `runs/`, `findings.md` and the commit history. Nothing else.

## Numbers
- **Tests:** 279, all passing (`uv run pytest`). Unit tests use no network.
- **6 collectors:**
  - `age`: when the domain appeared (RDAP registration, first certificate on crt.sh, Wayback history). No LLM.
  - `legal_id`: whether the legal entity named on the site is in a company register (Companies House for UK; North Data via search snippets for everyone else).
  - `footprint`: what independent sources say about the vendor (Tavily searches; the vendor's own domain is excluded).
  - `risk`: breaches, lawsuits, fines, insolvency, acquisitions, shutdowns (Have I Been Pwned plus EN/DE news).
  - `alive`: whether the product is still being built (changelog dates; GitHub, PyPI, npm, App Store records).
  - `looks`: how the site is built (builder fingerprints, placeholder text, dead help links). Tie-break only, never scored.
  - Plus `claims_check`, which compares the vendor's claims (e.g. founding year) with the collectors' facts.
- **10 external sources called:**
  - Records: RDAP, crt.sh, Wayback Machine, Companies House, Have I Been Pwned.
  - Platforms: GitHub API, PyPI, npm registry, iTunes lookup.
  - Search: Tavily.
  - Plus the vendor's own website. North Data and the review sites (G2, Capterra, Trustpilot, OMR, Software Advice, Gartner) and LinkedIn are never fetched: they are read only from search snippets.
- **Latest cold run** (`runs/20261126-122153-e21a41`, 11 products, empty caches):
  - Average **59.6 s** and **20.2 Tavily credits** per product (222 credits in total).
  - Range 45.7–86.6 s per product.
  - **About 188 s** for the whole 11-product run, at 4 products in parallel. This is the time from the run's start to its last file being written.

## Scenario tests (`tests/test_scenarios.py`, no network)
- Each scenario is judged three ways: with the judge model down, with a model that always says yes/trust, and with one that always says no/do_not_trust.
- **Hollow vendor** (polished site, domain 6 weeks old, founding year claimed long before, dead help page):
  - Expected: no / do_not_trust.
  - Actual: no / do_not_trust with all three judges.
- **Real but tiny** (domain 150 days old, one Product Hunt page, register confirms the founding year):
  - Expected: never no or do_not_trust; trust must be caution.
  - Actual: unclear / caution with all three judges.
- **Dead product** (old domain, the vendor's own shutdown notice, changelog frozen for 3 years):
  - Expected: real_alive no.
  - Actual: no / caution with all three judges.
- **Rule bugs these exposed** (on the old rules, 11 of 12 checks failed):
  - RDAP, crt.sh and Wayback counted a young domain three times (score 1.5), enough for "no" on youth alone. Age records are now one signal.
  - "no" could come from youth alone. It now needs two independent negative signals.
  - "do_not_trust" now needs a red flag: a trust hard flag, a contradicted claim, or the hollow pattern.
  - Vendors under 2 years old are capped at "caution" for trust.

## Stability
- **The labels are decided in code, not by the model.** Each question gets a score from the verified facts (each fact's weight, plus for positive and minus for negative). Fixed thresholds turn the score into a label, and a score exactly at a threshold gets the more cautious label.
- The judge model only writes the summary and ranks the reasons. If it proposes other labels, a warning is logged and the code's labels are kept.
- **10 of 11 products gave identical labels in two reruns.** Both reruns used only cached fetches and searches (0 Tavily credits), with the model's answer cache cleared before each one. The run directories are `runs/20261126-135826-0d987d` and `runs/20261126-140012-9e499a`.
- **Linear still varies** (yes/caution in one run, yes/trust in the other) because the evidence found varies. The model classifies search results, and in one run it counted a third strong independent site (techcrunch.com). With 3 strong sites, the rule for vendors 2–10 years old allows "trust"; with 2 it doesn't.

## Security / red team
- **5 attack fixture sites** (`tests/fixtures/redteam/`), each trying to steer an AI evaluator through a different channel:
  - `01_visible`: a plain paragraph telling AI evaluators what to conclude.
  - `02_css_hidden`: an off-screen div, zero font size, and a class hidden by a `<style>` rule.
  - `03_comment_meta`: instructions in an HTML comment and in a custom meta tag.
  - `04_attributes_colour`: image alt text, a link title, and white-on-white text.
  - `05_tag_escape`: tries to close `</untrusted_content>` and open a fake system block. It also has ordinary AI product copy and an llms.txt link, which must not be flagged.
- **3 guard bugs the fixtures found, all fixed:**
  - The sanitizer left the rest of an injected instruction in place.
  - Entity splitting broke the quote markers.
  - A shared footer caused duplicate flags.
- A vendor page with hidden instructions for AI evaluators is recorded as a red flag: a hard-flag negative trust fact with a checkable quote. The instructions are stripped before any text reaches the model, and never obeyed.

## Norma
- **Every real Norma finding** (`findings.md`, all from `live_check`):
  - `py-mng-open-no-encoding` (LOW), `vet/report.py`. `read_text("utf-8")` became `read_text(encoding="utf-8")`.
  - `py-perf-double-lookup` (LOW), `vet/verify.py`. Two dictionary lookups became one `by_id.get(fid)`.
  - `fa-mnt-no-typing-param` (MEDIUM), `vet/config.py`. A pydantic validator was replaced with a `Literal["json", "csv"]` field type, with the same behaviour.
  - `py-maint-else-after-return` (HIGH), `vet/collectors/classify.py`. A redundant `else` after `raise` was restructured with an early `continue`.
- **The Norma GitHub App checked PRs #2 to #10.** Each check said "No issues introduced by your changes."
- **Full `live_check` sweep of `main` after PR #10:** 57 Python files, 0 issues. Three config files are `not_checkable`; `uv.lock` was not sent.
- **TODO:** the Norma dashboard full scan, which includes the LLM-based rules.

## Two authors
- **Saurabh Kumar** (with Claude Code) wrote `vet demo` (PR #10):
  - Live progress per collector, then a verdict panel.
  - `--from-run` replays a saved run with no network.
  - 9 offline tests.
  - His `live_check` was refused (`remote_not_matched`), so his files were checked from Aryan's session afterwards. All clean.
- **Author conflict, found in review:** `vet demo` wrote and rendered the run but never called `validate_verdict_file`, unlike `vet run`.
  - Resolution: PR #10 was merged as written. A follow-up added one shared write → validate → render function for both commands, plus a test that `vet demo` reports the same validation problems as `vet run`.
  - Logged under "Author conflicts" in `findings.md`, and in `DEFENCE.md`.

## Claims vs records: three reasons from our test products
- None of our 11 test products had a founding-year claim contradicted by records (0 contradicted claim checks in `runs/`). That case is covered only by the hollow-vendor scenario test.
- The three below show records outweighing what the product's own site shows. Each is quoted exactly as the agent wrote it.
- **Pocket:** the site is still online, but the label is real_alive "no" (run `20261126-140012-9e499a`):
  - "Wikipedia reports that Mozilla announced on May 22, 2025 that it would shut down Pocket on July 8, 2025."
  - https://en.wikipedia.org/wiki/Pocket_(service)
- **Canva:** a polished, popular site, but a breach record holds trust at caution (run `20261126-100243-538e9f`):
  - "Have I Been Pwned lists a breach of Canva dated 2019-05-24 affecting 137,272,116 accounts (exposed: email addresses, geographic locations, names, passwords)"
  - https://haveibeenpwned.com/PwnedWebsites#Canva
- **Cedric:** a new AI SaaS. Its age comes from the domain records, not its pages (run `20261126-140012-9e499a`):
  - "Domain registered 2026-07-01; archived in 1 of the 1 years from 2026 to 2026; first certificate 2026-07-01 (meet-cedric.com)"
  - https://lookup.icann.org/en/lookup?name=meet-cedric.com

## Bugs found by real runs (all fixed and tested)
- **Sister products counted against the vendor:** Cal.eu when judging cal.com, and the Affinity sister brand. A negative now counts only if its quote names the exact product or domain.
- **Credential stuffing counted as a breach.** It is now a security incident. Breach vs. security incident is decided in code by wording rules.
- **A blocked homepage stopped all collectors:** Canva's 403 also stopped the HIBP breach check. Collectors now run on the domain alone, and the site reader falls back to the newest Wayback copy.
- **A namesake's good news counted as evidence:** "Smol has teamed up with Pulpex…" is about a detergent maker, not Smol Launch. Exact-entity matching now also applies to positive facts.
- **A customer's founding year was taken as the vendor's:** Cedric's "founded 2021" came from a blog post about a customer. Founding years from blog, customer, changelog and docs pages are ignored.
- **One refusal lost all classifications:** for Personio, one security report made the model refuse mid-answer and all 16 results were lost. Results are now classified in chunks of 6.
- **A North Data register record was lost** for Projektron: the entity name and HRB number are in the result URL, not the snippet. They are now matched in code.

## Evidence-honesty fixes (from reviewing the report screenshots)
- **"Confirmed" used to mean "any independent page mentions the product name."**
  - Projektron's "over 850 customers" was marked confirmed by profiles that only describe BCS.
  - A claim is now confirmed only when one record states the same number, name, year or identifier.
  - Re-checked offline, Projektron's "850 customers" and "300 references" are unconfirmed: no record states either number.
- **Tavily's own company-profile pages were used as evidence.**
  - Across the saved runs, 29 reasons and 51 claims-table records linked to tavily.com.
  - The fixed code drops these results in the Tavily client.
  - The verifier rejects any fact from a search-tool page.
  - The report's claims table hides them.

## Known limits (named, not built)
- Not detected today: a copied imprint, shelf companies, virtual-office addresses, founder
  history, bought reviews, review blogs owned by the vendor.
- No certification registers are queried: a SOC 2 or ISO badge is reported as a claim, never as
  verified, and never as missing.
- Pages are not rendered: JavaScript-only pages are read as far as their HTML goes, and a page we
  could not read is listed under "Not checked", never held against the vendor.
- The fetcher does not yet block redirects to private or local addresses, and it does not cap
  response size (first fix after the event; see docs/threat-model.md).

## Measured agreement with the organiser's labels (one traffic light per product)
Basic search depth. "Baseline" maps today's two-axis verdict to a light; "categorised" is the
rubric categoriser (VET_BUYER_CATEGORISE=true). Both scored on the same runs.

| Set | Baseline | Categorised | Red recall (base / cat) | False Reds (base / cat) |
|---|---|---|---|---|
| Dev, 12 rows (tuned on, 2 rounds) | 6/12 | 6/12 | 0/4 / 1/4 | 0 / 0 |
| Holdout, 12 rows (4 per label, never tuned on) | 4/12 | 5/12 | 2/4 / 3/4 | 1 / 0 |
| All 40 (seen: the rubric was written from these rows) | 19/40 | 19/40 | 2/10 / 4/10 | 1 / 1 |

Tuning round 3, rubric level, recomputed offline from the saved all-40 records (no new
searches): licensing confusion appears in Green rows as often as in Yellow rows, so it became
context instead of a Yellow driver.

| Variant | All 40 (seen) | Dev 12 | Red recall | Greens kept Green | Greens / Yellows called Red |
|---|---|---|---|---|---|
| Baseline | 19/40 | 6/12 | 2/10 | 7/14 | 0 / 1 |
| Categorised, licensing drives Yellow | 19/40 | 6/12 | 4/10 | 3/14 | 0 / 1 |
| (a) licensing drives Yellow only with 2 facts from 2 independent publishers | 21/40 | 7/12 | 4/10 | 4/14 | 0 / 1 |
| (b) licensing is always context (now in the code) | 22/40 | 7/12 | 4/10 | 6/14 | 0 / 1 |

All-40 numbers are on rows the rubric was written from: they are not a holdout. The decision
rule for switching the categoriser on was: beat 19/40, Red recall at least 4/10, at least as
many Greens kept as the baseline, no Green called Red. Option (b) misses the Greens condition
(6 vs 7), so the blind round ships the baseline mapping (VET_BUYER_CATEGORISE=false). The base
check in the offline recomputation uses the vendor identity and the two-axis "real" call in
place of the model's own identifiable/operating answers, which are not saved.
0 of 79 product runs failed today.

**The categoriser stays off for the blind round.** A live rehearsal with it switched on, on
three products the agent had not seen, gave 2 wrong Reds out of 3, and one product took 134 s
(over our 120 s limit).
