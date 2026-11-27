"""Batch overview: one local HTML page for a whole run (RUN_DIR/report/overview.html).

Built only from the run's verdict file: the stackgini CSV (one traffic light per input row),
or ``verdicts.json`` when no CSV exists. No network, no keys. The page is self-contained (inline
SVG chart, inline styles from base.html.j2, no scripts, fonts or CDNs) and stays inside the run
directory, which is gitignored: it is never published.

Text from the verdict file is escaped by Jinja2's autoescaping, and only http(s) links are
rendered as links; anything else is shown as plain text.
"""

import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from vet.buyer_view import FLAG_SEPARATOR, NO_FLAGS, baseline_overall
from vet.config import Settings
from vet.output import STACKGINI_CSV, VERDICTS_FILE, product_slug
from vet.schemas import Overall, Polarity, Verdict

logger = logging.getLogger(__name__)

REPORT_DIR = "report"
OVERVIEW_FILE = "overview.html"
HIBP_URL = "https://haveibeenpwned.com"
ORDER = (Overall.GREEN, Overall.YELLOW, Overall.RED)
# Chart geometry (SVG user units).
CHART_WIDTH = 640
BAR_HEIGHT = 34
BAR_GAP = 14
LABEL_WIDTH = 90
COUNT_WIDTH = 60
_FLAG_LINK = re.compile(r"\s*\((?P<kind>[^():]+): (?P<url>\S+)\)$")
_RUN_ID = re.compile(r"^(\d{8})-(\d{6})")


@dataclass
class Row:
    id: str
    url: str
    product: str
    vendor: str
    overall: Overall | None  # None: a label the overview does not know
    label: str
    flag_text: str = ""
    flag_url: str = ""
    report_href: str = ""

    @property
    def url_ok(self) -> bool:
        return is_http(self.url)

    @property
    def flag_url_ok(self) -> bool:
        return is_http(self.flag_url)


@dataclass
class Bar:
    overall: Overall
    label: str
    count: int
    y: int
    width: int


def is_http(url: str) -> bool:
    return url.lower().startswith(("https://", "http://"))


def first_flag(cell: str) -> tuple[str, str]:
    """(text, url) of the first flag in a stackgini flag cell, ("", "") when there is none."""
    cell = (cell or "").strip()
    if not cell or cell == NO_FLAGS:
        return "", ""
    flag = cell.split(FLAG_SEPARATOR, 1)[0].strip()
    m = _FLAG_LINK.search(flag)
    if m is None:
        return flag, ""
    return flag[: m.start()].strip(), m.group("url")


def _overall(label: str, settings: Settings) -> Overall | None:
    wanted = label.strip().lower()
    for overall, shown in settings.overall_labels.items():
        if shown.lower() == wanted or overall.value == wanted:
            return overall
    return None


def rows_from_csv(path: Path, settings: Settings) -> list[Row]:
    text = path.read_text(encoding="utf-8-sig")
    rows: list[Row] = []
    for raw in csv.DictReader(io.StringIO(text, newline="")):
        label = (raw.get("overall") or "").strip()
        overall = _overall(label, settings)
        flag_text, flag_url = first_flag(raw.get(f"{overall.value}_flags", "") if overall else "")
        rows.append(
            Row(
                id=(raw.get("id") or "").strip(),
                url=(raw.get("url") or "").strip(),
                product=(raw.get("product") or "").strip(),
                vendor=(raw.get("vendor") or "").strip(),
                overall=overall,
                label=label or "unknown",
                flag_text=flag_text,
                flag_url=flag_url,
            )
        )
    return rows


def _unlabel(value: str, labels: dict[Any, str]) -> str:
    """The internal enum value behind an organiser label (or the value itself)."""
    for internal, shown in labels.items():
        if shown == value:
            return str(internal.value)
    return value


