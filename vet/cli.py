"""Command-line entry point. Commands are wired up phase by phase."""

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from vet import __version__

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


@app.command()
def run(
    urls: Annotated[list[str] | None, typer.Argument(help="Product URLs to vet.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File with one URL per line.")
    ] = None,
) -> None:
    """Vet one or more products and write runs/<id>/verdicts.json."""
    _not_ready("run", 2)


@app.command()
def validate(
    verdicts: Annotated[Path, typer.Argument(help="Path to runs/<id>/verdicts.json.")],
) -> None:
    """Check a verdicts file against the schema."""
    _not_ready("validate", 2)


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(help="Path to runs/<id>.")],
    public: Annotated[
        bool, typer.Option("--public", help="Refuse runs that include labelled products.")
    ] = False,
) -> None:
    """Render a static HTML report into site/."""
    _not_ready("report", 3)


@app.command("eval")
def eval_(
    extra_only: Annotated[
        bool, typer.Option("--extra-only", help="Only run eval/extra_set.yaml.")
    ] = False,
) -> None:
    """Score the pipeline against labelled products and write eval.md."""
    _not_ready("eval", 4)
