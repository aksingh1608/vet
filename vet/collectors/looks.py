"""Looks: how the site is built. Low weight, tie-break only (the judge's rules leave looks
facts out of every score), and always filler: a reason only when nothing better exists.

* Site-builder fingerprints (generator meta tag, builder asset hosts).
* Placeholder text left in the pages (lorem ipsum, "Your Company").
* Share of linked help/docs pages that are dead.

Old-looking sites can belong to solid vendors and polished sites can be hollow, so none of
this is evidence on its own.
"""

import logging
import re
from datetime import UTC, datetime

from vet.collectors.base import Context, SitePage, fact_id
from vet.schemas import Axis, Fact, Polarity, SourceKind

logger = logging.getLogger(__name__)

NAME = "looks"
BUILDERS: dict[str, tuple[str, ...]] = {
    "Framer": ("framerusercontent.com", "framer.website", "made in framer"),
    "Webflow": ("webflow.com/", "data-wf-site", "website-files.com"),
    "Wix": ("wixstatic.com", "wix.com website builder"),
    "Squarespace": ("squarespace-cdn.com", "static1.squarespace.com"),
    "WordPress": ("wp-content/", "wp-includes/"),
    "Lovable": ("lovable.app", "gptengineer", "lovable-tagger"),
    "Bolt": ("bolt.new",),
    "v0": ("v0.dev", "v0.app"),
    "Carrd": ("carrd.co",),
}
_GENERATOR = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', re.I)
_PLACEHOLDER = re.compile(
    r"lorem ipsum dolor|your company name|company name here|john doe|jane doe|acme corp"
    r"|placeholder text|example@example\.com",
    re.I,
)
DEAD_HELP_SHARE = 0.5


def builder_fact(home: SitePage, today: datetime) -> Fact | None:
    raw = home.raw
    m = _GENERATOR.search(raw)
    if m:
        name, quote = m.group(1).strip(), m.group(1).strip()
    else:
        low = raw.lower()
        hit = next(((b, sig) for b, sigs in BUILDERS.items() for sig in sigs if sig in low), None)
        if hit is None:
            return None
        name, quote = hit
    return Fact(
        id=fact_id(NAME, 0),
        collector=NAME,
        statement=f"The homepage is built with {name}",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.NEUTRAL,
        weight=0.05,
        source_url=home.final_url or home.url,
        quote=quote,
        source_kind=SourceKind.VENDOR,
        fetched_at=today,
        topic="site_builder",
        is_filler=True,
    )


def placeholder_facts(pages: list[SitePage], today: datetime) -> list[Fact]:
    for page in pages:
        m = _PLACEHOLDER.search(page.text or "")
        if m:
            return [
                Fact(
                    id=fact_id(NAME, 1),
                    collector=NAME,
                    statement=f"The vendor's {page.kind} page still contains placeholder text "
                    f'("{m.group(0)}")',
                    axis=Axis.REAL_ALIVE,
                    polarity=Polarity.NEGATIVE,
                    weight=0.1,
                    source_url=page.final_url or page.url,
                    quote=m.group(0),
                    source_kind=SourceKind.VENDOR,
                    fetched_at=today,
                    topic="placeholder",
                    is_filler=True,
                )
            ]
    return []


def dead_help_fact(pages: list[SitePage], today: datetime) -> Fact | None:
    docs = [p for p in pages if p.kind == "docs" and not p.guessed]
    dead = [p for p in docs if p.status in (404, 410)]
    if not docs or len(dead) / len(docs) < DEAD_HELP_SHARE:
        return None
    return Fact(
        id=fact_id(NAME, 2),
        collector=NAME,
        statement=f"{len(dead)} of {len(docs)} help/docs pages linked from the homepage are "
        f"missing (HTTP {dead[0].status})",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.NEGATIVE,
        weight=0.1,
        source_url=dead[0].url,
        quote="",
        source_kind=SourceKind.VENDOR,
        fetched_at=today,
        topic="dead_help",
        is_filler=True,
    )


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    home = next((p for p in ctx.pages if p.kind == "home" and p.raw), None)
    facts: list[Fact] = []
    if home is not None:
        f = builder_fact(home, today)
        if f:
            facts.append(f)
    facts += placeholder_facts(ctx.pages, today)
    dead = dead_help_fact(ctx.pages, today)
    if dead:
        facts.append(dead)
    return facts
