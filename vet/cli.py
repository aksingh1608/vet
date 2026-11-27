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
    InputRow,
    ProductRecord,
    find_verdict_file,
    load_records,
    read_inputs,
    record_key,
    validate_stackgini_file,
    validate_verdict_file,
    write_run,
)

GIT_TIMEOUT_S = 5
DEFAULT_BLIND_PRODUCTS = 20
CREDITS_PER_PRODUCT_ADVANCED = 35
CREDITS_PER_PRODUCT_BASIC = 18
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


def _read_urls(urls: list[str] | None, file: Path | None) -> list[InputRow]:
    out = [InputRow(url=u) for u in urls or []]
    if file is not None:
        out += read_inputs(file)
    return out


def _input_key(row: InputRow) -> str:
    from vet.orchestrator import normalize_url

    return f"id:{row.id}" if row.id else f"url:{normalize_url(row.url)}"


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
        "id",
        "product",
        "overall",
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
            r.input.id if r.input else "",
            v.product_url,
            r.buyer.overall.value if r.buyer else "-",
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
    """Replace products already in the run (by id or URL), keep the rest, append new ones."""
    fresh = {record_key(r): r for r in new}
    merged = [fresh.pop(record_key(r), r) for r in existing]
    return merged + list(fresh.values())


def _in_input_order(records: list[ProductRecord], rows: list[InputRow]) -> list[ProductRecord]:
    """Records in input order; records of products not in the input stay at the end."""
    rank = {_input_key(row): i for i, row in enumerate(rows)}

    def position(r: ProductRecord) -> int:
        key = record_key(r)
        if r.input is not None and not r.input.id:
            key = _input_key(r.input)
        return rank.get(key, len(rank))

    return sorted(records, key=position)


def _save_run(
    run_dir: Path, records: list[ProductRecord], traces: list, settings: Settings
) -> tuple[Path, list[str], list[Path]]:
    """Write the run, validate its verdict file and render the private report.

    The one path every command that produces a run goes through (``run`` and ``demo``).
    """
    from vet.report import render

    verdicts_path = write_run(run_dir, records, traces, settings)
    problems = validate_verdict_file(verdicts_path, settings)
    report = render(run_dir, run_dir / REPORT_DIR)
    return verdicts_path, problems, report


def _print_problems(problems: list[str], started: float) -> None:
    if problems:
        for problem in problems:
            console.print(f"[red]✗[/red] {problem}")
    else:
        console.print(f"[green]✓[/green] verdict file valid ({time.monotonic() - started:.0f}s)")


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
        str | None,
        typer.Option("--format", help="Verdict file format: json, csv or stackgini."),
    ] = None,
    missing_only: Annotated[
        bool,
        typer.Option("--missing-only", help="With --into: run only inputs that have no record."),
    ] = False,
) -> None:
    """Vet products: verdict file + HTML report in runs/<id>/, summary table here.

    --file takes a .txt (one URL per line) or a CSV with url and optional id, product, vendor.
    """
    from vet.orchestrator import new_run_id, run_many

    targets = _read_urls(urls, file)
    if not targets:
        console.print("[red]Give at least one URL or --file.[/red]")
        raise typer.Exit(code=2)
    if into is not None and not into.is_dir():
        console.print(f"[red]{into} is not a run directory.[/red]")
        raise typer.Exit(code=2)
    if missing_only and into is None:
        console.print("[red]--missing-only needs --into runs/<id>.[/red]")
        raise typer.Exit(code=2)
    settings = _settings(advanced, output_format)
    started = time.monotonic()
    run_id = into.name if into else new_run_id()
    run_dir = into or settings.runs_dir / run_id
    existing = load_records(into) if into is not None else []
    todo = targets
    if missing_only:
        have = {record_key(r) for r in existing} | {
            _input_key(r.input) for r in existing if r.input is not None
        }
        todo = [t for t in targets if _input_key(t) not in have]
        console.print(f"{len(targets) - len(todo)} done already, running {len(todo)}")
    records: list[ProductRecord] = []
    traces: list = []
    if todo:
        run_id, records, traces = asyncio.run(
            run_many(todo, settings, _git_sha(), run_id=run_id, run_dir=run_dir)
        )
    if into is not None:
        records = _merge(existing, records)
    records = _in_input_order(records, targets)
    verdicts_path, problems, report = _save_run(run_dir, records, traces, settings)
    if settings.output_format == "stackgini":
        problems += validate_stackgini_file(verdicts_path, settings, targets)

    console.print(_summary_table(f"run {run_id}", records))
    console.print(f"verdicts: {verdicts_path}")
    console.print(f"report:   {report[-1]}")
    _print_problems(sorted(set(problems)), started)