def rows_from_verdicts(path: Path, settings: Settings) -> list[Row]:
    """Two-axis verdicts.json: the traffic light is the buyer view's baseline mapping; the flag
    is the first reason of the matching polarity (a finding first for Red)."""
    rows: list[Row] = []
    for data in json.loads(path.read_text(encoding="utf-8")):
        data = {
            **data,
            "real_alive": _unlabel(str(data.get("real_alive", "")), settings.real_alive_labels),
            "trust": _unlabel(str(data.get("trust", "")), settings.trust_labels),
        }
        verdict = Verdict.model_validate(data)
        overall = baseline_overall(verdict)
        flag_text, flag_url = "", ""
        if overall == Overall.RED and verdict.findings:
            flag_text, flag_url = verdict.findings[0].text, verdict.findings[0].source_url
        else:
            want = Polarity.POSITIVE if overall == Overall.GREEN else Polarity.NEGATIVE
            reason = next((r for r in verdict.reasons if r.polarity == want), None)
            if reason is not None:
                flag_text, flag_url = reason.text, reason.source_url
        rows.append(
            Row(
                id="",
                url=verdict.product_url,
                product="",
                vendor="",
                overall=overall,
                label=settings.overall_labels[overall],
                flag_text=flag_text,
                flag_url=flag_url,
            )
        )
    return rows


def load_rows(run_dir: Path, settings: Settings) -> tuple[list[Row], Path]:
    csv_path = run_dir / STACKGINI_CSV
    if csv_path.exists():
        return rows_from_csv(csv_path, settings), csv_path
    json_path = run_dir / VERDICTS_FILE
    if json_path.exists():
        return rows_from_verdicts(json_path, settings), json_path
    raise FileNotFoundError(f"no {STACKGINI_CSV} or {VERDICTS_FILE} in {run_dir}")


def report_page(row: Row, report_dir: Path) -> str:
    """The product's existing report page (same name as vet.report writes), relative to the
    overview, or "" when there is none."""
    slug = product_slug(row.url)
    names = [f"{slug}.html"]
    if row.id:
        names.insert(0, f"{re.sub(r'[^A-Za-z0-9_-]+', '-', row.id)}--{slug}.html")
    return next((n for n in names if (report_dir / n).is_file()), "")


def run_date(run_dir: Path, verdict_file: Path) -> str:
    """From the run id (``YYYYMMDD-HHMMSS-…``, UTC), else the verdict file's time."""
    m = _RUN_ID.match(run_dir.name)
    if m:
        when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    else:
        when = datetime.fromtimestamp(verdict_file.stat().st_mtime, tz=UTC)
    return when.strftime("%Y-%m-%d %H:%M UTC")


def bars(rows: list[Row], settings: Settings) -> list[Bar]:
    counts = {o: sum(1 for r in rows if r.overall == o) for o in ORDER}
    top = max(counts.values(), default=0) or 1
    span = CHART_WIDTH - LABEL_WIDTH - COUNT_WIDTH
    return [
        Bar(
            overall=o,
            label=settings.overall_labels[o],
            count=counts[o],
            y=i * (BAR_HEIGHT + BAR_GAP),
            width=round(span * counts[o] / top),
        )
        for i, o in enumerate(ORDER)
    ]


def _env() -> Environment:
    return Environment(
        loader=PackageLoader("vet", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_overview(run_dir: Path, settings: Settings | None = None) -> Path:
    """Write RUN_DIR/report/overview.html and return its path."""
    settings = settings or Settings.from_env()
    rows, source = load_rows(run_dir, settings)
    out_dir = run_dir / REPORT_DIR
    for row in rows:
        row.report_href = report_page(row, out_dir)
    chart = bars(rows, settings)
    html = (
        _env()
        .get_template("overview.html.j2")
        .render(
            run_id=run_dir.name,
            run_date=run_date(run_dir, source),
            source=source.name,
            rows=rows,
            bars=chart,
            total=len(rows),
            chart_width=CHART_WIDTH,
            chart_height=len(chart) * (BAR_HEIGHT + BAR_GAP) - BAR_GAP,
            bar_height=BAR_HEIGHT,
            label_width=LABEL_WIDTH,
            hibp_url=HIBP_URL,
        )
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / OVERVIEW_FILE
    path.write_text(html, encoding="utf-8")
    logger.info("overview: %d products from %s -> %s", len(rows), source.name, path)
    return path
