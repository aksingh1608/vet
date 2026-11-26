"""Command-line entry point. Commands are wired up phase by phase."""

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from vet import __version__
from vet.config import OUTPUT_FORMATS, Settings
from vet.output import (
    ProductRecord,
    find_verdict_file,
    load_records,
    product_slug,
    validate_verdict_file,
    write_run,
)

GIT_TIMEOUT_S = 5
SITE_DIR = Path("site")
REPORT_DIR = "report"  # inside the run directory: private, never published
EXTRA_SET = Path("eval") / "extra_set.yaml"
LABELLED_DIR = Path("data") / "labelled"

app = typer.Typer(
    name="vet",
    help="Vendor due diligence: is this product real and alive, and can you trust the vendor?",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(stderr=True)


def _git_sha() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


def _read_urls(urls: list[str] | None, file: Path | None) -> list[str]:
    out = list(urls or [])
    if file is not None:
        lines = file.read_text(encoding="utf-8").splitlines()
        out += [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    return out


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"vet {__version__}")
        raise typer.Exit


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(RichHandler(console=console, show_path=False))
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Per-request HTTP logs go to the trace, not the console.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


def _settings(advanced: bool, output_format: str | None) -> Settings:
    settings = Settings.from_env()
    updates: dict[str, object] = {}
    if advanced:
        updates["tavily_advanced"] = True
    if output_format:
        if output_format.lower() not in OUTPUT_FORMATS:
            console.print(f"[red]--format must be one of {', '.join(OUTPUT_FORMATS)}[/red]")
            raise typer.Exit(code=2)
        updates["output_format"] = output_format.lower()
    return settings.model_copy(update=updates) if updates else settings


def _summary_table(title: str, records: list[ProductRecord]) -> Table:
    table = Table(title=title)
    for col in (
        "product",
        "real_alive",
        "trust",
        "conf",
        "reasons",
        "trust reason",
        "findings",
        "secs",
        "tavily credits",
        "errors",
    ):
        table.add_column(col)
    for r in records:
        v = r.verdict
        table.add_row(
            v.product_url,
            v.real_alive,
            v.trust,
            f"{v.confidence:.2f}",
            str(len(v.reasons)),
            "yes" if any(r.axis == "trust" for r in v.reasons) else "no",
            str(len(v.findings)),
            f"{r.seconds:.0f}",
            str(r.usage.tavily_credits),
            str(len(r.errors)),
        )
    return table


def _merge(existing: list[ProductRecord], new: list[ProductRecord]) -> list[ProductRecord]:
    """Replace products already in the run (by slug), keep the rest, append new ones."""
    fresh = {product_slug(r.verdict.product_url): r for r in new}
    merged = [fresh.pop(product_slug(r.verdict.product_url), r) for r in existing]
    return merged + list(fresh.values())


@app.command()
def run(
    urls: Annotated[list[str] | None, typer.Argument(help="Product URLs to vet.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File with one URL per line.")
    ] = None,
    advanced: Annotated[
        bool, typer.Option("--advanced", help="Tavily advanced search (2 credits per search).")
    ] = False,
    into: Annotated[
        Path | None,
        typer.Option("--into", help="Rerun these products inside an existing runs/<id>."),
    ] = None,
    output_format: Annotated[
        str | None, typer.Option("--format", help="Verdict file format: json or csv.")
    ] = None,
) -> None:
    """Vet products: verdict file + HTML report in runs/<id>/, summary table here."""
    from vet.orchestrator import run_many
    from vet.report import render

    targets = _read_urls(urls, file)
    if not targets:
        console.print("[red]Give at least one URL or --file.[/red]")
        raise typer.Exit(code=2)
    if into is not None and not into.is_dir():
        console.print(f"[red]{into} is not a run directory.[/red]")
        raise typer.Exit(code=2)
    settings = _settings(advanced, output_format)
    started = time.monotonic()
    run_id, records, traces = asyncio.run(
        run_many(targets, settings, _git_sha(), run_id=into.name if into else None)
    )
    run_dir = into or settings.runs_dir / run_id
    if into is not None:
        records = _merge(load_records(into), records)
    verdicts_path = write_run(run_dir, records, traces, settings)
    problems = validate_verdict_file(verdicts_path, settings)
    report = render(run_dir, run_dir / REPORT_DIR)

    console.print(_summary_table(f"run {run_id}", records))
    console.print(f"verdicts: {verdicts_path}")
    console.print(f"report:   {report[-1]}")
    if problems:
        for problem in problems:
            console.print(f"[red]✗[/red] {problem}")
    else:
        console.print(f"[green]✓[/green] verdict file valid ({time.monotonic() - started:.0f}s)")


@app.command()
def demo(
    url: Annotated[
        str | None,
        typer.Argument(help="Product URL. Optional with --from-run when the run has one product."),
    ] = None,
    from_run: Annotated[
        Path | None,
        typer.Option("--from-run", help="Replay a saved runs/<id> without the network."),
    ] = None,
    advanced: Annotated[bool, typer.Option("--advanced", help="Tavily advanced search.")] = False,
) -> None:
    """Vet one product with live progress, then show the verdict panel (for the pitch)."""
    from vet.demo import DemoError, pick_record, replay, run_live

    if from_run is not None:
        if not from_run.is_dir():
            console.print(f"[red]{from_run} is not a run directory.[/red]")
            raise typer.Exit(code=2)
        try:
            record = pick_record(load_records(from_run), url)
        except (DemoError, ValueError) as exc:
            console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from exc
        replay(record, console, from_run)
        return
    if not url:
        console.print("[red]Give a URL, or --from-run RUN_DIR.[/red]")
        raise typer.Exit(code=2)
    settings = _settings(advanced, None)
    _, run_dir = run_live(url, settings, _git_sha(), console, REPORT_DIR)
    console.print(f"run:    {run_dir}")
    console.print(f"report: {run_dir / REPORT_DIR}")


@app.command()
def validate(
    verdicts: Annotated[Path, typer.Argument(help="runs/<id> or its verdict file.")],
) -> None:
    """Check a verdict file (JSON or CSV) against the schema and the label mapping."""
    path = find_verdict_file(verdicts) if verdicts.is_dir() else verdicts
    problems = validate_verdict_file(path, Settings.from_env())
    if problems:
        for problem in problems:
            console.print(f"[red]✗[/red] {problem}")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] {path} is valid")


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(help="Path to runs/<id>.")],
    public: Annotated[
        bool, typer.Option("--public", help="Refuse runs that include labelled products.")
    ] = False,
) -> None:
    """Render a static HTML report into site/."""
    from vet.report import PublicExportRefused, render

    try:
        written = render(run_dir, SITE_DIR, public=public)
    except PublicExportRefused as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✓[/green] wrote {len(written)} pages to {SITE_DIR}/")


