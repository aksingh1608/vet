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

Set the organiser's format in `.env` as soon as they publish it (no code change needed):

```bash
VET_OUTPUT_FORMAT=json                 # or csv
VET_REAL_ALIVE_LABELS={"yes": "...", "unclear": "...", "no": "..."}
VET_TRUST_LABELS={"trust": "...", "caution": "...", "do_not_trust": "..."}
```

Then run `uv run vet validate runs/<smoke-test-id>` to confirm the labels map.

## 19:00 Products arrive

1. Put one URL per line in `blind.txt` (lines starting with `#` are ignored).
2. Run everything, in parallel (5 at a time), with Tavily advanced search:

   ```bash
   uv run vet run --file blind.txt --advanced
   ```

   Expect about 1 minute per batch of 5 (3 minutes is the per-product budget). It prints a
   summary table, the verdict file path, the report path, and whether the file is valid.
   Advanced search costs 2 credits per search: about 15–20 credits per product.

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
uv run vet validate runs/<id>        # must print ✓
ls runs/<id>/verdicts.*              # the file to submit (json or csv)
```

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
| A vendor site answers 403 | No claims from the site; records, search and breach checks still run | Nothing; mention it if asked |
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
