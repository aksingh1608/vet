# vet

A vendor due-diligence agent. Give it a software product's URL; it returns:

- **real_alive**: is the product real and still being built? (`yes` / `unclear` / `no`)
- **trust**: would you trust the company with your data and money? (`trust` / `caution` / `do_not_trust`)
- **reasons**: 4–7 short reasons, strongest first, each with a link to a page that says what the reason claims.

The vendor's own site only supplies *claims to check*. Evidence comes from independent sources and records:
domain registration, certificate logs, web archive history, company registers, breach records, third-party
mentions, review sites and release activity.

Built for the Stackgini "Agents with Taste" challenge (AI Agents Berlin, 27 Nov 2026). See `SPEC.md` for
the full design and `AGENTS.md` for working rules.

## Setup

```bash
uv sync                 # Python 3.12, installs vet + dev tools
cp .env.example .env    # then fill in the API keys
```

## Usage

```bash
uv run vet --help
uv run vet run https://example.com            # Phase 2
uv run vet run --file urls.txt                # Phase 3, up to 5 in parallel
uv run vet validate runs/<id>/verdicts.json   # Phase 2
uv run vet report runs/<id>                   # Phase 3, static HTML into site/
uv run vet eval                               # Phase 4
```

## Development

```bash
uv run pytest           # unit tests, no network
uv run ruff check .
uv run ruff format .
```

## Network etiquette

All network traffic goes through `vet/fetch.py`: at most 1 request/second per host by default, GET only and
robots.txt respected for websites, 15 s timeout, retries with backoff, an identifying User-Agent and a
SQLite cache. vet never submits forms, logs in, or contacts vendors.

## Data

`data/labelled/` and `runs/` are gitignored. The organiser's examples and our verdicts on their products are
never committed or published.
