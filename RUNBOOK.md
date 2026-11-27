# RUNBOOK: live round, Friday 27 Nov 2026, 19:00–19:45

One laptop runs `vet`; one person reads the report. Blind products and our verdicts on them
stay in `runs/` (gitignored). Never commit them, never `vet report --public` them.

## 18:30 Before the round

```bash
cd ~/vet
git checkout main && git pull --ff-only
uv sync
uv run ruff check . && uv run pytest -q          # must be green
grep -cE '^(ANTHROPIC|TAVILY|COMPANIES_HOUSE)_API_KEY=.+' .env   # must print 3
uv run vet run https://basecamp.com               # smoke test, ~30 s, cached: 0 credits
```

The submission is the **stackgini** format (CSV, confirmed by Stackgini; it is the default): one traffic light (Green / Yellow / Red) per input
row with green, yellow and red flags and a reasoning. Columns default to
`id,url,product,vendor,overall,green_flags,yellow_flags,red_flags,reasoning`; if the organiser
confirms different columns, set them in `.env` (no code change):

```bash
VET_OUTPUT_FORMAT=stackgini
VET_STACKGINI_COLUMNS=id,url,product,vendor,overall,green_flags,yellow_flags,red_flags,reasoning
```

Check credits before the round: the blind round at advanced depth needs about 35 credits per
product (2,500 are reserved).

```bash
uv run vet credits --products 20       # prints used, left, and products covered per depth
```

The traffic light comes from the **baseline mapping** of the two-axis verdict by default
(`VET_BUYER_CATEGORISE=false`). After the last tuning round (licensing confusion is context,
never a Yellow driver) the categorised view scored 22/40 against the baseline's 19/40, with Red
recall 4/10 against 2/10, but it kept 6 of 14 Greens against the baseline's 7. The decision rule
required at least as many Greens kept, so the categoriser stays off. To use the categorised view instead (corporate searches + categoriser; about 4–6
more searches per product), set in `.env`:

```bash
VET_BUYER_CATEGORISE=true
```

The two-axis format still works as the fallback (`--format json` or `csv`):

```bash
VET_OUTPUT_FORMAT=json                 # or csv
VET_REAL_ALIVE_LABELS={"yes": "...", "unclear": "...", "no": "..."}
VET_TRUST_LABELS={"trust": "...", "caution": "...", "do_not_trust": "..."}
```

Then run `uv run vet validate runs/<smoke-test-id>` to confirm the labels map.

## 19:00 Blind round: exact steps (organiser rule: verdicts in the public repo by 19:45)

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

If the Norma check has not started 3 minutes after the PR opens, merge anyway and add a comment
to the PR saying so (`gh pr comment --body "Merged before the Norma check started (19:45 deadline)."`).
If a product fails or the run is interrupted, resume with
`--into runs/<id> --missing-only`. A failed product still gets a valid Yellow row that says
"run failed".

## 19:00 Products arrive

1. Save the organiser's list as `blind.csv` (columns `url` and, when given, `id`, `product`,
   `vendor`; ids are carried through) or as `blind.txt` with one URL per line.
2. Run everything, in parallel (4 at a time), with Tavily advanced search. **This is the exact
   blind-round command** (confirmed by Stackgini: the verdict file is CSV, submitted together
   with the GitHub repo link):

   ```bash
   uv run vet run --file blind.csv --advanced --format stackgini
   ```

   It writes `runs/<id>/stackgini.csv` (the file to submit; `stackgini.json` next to it is the
   same rows for us). `stackgini` is also the default format, so `--format stackgini` only makes
   it explicit.

   Each product is saved to `runs/<id>/products/` the moment it is done. If the run is
   interrupted (crash, laptop, network), resume it; only products without a record run again:

   ```bash
   uv run vet run --file blind.csv --advanced --format stackgini --into runs/<id> --missing-only
   ```

   One product that fails never stops the batch: it gets a Yellow row whose reasoning says the
   run failed and what was not checked. If the trace or log shows Tavily 429 answers, lower
   parallelism to 3 (`VET_MAX_PARALLEL_PRODUCTS=3` in `.env`) and resume with `--missing-only`.

   Rehearsal on 26 Nov (5 unseen products, cold cache, `--advanced`): **73 s wall time,
   74 Tavily credits (12–18 per product)**, valid file, no crash. It prints a summary table
   (including whether each product has a trust reason), the verdict file path, the report path,
   and whether the file is valid.