@app.command("eval")
def eval_(
    labels: Annotated[
        list[Path] | None,
        typer.Option("--labels", "-l", help="Label file(s) or folder(s). Default: both sets."),
    ] = None,
    extra_only: Annotated[
        bool, typer.Option("--extra-only", help="Only run eval/extra_set.yaml.")
    ] = False,
    advanced: Annotated[bool, typer.Option("--advanced", help="Tavily advanced search.")] = False,
) -> None:
    """Run the labelled products and write runs/<id>/eval.md."""
    from vet.evaluate import score, write_eval
    from vet.labels import LabelError, label_files, load_labels
    from vet.orchestrator import run_many

    settings = _settings(advanced, None)
    sources = [EXTRA_SET] if extra_only else (labels or [LABELLED_DIR, EXTRA_SET])
    files = label_files(*sources)
    try:
        loaded = [label for f in files for label in load_labels(f, settings)]
    except LabelError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    labelled = [label for label in loaded if label.labelled]
    if not labelled:
        console.print(
            f"[yellow]No labelled products in {', '.join(map(str, files)) or 'any file'} "
            f"({len(loaded)} placeholders).[/yellow]"
        )
        raise typer.Exit(code=2)
    run_id, records, traces = asyncio.run(
        run_many([label.url for label in labelled], settings, _git_sha())
    )
    run_dir = settings.runs_dir / run_id
    write_run(run_dir, records, traces, settings)
    result = score(records, labelled)
    path = write_eval(run_dir, result, [str(f) for f in files])
    console.print(_summary_table(f"eval {run_id}", records))
    for axis in (result.real_alive, result.trust):
        console.print(f"{axis.axis}: {axis.correct}/{axis.total} ({axis.accuracy:.0%})")
    console.print(f"wrote {path} ({len(result.misses)} misses)")
