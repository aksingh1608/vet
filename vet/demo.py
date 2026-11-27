"""`vet demo`: one product with live progress in the terminal, then a verdict panel.

Display only. The live run is the normal pipeline (``run_many``); the CLI then writes,
validates and reports it the same way ``vet run`` does. Progress comes from the trace events
the pipeline already emits. A replay reads a saved run directory and makes no network calls.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from vet import trace
from vet.config import Settings
from vet.orchestrator import (
    AFTER_SITE,
    BUYER_ONLY_COLLECTORS,
    collectors_after_site,
    normalize_url,
    run_many,
)
from vet.output import TRACE_FILE, ProductRecord, product_slug
from vet.report import safe_url
from vet.schemas import Polarity, RealAlive, Trust

SITE_STEP = "site_reader"
AGE_STEP = "age"
JUDGE_STEP = "judge + verify"
# Default steps: the buyer-only collectors run only with VET_BUYER_CATEGORISE=true.
COLLECTOR_STEPS = [SITE_STEP, AGE_STEP, *(k for k in AFTER_SITE if k not in BUYER_ONLY_COLLECTORS)]
LIVE_REFRESH_PER_S = 12

RUNNING, OK, FAILED, WAITING = "running", "ok", "failed", "waiting"

REAL_ALIVE_STYLE = {
    RealAlive.YES: "bold green",
    RealAlive.UNCLEAR: "bold yellow",
    RealAlive.NO: "bold red",
}
TRUST_STYLE = {
    Trust.TRUST: "bold green",
    Trust.CAUTION: "bold yellow",
    Trust.DO_NOT_TRUST: "bold red",
}
POLARITY_MARK = {Polarity.POSITIVE: "+", Polarity.NEGATIVE: "-"}


class DemoError(Exception):
    """The demo cannot start (bad run directory, product not in the run)."""


@dataclass
class Step:
    name: str
    state: str = WAITING
    seconds: float = 0.0
    detail: str = ""
    started: float = 0.0


class Progress:
    """Collector rows driven by trace events. Renders as a Rich table."""

    def __init__(self, steps: list[str] | None = None) -> None:
        self.steps = {name: Step(name) for name in [*(steps or COLLECTOR_STEPS), JUDGE_STEP]}
        self._judge_from_t: object = None

    def _step(self, name: str) -> Step:
        if name not in self.steps:
            # Keep the judge row last when a collector we did not expect shows up.
            judge = self.steps.pop(JUDGE_STEP)
            self.steps[name] = Step(name)
            self.steps[JUDGE_STEP] = judge
        return self.steps[name]

    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "collector_start":
            step = self._step(str(event.get("collector", "?")))
            step.state, step.started = RUNNING, time.monotonic()
        elif kind == "collector_end":
            step = self._step(str(event.get("collector", "?")))
            self._finish(step, float(event.get("seconds", 0.0)), str(event.get("error", "")))
        elif kind == "claims_check":
            step = self.steps[JUDGE_STEP]
            step.state, step.started = RUNNING, time.monotonic()
            self._judge_from_t = event.get("t")
        elif kind == "product_done":
            judge = self.steps[JUDGE_STEP]
            # Trace times work for live runs and replays; the wall clock is the fallback.
            t, from_t = event.get("t"), self._judge_from_t
            if isinstance(t, int | float) and isinstance(from_t, int | float):
                seconds = t - from_t
            else:
                seconds = time.monotonic() - judge.started
            self._finish(judge, round(seconds, 2), "")

    @staticmethod
    def _finish(step: Step, seconds: float, error: str) -> None:
        step.state = FAILED if error else OK
        step.seconds, step.detail = seconds, error

    def finish(self, record: ProductRecord) -> None:
        """Settle every row from the saved record (covers collectors cancelled at the deadline)."""
        for result in record.collectors:
            self._finish(self._step(result.name), result.seconds, result.error)
        judge = self.steps[JUDGE_STEP]
        if judge.state != OK:  # no trace to time it: show it done, without a time
            judge.state, judge.seconds = OK, -1.0

    def __rich__(self) -> RenderableType:
        table = Table.grid(padding=(0, 2))
        table.add_column(width=2)
        table.add_column(min_width=16)
        table.add_column(justify="right", min_width=7)
        table.add_column(overflow="ellipsis", no_wrap=True, max_width=60)
        for step in self.steps.values():
            if step.state == RUNNING:
                icon: RenderableType = Spinner("dots", style="cyan")
                secs = Text(f"{time.monotonic() - step.started:.1f}s", style="dim")
            elif step.state == OK:
                shown = f"{step.seconds:.1f}s" if step.seconds >= 0 else "–"
                icon, secs = Text("✓", style="green"), Text(shown)
            elif step.state == FAILED:
                icon, secs = Text("✗", style="red"), Text(f"{step.seconds:.1f}s")
            else:
                icon, secs = Text("·", style="dim"), Text("")
            table.add_row(icon, Text(step.name), secs, Text(step.detail, style="dim red"))
        return table


OVERALL_STYLE = {"green": "green", "yellow": "yellow", "red": "red"}
MAX_PANEL_FLAGS = 6


def verdict_panel(record: ProductRecord) -> Panel:
    """The closing panel: product, both calls in colour, reasons with their links.

    Everything from the run is built as Text, never as markup, so page content cannot
    style or hide itself in the terminal.
    """
    v = record.verdict
    name = record.identity.product_name or record.identity.vendor_name
    header = Text()
    header.append(name or v.product_url, style="bold")
    if name:
        header.append(f"  {v.product_url}", style="dim")
    calls = Text()
    calls.append("real_alive ")
    calls.append(f" {v.real_alive.value} ", style=f"reverse {REAL_ALIVE_STYLE[v.real_alive]}")
    calls.append("   trust ")
    calls.append(f" {v.trust.value} ", style=f"reverse {TRUST_STYLE[v.trust]}")
    calls.append(f"   confidence {v.confidence:.2f}", style="dim")

    parts: list[RenderableType] = [header]
    if record.buyer is not None:
        light = Text()
        light.append(
            f" {record.buyer.overall.value.upper()} ",
            style=f"reverse {OVERALL_STYLE[record.buyer.overall.value]}",
        )
        light.append(f"  buyer action: {record.buyer.action}", style="bold")
        parts += [light, Text(record.buyer.reasoning)]
        for flag in record.buyer.flags[:MAX_PANEL_FLAGS]:
            parts.append(Text(f"[{flag.colour.value}] {flag.text}  {flag.source_url}"))
    parts.append(calls)
    if v.summary:
        parts.append(Text(v.summary, style="italic"))
    parts.append(Text("Reasons", style="bold underline"))
    for i, reason in enumerate(v.reasons, start=1):
        mark = POLARITY_MARK.get(reason.polarity, "·")
        line = Text(f"{i}. ", style="bold")
        line.append(f"[{mark} {reason.axis.value}] ", style="dim")
        line.append(reason.text)
        url = safe_url(reason.source_url)
        link = Text("   ")
        link.append(reason.source_url, style=f"link {url} blue" if url != "#" else "dim")
        parts += [line, link]
    if v.findings:
        parts.append(Text("Findings", style="bold underline red"))
        for finding in v.findings:
            parts.append(Text(f"! {finding.text}", style="red"))
            parts.append(Text(f"   {finding.source_url}", style="dim"))
    footer = (
        f"{record.seconds:.0f}s · {record.usage.tavily_credits} Tavily credits · run {v.run_id}"
    )
    return Panel(Group(*parts), title="vet", subtitle=footer, border_style="cyan", expand=True)


def pick_record(records: list[ProductRecord], url: str | None) -> ProductRecord:
    if not records:
        raise DemoError("the run has no saved products")
    if url is None:
        if len(records) == 1:
            return records[0]
        urls = ", ".join(r.verdict.product_url for r in records)
        raise DemoError(f"the run has {len(records)} products; name one of: {urls}")
    slug = product_slug(normalize_url(url))
    for record in records:
        if product_slug(record.verdict.product_url) == slug:
            return record
    raise DemoError(f"{url} is not in this run")


def saved_events(run_dir: Path, product_url: str) -> list[dict[str, Any]]:
    """This product's events from the run's trace.jsonl (empty when there is none)."""
    path = run_dir / TRACE_FILE
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("product") == product_url:
            events.append(event)
    return events


def replay(record: ProductRecord, console: Console, run_dir: Path) -> None:
    """Show a saved product as if it had just run, from its record and trace. No network."""
    progress = Progress([r.name for r in record.collectors])
    for event in saved_events(run_dir, record.verdict.product_url):
        progress.handle(event)
    progress.finish(record)
    console.print(progress)
    console.print(verdict_panel(record))


def run_live(
    url: str, settings: Settings, git_sha: str, console: Console
) -> tuple[str, list[ProductRecord], list[trace.Trace]]:
    """Run the normal pipeline on one product, showing collector progress as it happens."""
    progress = Progress([SITE_STEP, AGE_STEP, *collectors_after_site(settings)])
    token = trace.set_listener(progress.handle)
    try:
        with Live(progress, console=console, refresh_per_second=LIVE_REFRESH_PER_S):
            run_id, records, traces = asyncio.run(run_many([url], settings, git_sha))
            progress.finish(records[0])
    finally:
        trace.reset_listener(token)
    return run_id, records, traces