@app.command()
def credits(
    products: Annotated[
        int, typer.Option("--products", help="Blind products to reserve credits for.")
    ] = DEFAULT_BLIND_PRODUCTS,
) -> None:
    """Tavily credits used and left, and how many blind products the rest covers."""
    from vet.fetch import Fetcher
    from vet.tavily import Tavily

    settings = Settings.from_env()
    if settings.tavily_api_key is None:
        console.print("[red]TAVILY_API_KEY is not set.[/red]")
        raise typer.Exit(code=2)

    async def ask() -> tuple[int, int | None]:
        async with Fetcher(settings) as fetcher:
            return await Tavily(settings, fetcher).usage()

    used, limit = asyncio.run(ask())
    if limit is None:
        console.print(f"Tavily credits used: {used}; limit unknown")
        return
    left = limit - used
    console.print(f"Tavily credits used: {used} of {limit}; left: {left}")
    for depth, per in (
        ("advanced", CREDITS_PER_PRODUCT_ADVANCED),
        ("basic", CREDITS_PER_PRODUCT_BASIC),
    ):
        reserve = per * products
        console.print(
            f"  {depth}: {per}/product, {products} blind products need {reserve}; "
            f"left covers {left // per} products"
        )


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
    from vet.demo import DemoError, pick_record, replay, run_live, verdict_panel

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
    started = time.monotonic()
    run_id, records, traces = run_live(url, settings, _git_sha(), console)
    run_dir = settings.runs_dir / run_id
    verdicts_path, problems, report = _save_run(run_dir, records, traces, settings)
    console.print(verdict_panel(records[0]))
    console.print(f"verdicts: {verdicts_path}")
    console.print(f"report:   {report[-1]}")
    _print_problems(problems, started)


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
    ids: Annotated[
        str | None, typer.Option("--ids", help="Comma-separated label ids to run (e.g. 4,7,21).")
    ] = None,
    into: Annotated[
        Path | None,
        typer.Option("--into", help="Resume an eval run: only labels without a record run."),
    ] = None,
) -> None:
    """Run the labelled products and write runs/<id>/eval.md."""
    from vet.evaluate import overall_table, score, write_eval
    from vet.labels import LabelError, label_files, load_labels
    from vet.orchestrator import new_run_id, run_many

    settings = _settings(advanced, "stackgini")
    sources = [EXTRA_SET] if extra_only else (labels or [LABELLED_DIR, EXTRA_SET])
    files = label_files(*sources)
    try:
        loaded = [label for f in files for label in load_labels(f, settings)]
    except LabelError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    labelled = [label for label in loaded if label.labelled]
    if ids:
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        by_id = {label.id: label for label in labelled if label.id}
        missing = [i for i in wanted if i not in by_id]
        if missing:
            console.print(f"[red]✗[/red] unknown label ids: {', '.join(missing)}")
            raise typer.Exit(code=2)
        labelled = [by_id[i] for i in wanted]
    if not labelled:
        console.print(
            f"[yellow]No labelled products in {', '.join(map(str, files)) or 'any file'} "
            f"({len(loaded)} placeholders).[/yellow]"
        )
        raise typer.Exit(code=2)
    rows = [
        InputRow(url=label.url, id=label.id, product=label.product, vendor=label.vendor)
        for label in labelled
    ]
    run_id = into.name if into else new_run_id()
    run_dir = into or settings.runs_dir / run_id
    existing = load_records(run_dir) if into is not None else []
    have = {record_key(r) for r in existing}
    todo = [r for r in rows if _input_key(r) not in have]
    records: list[ProductRecord] = []
    traces: list = []
    if todo:
        run_id, records, traces = asyncio.run(
            run_many(todo, settings, _git_sha(), run_id=run_id, run_dir=run_dir)
        )
    records = _in_input_order(_merge(existing, records), rows)
    write_run(run_dir, records, traces, settings)
    result = score(records, labelled)
    path = write_eval(run_dir, result, [str(f) for f in files])
    console.print(_summary_table(f"eval {run_id}", records))
    for axis in (result.real_alive, result.trust):
        if axis.total:
            console.print(f"{axis.axis}: {axis.correct}/{axis.total} ({axis.accuracy:.0%})")
    if result.overall.total:
        for line in overall_table(result):
            console.print(line, markup=False)
    seconds = [r.seconds for r in records]
    credits_used = sum(r.usage.tavily_credits for r in records)
    if seconds:
        console.print(
            f"seconds/product: mean {sum(seconds) / len(seconds):.0f}, max {max(seconds):.0f}; "
            f"tavily credits this run: {credits_used}"
        )
    console.print(f"wrote {path} ({len(result.misses)} axis misses)")
