# RUNBOOK: live round, Friday 27 Nov 2026, 19:00–19:45

One laptop runs `vet`. There is exactly one blind-round procedure, below. Runs stay in `runs/`
(gitignored); the **only** blind-round file that is committed is `stackgini-verdicts.csv` at the
repo root, copied exactly as vet wrote it (organiser rule; see AGENTS.md). Never
`vet report --public` a run.

## 1. 18:30 Checks

```bash
cd ~/vet
git checkout main && git pull --ff-only
uv sync
uv run ruff check . && uv run pytest -q          # must be green
grep -cE '^(ANTHROPIC|TAVILY)_API_KEY=.+' .env   # must print 2 (COMPANIES_HOUSE_API_KEY, GITHUB_TOKEN optional)
uv run vet credits --products 10                 # credits left; advanced depth needs about 35 per product
uv run vet run https://basecamp.com              # smoke test: writes runs/<id>/stackgini.csv
uv run vet validate runs/<smoke-test-id>         # must print ✓
```

Settings that apply by default (nothing to set): output format `stackgini` (CSV, one row per
input), traffic light from the baseline mapping (`VET_BUYER_CATEGORISE=false`; the categoriser
stays off, see README "Measured results"). If the organiser confirms different columns, set
`VET_STACKGINI_COLUMNS=...` in `.env`.

## 2. 19:00 Blind round: exact steps (verdicts in the public repo by 19:45)

The organiser sends 10 products as a CSV with `id,url,product,vendor`. They grade the repo as it
is at 19:45, so `stackgini-verdicts.csv` must be merged on `main` before then.

```bash
cd ~/vet && git checkout main && git pull --ff-only      # latest fixes
# 1. Save the email attachment as ~/blind.csv (id,url,product,vendor).
uv run vet run --file ~/blind.csv --advanced --format stackgini
uv run vet validate runs/<id>                            # must print ✓
python3 -c "import csv;print(sum(1 for _ in csv.DictReader(open('runs/<id>/stackgini.csv',encoding='utf-8'))))"   # must print 10
cp runs/<id>/stackgini.csv stackgini-verdicts.csv        # never edit it by hand
git checkout -b results/stackgini-verdicts
git add stackgini-verdicts.csv
git commit -m "Blind-round verdicts (vet run <id>)"
git push -u origin results/stackgini-verdicts
gh pr create --base main --title "Blind-round verdicts" --body "stackgini-verdicts.csv from runs/<id>, written by vet, unedited."
# wait for the Norma check, then:
gh pr merge --merge                                      # before 19:40
```

- **Row count is not 10:** check that the first line of `~/blind.csv` is exactly
  `id,url,product,vendor` (lowercase, comma-separated). Only the **input** file may be fixed;
  then run the steps again. The verdict file is never edited.
- **Norma check not started 3 minutes after the PR opens:** merge anyway and say so on the PR:
  `gh pr comment --body "Merged before the Norma check started (19:45 deadline)."`
- A failed product still gets a valid Yellow row that says "run failed"; resume it (section 3)
  if there is time.

## 3. Resume a failed or interrupted product

Each product is saved to `runs/<id>/products/` as soon as it is done.

```bash
# The run stopped (crash, network, laptop): run only the products that have no record yet.
uv run vet run --file ~/blind.csv --advanced --format stackgini --into runs/<id> --missing-only

# One product failed or looks wrong: delete its record, then run the same command.
rm runs/<id>/products/<input-id>--*.json
uv run vet run --file ~/blind.csv --advanced --format stackgini --into runs/<id> --missing-only
```

Always pass the full `~/blind.csv`, so the file keeps one row per input in input order. Then
validate again and continue with the `cp` step of section 2.

## 4. 19:40 Check and submit

```bash
git fetch origin && git show origin/main:stackgini-verdicts.csv | head -3   # must show the header and rows
```

Or open https://github.com/aksingh1608/vet/blob/main/stackgini-verdicts.csv in a browser: the
file must be visible at the top level of `main`. Then submit the form at
https://aiagentsberlin.xyz/hackathons/build/submit (challenge: Stackgini; repo link:
https://github.com/aksingh1608/vet).

## 5. Fallback, only if vet itself breaks

1. Run the same command (section 2) on `main` again, into a fresh run.
2. If that is impossible, commit whatever **valid** verdict file exists
   (`uv run vet validate` must pass) as `stackgini-verdicts.csv`, and add one line to README.md
   on how to read it. Then merge and submit as in section 4.

## If a source is down

Every collector fails on its own; the run never crashes. The product still gets a verdict
from what did answer, and the error is shown in the run's summary table (`errors`) and the report.

| Symptom | What happens | What to do |
|---|---|---|
| Tavily errors or no credits left | footprint, risk and legal_id return nothing; verdicts are thinner (more "unclear"/"caution") | Drop `--advanced` (half the credits). Check credits in the Tavily dashboard. Cached products cost 0 on rerun |
| Anthropic API errors | The judge falls back to the deterministic rules (confidence 0.3); the site reader cannot extract claims | Wait a minute, then resume the affected products (section 3) |
| crt.sh slow or down | Optional: age continues with RDAP and Wayback | Nothing |
| Wayback slow | age may time out (45 s); the other collectors still run | Resume the product (section 3) |
| A vendor site answers 403 or 429 | The homepage and key pages (legal, about, customers, pricing) are read from the newest Wayback capture with status 200; sources show the snapshot URL and "archived copy of <date>". No capture: no claims, but records, search and breach checks still run | Nothing; the `errors` column names the archived copy used |
| A rerun must ask the model again (e.g. after a prompt change) | LLM answers are cached by exact input, so reruns are reproducible and free | `VET_LLM_CACHE=false uv run vet run ... --into runs/<id>` |
| Log line `a chunk of N results failed` | The model refused or cut off one chunk of search results (seen once: a security report about the vendor); only that chunk is lost | Nothing; resume the product (section 3) if its flags look thin |
| Whole run too slow | Products past the budget are judged on what was collected | Raise `VET_PRODUCT_BUDGET_S` (default 180) or split `~/blind.csv` into two runs |

## Switch to the phone hotspot

If the venue Wi-Fi drops or blocks APIs:

1. Turn on the hotspot on the phone (or plug in USB tethering).
2. Connect:

   ```bash
   nmcli device wifi list                                   # find the phone's SSID
   nmcli device wifi connect "<SSID>" password "<password>"
   # USB tethering instead: nmcli device   (look for usb0, then)
   nmcli device connect usb0
   ```

3. Check the APIs answer:

   ```bash
   curl -sI https://api.tavily.com | head -1
   curl -sI https://api.anthropic.com | head -1
   curl -sI https://web.archive.org | head -1
   ```

4. Rerun only the products that failed (section 3). Everything already fetched
   is in the cache (`.cache/fetch.sqlite`) and is not fetched again.

## Demo: one product of our choice

```bash
uv run vet run https://the-product.example --advanced
xdg-open runs/<id>/report/index.html
```

Show the traffic light, the buyer action and the flags first (click one link), then the two-axis
detail and the collector table at the bottom (what we checked and how long it took).
