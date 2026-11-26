"""Static HTML report: one page per product plus an index, rendered with Jinja2.

Everything shown comes from a run directory (products/*.json). Text from vendor pages and
third-party sites is escaped by Jinja2's autoescaping, and only http(s) links are rendered, so
a page cannot inject markup or a javascript: link into the report.

``public=True`` refuses a run that includes any product listed under data/labelled/ (the
organiser's examples are never published).
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, select_autoescape

from vet.output import ProductRecord, find_verdict_file, load_records, product_slug
from vet.schemas import Axis, Fact, Polarity
from vet.sources import is_search_tool_url
from vet.text import registered_domain

logger = logging.getLogger(__name__)

LABELLED_DIR = Path("data") / "labelled"
HIBP_URL = "https://haveibeenpwned.com"
_DOMAIN = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.I)


# Presentation only: which facts the report shows where. Nothing here changes a verdict.
RED_FLAG_TOPICS = frozenset({"breach", "contradicted_claim", "prompt_injection"})
TIMELINE_TOPICS = (
    ("domain_registered", "Domain registered"),
    ("first_certificate", "First certificate"),
    ("first_capture", "First archive capture"),
)
MAX_CLAIM_RECORDS = 3


class PublicExportRefused(RuntimeError):
    """The run includes products from the labelled set."""


def safe_url(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else "#"


def source_domain(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return registered_domain(host) if host else ""


def _failed(fact_id: str, checks: dict) -> bool:
    check = checks.get(fact_id)
    return check is not None and not check.passed


def red_flags(record: ProductRecord, checks: dict) -> list[Fact]:
    """Hard flags, breaches, contradictions and injection hits that did not fail verification."""
    return [
        f
        for f in record.facts
        if f.polarity == Polarity.NEGATIVE
        and (f.is_hard_flag or f.topic in RED_FLAG_TOPICS)
        and not _failed(f.id, checks)
    ]


def timeline(record: ProductRecord) -> list[dict]:
    """Registered, first certificate, first capture (those on record), then the run date."""
    points = []
    for topic, label in TIMELINE_TOPICS:
        dates = [(f.observed_at, f) for f in record.facts if f.topic == topic and f.observed_at]
        if dates:
            when, fact = min(dates, key=lambda d: d[0])
            points.append({"label": label, "date": when, "url": fact.source_url})
    points.sort(key=lambda p: p["date"])
    points.append({"label": "Today", "date": record.verdict.created_at, "url": ""})
    return points


def claim_rows(record: ProductRecord, facts: dict[str, Fact]) -> list[dict]:
    claims = {c.id: c for c in record.claims}
    rows = []
    for check in record.claim_checks:
        claim = claims.get(check.claim_id)
        records = [
            facts[i]
            for i in check.fact_ids
            if i in facts and not is_search_tool_url(facts[i].source_url)
        ][:MAX_CLAIM_RECORDS]
        rows.append({"claim": claim, "check": check, "records": records})
    order = {"contradicted": 0, "confirmed": 1, "unconfirmed": 2}
    return sorted(rows, key=lambda r: order[r["check"].status])


def reason_groups(record: ProductRecord) -> list[tuple[str, list]]:
    reasons = record.verdict.reasons
    return [
        (title, [r for r in reasons if r.axis == axis])
        for axis, title in ((Axis.REAL_ALIVE, "Real & alive"), (Axis.TRUST, "Trust"))
    ]


def _date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "unknown"


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("vet", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["safe_url"] = safe_url
    env.filters["domain"] = source_domain
    env.filters["date"] = _date
    return env


def load_run(run_dir: Path) -> list[ProductRecord]:
    """Product records in the order of the verdict file."""
    order: list[str] = []
    verdicts = find_verdict_file(run_dir)
    if verdicts.exists():
        text = verdicts.read_text(encoding="utf-8")
        if verdicts.suffix == ".csv":
            order = [r["product_url"] for r in csv.DictReader(text.splitlines())]
        else:
            order = [v.get("product_url", "") for v in json.loads(text)]
    records = load_records(run_dir)
    rank = {url: i for i, url in enumerate(order)}
    return sorted(records, key=lambda r: rank.get(r.verdict.product_url, len(rank)))


def labelled_domains(labelled_dir: Path = LABELLED_DIR) -> set[str]:
    """Every domain mentioned in any file under data/labelled/."""
    found: set[str] = set()
    if not labelled_dir.exists():
        return found
    for path in labelled_dir.rglob("*"):
        if path.is_file() and path.name != ".gitkeep":
            text = path.read_text(encoding="utf-8", errors="ignore")
            found |= {registered_domain(m.group(1)) for m in _DOMAIN.finditer(text)}
    return found


def check_public(records: list[ProductRecord], labelled_dir: Path = LABELLED_DIR) -> None:
    private = labelled_domains(labelled_dir)
    hits = sorted({registered_domain(r.verdict.product_url) for r in records} & private)
    if hits:
        raise PublicExportRefused(
            f"run includes labelled products ({', '.join(hits)}); not exporting publicly"
        )


def render(
    run_dir: Path,
    out_dir: Path,
    *,
    public: bool = False,
    labelled_dir: Path = LABELLED_DIR,
) -> list[Path]:
    records = load_run(run_dir)
    if public:
        check_public(records, labelled_dir)
    env = _env()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    pages = []
    for record in records:
        slug = product_slug(record.verdict.product_url)
        facts = {f.id: f for f in record.facts}
        checks = {c.fact_id: c for c in record.verification}
        path = out_dir / f"{slug}.html"
        path.write_text(
            env.get_template("report.html.j2").render(
                r=record,
                v=record.verdict,
                facts=facts,
                checks=checks,
                hibp_url=HIBP_URL,
                groups=reason_groups(record),
                claim_rows=claim_rows(record, facts),
                red_flags=red_flags(record, checks),
                timeline=timeline(record),
            ),
            encoding="utf-8",
        )
        written.append(path)
        pages.append({"slug": slug, "r": record, "v": record.verdict})
    index = out_dir / "index.html"
    index.write_text(
        env.get_template("index.html.j2").render(
            run_id=records[0].verdict.run_id if records else run_dir.name,
            pages=pages,
            hibp_url=HIBP_URL,
        ),
        encoding="utf-8",
    )
    written.append(index)
    logger.info("report: %d pages in %s", len(written), out_dir)
    return written
