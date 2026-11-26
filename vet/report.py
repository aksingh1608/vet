"""Static HTML report: one page per product plus an index, rendered with Jinja2.

Everything shown comes from a run directory (products/*.json). Text from vendor pages and
third-party sites is escaped by Jinja2's autoescaping, and only http(s) links are rendered, so
a page cannot inject markup or a javascript: link into the report.

``public=True`` refuses a run that includes any product listed under data/labelled/ (the
organiser's examples are never published).
"""

import json
import logging
import re
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from vet.output import PRODUCTS_DIR, VERDICTS_FILE, ProductRecord, product_slug
from vet.text import registered_domain

logger = logging.getLogger(__name__)

LABELLED_DIR = Path("data") / "labelled"
HIBP_URL = "https://haveibeenpwned.com"
_DOMAIN = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.I)


class PublicExportRefused(RuntimeError):
    """The run includes products from the labelled set."""


def safe_url(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else "#"


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("vet", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["safe_url"] = safe_url
    return env


def load_run(run_dir: Path) -> list[ProductRecord]:
    """Product records in the order of verdicts.json."""
    order: list[str] = []
    verdicts = run_dir / VERDICTS_FILE
    if verdicts.exists():
        order = [v.get("product_url", "") for v in json.loads(verdicts.read_text(encoding="utf-8"))]
    records = [
        ProductRecord.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((run_dir / PRODUCTS_DIR).glob("*.json"))
    ]
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
                r=record, v=record.verdict, facts=facts, checks=checks, hibp_url=HIBP_URL
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