## 19:05 Check the table

For every row, look at **reasons** (must be 4–7), **errors**, and **secs**.

- A product with errors, fewer than 4 reasons, or a verdict that looks wrong: rerun just
  that one (next section).
- Open the report and read the reasons and findings of every product. Click a few links:

  ```bash
  xdg-open runs/<id>/report/index.html
  ```

## Rerun a single product

```bash
uv run vet run https://the-product.example --into runs/<id> --advanced
```

This replaces that product in the same run: verdict file, product JSON, trace and report are
all rewritten, and the file is validated again. The other products are kept as they are.
Tavily answers are cached, so a rerun only pays for searches it has not made before.

## 19:30 Submit

```bash
uv run vet validate runs/<id>        # must print ✓ (one row per input, label one of three,
                                     # every flag link http(s) and not a search tool, reasoning)
ls runs/<id>/stackgini.csv           # the file to submit (stackgini.json is the same rows)
```

Submit `runs/<id>/stackgini.csv` together with the repo link: https://github.com/aksingh1608/vet

**Fallback** if the new code misbehaves: `git checkout pre-event-stable` (the last two-axis
version before the buyer view), rerun with `--format json`, and submit that.

Submit that file. Keep the terminal output and `runs/<id>/` until the results are announced.
The deadline is **19:45**: submit what you have at 19:35 at the latest, then rerun if time
is left.

## If a source is down

Every collector fails on its own; the run never crashes. The product still gets a verdict
from what did answer, and the error is shown in the `errors` column and the report.

| Symptom | What happens | What to do |
|---|---|---|
| Tavily errors or no credits left | footprint, risk and legal_id return nothing; verdicts are thinner (more "unclear"/"caution") | Drop `--advanced` (half the credits). Check credits in the Tavily dashboard. Cached products cost 0 on rerun |
| Anthropic API errors | The judge falls back to the deterministic rules (confidence 0.3); the site reader cannot extract claims | Wait a minute and rerun the affected products with `--into` |
| crt.sh slow or down | Optional: age continues with RDAP and Wayback | Nothing |
| Wayback slow | age may time out (45 s); the other collectors still run | Rerun the product with `--into` |
| A vendor site answers 403 or 429 | The homepage and key pages (legal, about, customers, pricing) are read from the newest Wayback capture with status 200; sources show the snapshot URL and "archived copy of <date>". No capture: no claims, but records, search and breach checks still run | Nothing; the `errors` column names the archived copy used |
| A rerun must ask the model again (e.g. after a prompt change) | LLM answers are cached by exact input, so reruns are reproducible and free | `VET_LLM_CACHE=false uv run vet run ... --into runs/<id>` |
| Log line `a chunk of N results failed` | The model refused or cut off one chunk of search results (seen once: a security report about the vendor); only that chunk is lost | Nothing; rerun the product with `--into` if it ends with fewer than 4 reasons |
| Whole run too slow | Products past the budget are judged on what was collected | Raise `VET_PRODUCT_BUDGET_S` (default 180) or split `blind.txt` into two runs |

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

4. Rerun only the products that failed, with `--into runs/<id>`. Everything already fetched
   is in the cache (`.cache/fetch.sqlite`) and is not fetched again.

## Top-3 live run

```bash
echo "https://the-live-product.example" > live.txt
uv run vet run --file live.txt --advanced
xdg-open runs/<id>/report/index.html
```

Walk the room through the report: verdict, reasons with links (click one), findings, and the
collector table at the bottom (what we checked and how long it took).
