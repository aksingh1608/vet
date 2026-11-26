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
from vet.config import Settings
from vet.output import VERDICTS_FILE, validate_verdict_file, write_run

GIT_TIMEOUT_S = 5
SITE_DIR = Path("site")

app = typer.Typer(
    name="vet",
    help="Vendor due diligence: is this product real and alive, and can you trust the vendor?",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(stderr=True)


def _not_ready(command: str, phase: int) -> None:
    console.print(f"[yellow]`vet {command}` arrives in Phase {phase}.[/yellow]")
    raise typer.Exit(code=2)


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


@app.command()
def run(
    urls: Annotated[list[str] | None, typer.Argument(help="Product URLs to vet.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File with one URL per line.")
    ] = None,
) -> None:
    """Vet one or more products and write runs/<id>/verdicts.json."""
    from vet.orchestrator import run_many

    targets = _read_urls(urls, file)
    if not targets:
        console.print("[red]Give at least one URL or --file.[/red]")
        raise typer.Exit(code=2)
    settings = Settings.from_env()
    started = time.monotonic()
    run_id, records, traces = asyncio.run(run_many(targets, settings, _git_sha()))
    verdicts_path = write_run(settings.runs_dir / run_id, records, traces, settings)

    table = Table(title=f"run {run_id}")
    for col in (
        "product",
        "real_alive",
        "trust",
        "conf",
        "reasons",
        "secs",
        "findings",
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
            f"{r.seconds:.0f}",
            str(len(v.findings)),
            str(r.usage.tavily_credits),
            str(len(r.errors)),
        )
    console.print(table)
    console.print(f"wrote {verdicts_path} in {time.monotonic() - started:.0f}s")


@app.command()
def validate(
    verdicts: Annotated[Path, typer.Argument(help="Path to runs/<id>/verdicts.json.")],
) -> None:
    """Check a verdicts file against the schema."""
    path = verdicts / VERDICTS_FILE if verdicts.is_dir() else verdicts
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
    extra_only: Annotated[
        bool, typer.Option("--extra-only", help="Only run eval/extra_set.yaml.")
    ] = False,
) -> None:
    """Score the pipeline against labelled products and write eval.md."""
    _not_ready("eval", 4)
