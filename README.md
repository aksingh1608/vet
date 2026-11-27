# vet

**vet checks a software vendor's claims against independent records and gives a buyer one traffic
light (Green, Yellow or Red) with sourced flags and a short reasoning.**

## Two challenges, one repo

| Challenge | What is judged | Where to look |
|---|---|---|
| **Stackgini "Agents with Taste"** | The calls (Green / Yellow / Red), the reasons and their links | [For Stackgini judges](#for-stackgini-judges): why the approach is different → measured results |
| **Norma "Ship agent code your team can defend"** | Team policy and consistency, the accepted tradeoff, findings fixed, Production-Ready Score | [For Norma judges](#for-norma-judges), [DEFENCE.md](DEFENCE.md) |

> **In 60 seconds**
> - **What:** give vet product URLs. For each one it reads the vendor site as *claims*, then
>   collects *evidence* from records and independent sources, verifies every cited source, and
>   writes one row of the verdict CSV.
> - **Built for:** Stackgini "Agents with Taste" (vendor due diligence) and Norma (governed AI coding).
> - **Measured** against the organiser's 40 labelled products (one traffic light each): **19/40**
>   on all 40, **4/12** on a holdout we never tuned on, **0 Greens called Red**.
>   The 40 rows are the ones the rubric was written from (details below).
> - **Speed:** cold cache at advanced depth, **60–68 s and 20–26 search credits per product**,
>   4 products in parallel.
> - **One command:** `uv run vet run --file input.csv --advanced --format stackgini`

## How to read stackgini-verdicts.csv

`stackgini-verdicts.csv` (repo root) holds the blind-round verdicts, exactly as `vet` wrote them.
It is UTF-8 CSV with one row per input product, in input order.

| Column | Content |
|---|---|
| `id`, `url`, `product`, `vendor` | Copied from the organiser's input |
| `overall` | `Green`, `Yellow` or `Red` |
| `green_flags`, `yellow_flags`, `red_flags` | Flags separated by ` \| `; `None identified.` when there are none |
| `reasoning` | A short summary of the call, then the buyer action |

**Flags:**
- Each flag is one sentence, ending with its source type and link: `… (press: https://…)`.
- A flag whose page refuses automated access (HTTP 403/429) was verified from the cached search
  snippet. It ends `(<type>, snippet-verified; page blocks automated access: https://…)`.
- Every flag comes from a verified fact. The writer drops any flag without an openable http(s)
  link.

**Reasoning:**
- The summary itself contains no links; every source is in the flags.
- If pages could not be read, the reasoning ends with `Not checked: …`. That clause names those
  pages and why, and it can contain their URLs.

## For Stackgini judges

### Why this approach is different

| Choice | Where in the code |
|---|---|
| **Evidence over looks.** Site design never decides a label: the `looks` facts are low-weight filler, excluded from every score and used only as last-resort reasons. | `vet/collectors/looks.py`, `vet/judge.py` |
| **Every reason is verified against its source.** The quote must be on the page or in the machine record, a model checks that it supports the sentence, and anything that fails is dropped. | `vet/verify.py` |
| **Labels are decided in code, not by the model.** The model ranks fact ids; code rules decide the two axes and the traffic light. | `vet/judge.py`, `vet/buyer_view.py` |
| **Page text is hostile input.** Hidden text and text addressed to AI are removed before any model sees a page, and the attempt itself becomes a red flag. | `vet/guard.py`, `tests/fixtures/redteam/` |
| **Honest measurement.** Baseline vs categoriser, a holdout, and live rehearsals on unseen products. We kept the categoriser **off** because the live rehearsal failed it. | [Measured results](#measured-results) |

### Input → output

Input (`docs/examples/input.csv`) and the verdict file `vet` wrote for it
(`docs/examples/output.csv`, the exact blind-round command, 27 Nov):

| id | url | overall | first green flag (from output.csv) |
|---|---|---|---|
| ex2 | https://www.projektron.de | Green | North Data has a register page for Projektron GmbH (Amtsgericht Charlottenburg HRB 79669). |
| ex3 | https://zammad.com | Green | North Data has a register page for Zammad GmbH (Charlottenburg HRB 163946 B). |

ex3 also carries a yellow flag, kept because it is verified: SecurityWeek reports that two Zammad
zero-days were exploited to breach DIVD, a Zammad user, not Zammad's own systems. Every flag ends
with its source type and link. Output columns: `id,url,product,vendor,overall,green_flags,yellow_flags,red_flags,reasoning`.

The batch overview (`vet overview`) for that run:

![Batch overview: two products, both Green, first flag and report link per row](docs/img/overview.png)

A **Red** example, on a **synthetic test site** (no real company, rendered from the test fixtures
with no network): a domain registered six weeks ago, a founding year claimed as 2015, no register
entry, no independent mentions. This is the two-axis report page (no / do not trust); the buyer
view maps it to Red.

![Report for the synthetic hollow vendor: not real, do not trust](docs/img/hollow-scenario.png)

### How it works

```mermaid
flowchart LR
    A[input.csv<br/>id, url] --> B[site reader<br/>vendor pages = claims]
    B --> G[guard<br/>hidden + AI-addressed text removed,<br/>attempts become red flags]
    G --> C{collectors in parallel}
    C --> C1[age<br/>RDAP, crt.sh, Wayback]
    C --> C2[legal_id<br/>company registers]
    C --> C3[footprint<br/>reviews, customers, press]
    C --> C4[risk<br/>breaches, lawsuits, shutdowns]
    C --> C5[alive<br/>GitHub, PyPI, npm, App Store]
    C --> C6[identity<br/>entity, traction, logo wall]
    C --> C7[archive<br/>earliest Wayback copy]
    C --> C8[corporate<br/>regulators, owners, licensing]
    C --> C9[looks<br/>filler only]
    C1 & C2 & C3 & C4 & C5 & C6 & C7 & C8 & C9 --> D[claims check<br/>claims vs records]
    D --> E[judge model<br/>ranks fact ids]
    E --> F[verification<br/>quote on page + support check]
    F --> H[labels decided in code]
    H --> I[buyer view<br/>Green / Yellow / Red,<br/>flags, reasoning]
    I --> J[stackgini.csv]
    I --> K[HTML report]
    I --> L[overview.html]
```

**The model writes, the code decides.**

`corporate` runs only when the categoriser is on (`VET_BUYER_CATEGORISE=true`, off by default).

### What we check

| Signal family | Collector | Source type |
|---|---|---|
| What the vendor says (founding year, customers, certifications) | `site_reader` | vendor (claims, never evidence in its favour) |
| Domain registration, first certificate, archive history | `age` | record (RDAP, crt.sh, Wayback CDX) |
| Earliest archived homepage: same vendor or a different site | `archive` | record (Wayback snapshot) |
| Legal entity, register number | `legal_id` | record (Companies House API; North Data via search snippets) |
| Reviews, named customers, press, marketplaces | `footprint` | independent (Tavily search, quote must be in the result) |
| Breaches, lawsuits, fines, insolvency, shutdowns | `risk` | record (Have I Been Pwned) and press |
| Release activity of matching accounts | `alive` | record (GitHub, PyPI, npm, iTunes lookup) |
| Legal entity on the site, traction consistency, logo wall, policies, "coming soon" | `identity` | vendor pages (contradictions observed by code) |
| Regulator and court findings, ownership, licensing, sanctions, lifecycle statements | `corporate` | regulator, court, press; vendor statements against interest |
| Claims vs records (e.g. founding year vs domain age) | `claims_check` | computed from the above |
| Attempts to steer an AI evaluator | `guard` | the page itself (hard red flag) |

Breach data: [Have I Been Pwned](https://haveibeenpwned.com).

### AI security layer

Full model: [docs/threat-model.md](docs/threat-model.md).

- **Trust boundaries.** Internet → `fetch.py` → guard → `<untrusted_content>` block → model →
  fact ids → code rules → verified output. Each boundary has its own control.
- **The guard** (`vet/guard.py`):
  - What it scans: visible text, CSS-hidden elements, comments, `alt`/`title` attributes and
    `<meta>` tags.
  - What it removes: hidden elements and every paragraph that addresses an AI evaluator.
  - Tag escape: an `</untrusted_content>` inside a page is escaped, so it cannot close the data
    block.
  - The attempt itself is recorded as a hard red flag.
- **Red-team fixtures** (`tests/fixtures/redteam/`, 19 tests in `tests/test_guard.py`). Five
  vendor homepages:
  - visible "note to AI evaluators" text;
  - CSS-hidden text;
  - comments and meta tags;
  - alt/title and white-on-white text;
  - a tag-escape attempt.
  Every payload carries a marker word that the tests prove never reaches the model. Ordinary AI
  product copy and an `llms.txt` link are *not* flagged.
- **Least privilege for the model.** No tools and no network. It returns labels within the
  allowed set and **fact ids**, never sources or URLs of its own. Ids that do not exist are
  dropped, and quotes must appear verbatim on the page they cite.
- **Read-only fetcher** (`vet/fetch.py`). Every network call goes through it:
  - GET only, except POST where an API requires it;
  - robots.txt respected for websites;
  - a per-host rate limit (Wayback ≤ 20/min, crt.sh ≤ 4/min, Tavily ≤ 100/min);
  - 15 s timeouts and a cache;
  - never forms, logins, or contact with vendors.
- **No secrets in logs.** Keys come only from `.env`, are sent only in headers, and never appear
  in URLs, cache keys, logs or traces.

### Measured results

The organiser sent 40 labelled products. We score agreement on the single traffic light; the
labelled data never leaves this machine. Basic search depth; the baseline (what ships) maps the
two-axis verdict to a light.

| Set | Baseline (ships) | Categoriser | Red recall (base / cat) | Greens called Red (base / cat) |
|---|---|---|---|---|
| Dev, 12 rows (tuned on) | 6/12 | 7/12 | 0/4 / 1/4 | 0 / 0 |
| Holdout, 12 rows (4 per label, never tuned on, run once) | **4/12** | 5/12 | 2/4 / 3/4 | 0 / 0 |
| All 40, **seen** (the rubric was written from these rows) | **19/40** | 22/40 | 2/10 / 4/10 | 0 / 0 |

The categoriser column for dev and all 40 is the final rule (licensing confusion as context),
recomputed offline from saved runs. The holdout figure is from before that change.

**Why the categoriser is off.** It scores higher on the labelled rows. But a live rehearsal with
it switched on, on 3 products the agent had not seen, gave **2 wrong Reds of 3**, and one product
took **134 s** (our limit is 120 s). So the blind round uses the baseline.

**Speed and cost** (blind-round settings: baseline, advanced depth):

| Run | Seconds per product | Credits per product | Failed |
|---|---|---|---|
| Rehearsal, 3 unseen products, cold cache | 60–68 | 20–26 | 0 of 3 |
| Example run, 3 products (2 with warm cache; 2 shown above) | 22–61 | 8–26 | 0 of 3 |

0 failed products across all of 27 Nov's runs. If one fails, the batch keeps going and that row
says "run failed: ...".

## For Norma judges

In Norma's scoring order. Full record: [DEFENCE.md](DEFENCE.md) and [findings.md](findings.md).

### 1. Team policy and consistency (30)

Two organisation policies in the Norma workspace "Vet", checked on every dependency manifest and
lockfile. Details and the rule-level policy: [docs/norma-policy.md](docs/norma-policy.md).

| Policy | Rule id | Severity | Why | Proposed by |
|---|---|---|---|---|
| No second HTTP client, no browser automation (banned: `requests`, `aiohttp`, `playwright`, `selenium`) | `policy-banned-deps-a5f2-1.0` | High | Every network call goes through `vet/fetch.py`, which enforces robots.txt, rate limits and the cache. vet is read-only and never renders pages, fills forms or logs in. | Saurabh: `requests`, `aiohttp`. Both: browser automation |
| Permissive licences only (MIT, BSD-2/3-Clause, Apache-2.0, PSF-2.0, ISC) | `policy-licence-a9e3-1.0` | Medium | No licence obligations we cannot review. Dry run: 0 violations. | Both |

![Norma workspace "Vet": the two policies, both on the vet repository with 0 open issues](docs/img/norma-policies.png)

Consistency between the two authors: both agents worked against the same rules. The one place
the authors' code differs (whether a collector's facts reach the two-axis judge) is recorded in
[DEFENCE.md](DEFENCE.md#policy-and-consistency).

### 2. The tradeoff we accepted (30)

**The per-product catch (a Norma AI finding).**
- **The finding:** on 27 Nov Norma's AI review flagged the per-product catch in
  `vet/orchestrator.py` (`run_product_safe`): High, `error-handling-layer-1.0`, "A failure is
  caught and nobody ever finds out".
- **What we accepted:** the catch itself. One failed product must never stop a batch; a blind
  round must return one row per input.
- **What we fixed:** only the visibility half. A failure now shows in five places, all checked
  by `test_a_failed_product_is_visible_everywhere`:
  1. an ERROR log line with the exception type;
  2. a `product_failed` trace event;
  3. the record's errors;
  4. the row's reasoning ("run failed: ...");
  5. "N of M products failed" in the CLI summary.
- **Result:** after that change the AI review no longer reports it ("No AI findings on your last
  scan, 27/11/2026 16:01 UTC"). The broad catch remains in the code by design.

**The snippet fallback (self-identified, not a Norma finding).**
- **What it does:** when a cited page refuses automated access (403/429), its quote is verified
  from the cached search snippet. That was 119 of 876 flags on 27 Nov.
- **Kept and marked:** each such flag says "snippet-verified; page blocks automated access".
- **Fix after the event:** verify these pages through archive copies.

Record: [DEFENCE.md, Accepted](DEFENCE.md#accepted), [findings.md](findings.md).

### 3. Found and fixed (20)

- **Our own policy caught a real dependency:** the banned-packages policy flagged an unused
  optional extra `browser = ["playwright>=1.45"]` in `pyproject.toml` (score 96, 1 High).
  - No code imported it.
  - We removed it and refreshed `uv.lock` in PR #21; the score went from 96 to 100.
- **Earlier findings from `live_check`, all fixed** ([findings.md](findings.md)):

| Rule | File | Severity |
|---|---|---|
| `py-maint-else-after-return` | `vet/collectors/classify.py` | High |
| `fa-mnt-no-typing-param` | `vet/config.py` | Medium |
| `py-mng-open-no-encoding` | `vet/report.py` | Low |
| `py-perf-double-lookup` | `vet/verify.py` | Low |

### 4. Production-Ready Score (20)

100/100 (Gold) on `main`: 6 rulesets, 158 rules, 0 issues, 100% in every functional area.

![Norma Production-Ready Score for vet on main: 100/100 Gold, 0 issues](docs/img/norma-score-100.png)

### Required items

- [x] **01. Both agents connected to Norma through MCP.** Both authors' Claude Code sessions
  were connected to the Norma MCP server, and `.mcp.json` connects every agent that opens the
  repo (URL only, no token). The `live_check` results are logged in [findings.md](findings.md).
- [x] **02. Pull requests per author.**
  - Aryan Kumar Singh: #1–9, #11–13, #16, #17, #19–24, and this README PR.
  - Saurabh Kumar: #10, #14, #15, #18.
  - The Norma GitHub check ran on every PR.
- [x] **03. A fix:** PR #21 (playwright extra, policy score 96 → 100); also the four rule
  findings above.
- [x] **04. A policy:** the two policies above, written down in
  [docs/norma-policy.md](docs/norma-policy.md).
- **Author conflict:** both authors registered new collectors in the same lines of
  `vet/orchestrator.py`; each merge kept all of them. See
  [findings.md, Author conflicts](findings.md#author-conflicts) and
  [DEFENCE.md](DEFENCE.md#conflict-between-authors).

## Quickstart

```bash
uv sync                                   # Python 3.12
cp .env.example .env                      # then fill in ANTHROPIC_API_KEY and TAVILY_API_KEY
                                          # (COMPANIES_HOUSE_API_KEY optional)
uv run vet run --file input.csv --advanced --format stackgini   # CSV with columns id,url
uv run vet validate runs/<id>             # one row per input, valid labels and links
uv run vet overview runs/<id>             # writes runs/<id>/report/overview.html
```

Outputs go to `runs/<id>/` (gitignored):
- `stackgini.csv` (the verdict file) and `stackgini.json`;
- one JSON record per product in `products/`;
- `trace.jsonl`;
- the HTML reports in `report/`.

If a run is interrupted, resume it with `--into runs/<id> --missing-only`. The full live-round
procedure is in [RUNBOOK.md](RUNBOOK.md).

## Code map

| Path | What it does | Tests |
|---|---|---|
| `vet/cli.py` | Commands: `run`, `eval`, `validate`, `report`, `overview`, `demo`, `credits` | 10 (`test_demo`), 7 (`test_overview`) |
| `vet/orchestrator.py` | One product end to end within a time budget; batch survival | 4 (`test_orchestrator`), 14 (`test_buyer_view`) |
| `vet/fetch.py` | The only HTTP client: robots.txt, per-host limits, timeouts, retries, cache | 26 |
| `vet/guard.py` | Hidden and AI-addressed text: remove, then flag | 19 |
| `vet/site_reader.py` | Reads the vendor's pages into claims; Wayback fallback for blocked pages | 18, 2 (`test_archive_fallback`) |
| `vet/collectors/` | age 5, alive and looks 5, legal_id 8, footprint 2 + 9 (`test_recall`), risk 26, identity 8, archive 10, corporate 8, classify 1 | 82 |
| `vet/claims_check.py` | Claims against records (confirmed, unconfirmed, contradicted) | 11 |
| `vet/judge.py` | Two-axis labels decided in code; the model ranks fact ids | 24, 35 (`test_scenarios`) |
| `vet/verify.py` | Re-checks every reason and finding; reason selection | 9, 24 (`test_selection`) |
| `vet/buyer_view.py` | Traffic light (baseline mapping), flags, failed-run row | in `test_buyer_view` |
| `vet/categorise.py` | Rubric categoriser (off by default) | 44 |
| `vet/output.py` | `stackgini.csv`, validation, per-product records | 6 |
| `vet/report.py`, `vet/overview.py` | HTML report and batch overview (autoescaped, http(s) links only) | 7, 7 |
| `vet/labels.py`, `vet/evaluate.py` | Labelled-set loader and eval harness | 6, 1 |
| `vet/llm.py`, `vet/tavily.py`, `vet/schemas.py` | Model wrapper (cached, structured), search client, data contracts | 4, 5, 12 |

**Total: 370 tests**, all offline.

## Design decisions

| Decision | Alternative we rejected | Why |
|---|---|---|
| Labels decided by code rules | Let the model choose the label | Reproducible, and a page cannot argue the model into a label |
| Model returns fact ids only | Model writes citations | It cannot invent a source; every link comes from a collected fact |
| One fetcher for all traffic | An HTTP client per collector | robots.txt, rate limits and the cache are enforced in one place (and by a Norma policy) |
| Review sites, North Data and LinkedIn read only via search snippets | Scraping them | They block or forbid bots |
| LLM answers cached by input | Temperature 0 | The current models take no temperature setting; the cache makes reruns identical |
| Keep the per-product catch, surface the failure | Let one error stop the batch | A blind round must always produce one row per input |
| Baseline traffic light in the blind round | Categoriser on | The live rehearsal gave 2 wrong Reds of 3 and one product took 134 s |
| A young domain is never a negative in the buyer view | Penalise new domains | Rebrands and carve-outs get new domains |
| A dated website never decides a label | Score design quality | The labelled data shows looks do not drive the call |

## Tech stack

| Tool | Why |
|---|---|
| Python 3.12, asyncio | Collectors and products run concurrently within one time budget |
| httpx | One async client, behind `vet/fetch.py` |
| pydantic v2 | Every fact, verdict and model answer is validated |
| Anthropic API (judge and fast model) | Structured outputs; the fast model classifies, the judge ranks and writes |
| Tavily | Search with cached responses, so reruns cost 0 credits |
| trafilatura | Main-text extraction from vendor pages |
| Jinja2 | HTML report and overview with autoescaping |
| SQLite | Fetch and LLM caches |
| Typer + Rich | CLI and live progress |
| uv, ruff, pytest | Reproducible installs, lint, 370 offline tests |
| Norma (MCP + GitHub App) | Rule checks while coding and on every PR; team policies |

## Limits and next steps

- **Not detected yet** (from Saurabh's review): a copied imprint, shelf companies, virtual-office
  addresses, founder history, bought reviews, review blogs owned by the vendor, and pages that
  need JavaScript rendering.
- **Snippet-verified flags.** When a page refuses automated access (403/429), its quote is
  verified from the cached search snippet, and the flag says "snippet-verified; page blocks
  automated access". That was 119 of the 876 flags in the runs of 27 Nov. Planned fix: verify
  these pages through an archive copy.
- **No certification registers.** A SOC 2 or ISO badge is reported as a claim, never as verified
  and never as missing.
- **Fetcher hardening comes first after the event.** Redirects to private or local addresses are
  not blocked yet, and response size is not capped.
- **Categoriser:** fix the two false-Red patterns the rehearsal exposed (a third party ending its
  own service; an old major version's end of life), then re-measure on unseen products.
- **A false flag found and fixed:** in one example run, the newest date on a changelog was misread,
  because main-text extraction had dropped the dated headings. Dates are now read from the whole
  page with hidden text removed (`vet/collectors/alive.py`, with tests). That example row was not
  published.

## Team

Team **"Vet"**: Aryan Kumar Singh and Saurabh Kumar, each working with Claude Code.

## Data policy

The organiser's labelled data and our verdicts on their products are never in this repo:
`data/labelled/` and `runs/` are gitignored and were never committed. The tests use invented
companies only. Secrets come only from `.env`, which is never committed.
