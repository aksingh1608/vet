"""Read the vendor's own site: discover key pages, extract claims, and record a few site facts
(copyright year, missing pages). The guard scans every page for text addressed to AI systems
and strips it, with hidden text and comments, before anything reaches the LLM.

Blocked pages (HTTP 403 or 429): the homepage and the key pages (legal, about, customers,
pricing) are read from the newest Wayback Machine capture instead. The source of every claim
and fact from such a page is the snapshot URL, and fact statements say "archived copy of
<date>". A missing capture is not a dead page, so archived pages never yield 404 facts.

Everything here is VENDOR-sourced. Claims are things to check; site facts can only count
against the vendor, never for it.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field

from vet import guard
from vet.collectors.base import Context, SiteIdentity, SitePage, fact_id
from vet.fetch import FetchError, FetchStatusError, extract_main_text
from vet.llm import load_prompt, untrusted
from vet.schemas import Axis, Claim, ClaimType, Fact, Polarity, SourceKind
from vet.text import clean_url, contains_quote, extract_links, html_to_text, same_site, truncate

logger = logging.getLogger(__name__)

MAX_PAGES = 15
PAGE_CHARS_FOR_LLM = 3500
TOTAL_CHARS_FOR_LLM = 40000
STALE_COPYRIGHT_YEARS = 2

# Page kind -> keywords matched against link path and anchor text (EN + DE).
PAGE_KINDS: dict[str, tuple[str, ...]] = {
    "legal": ("impressum", "imprint", "legal-notice", "legal notice", "anbieterkennzeichnung"),
    "about": ("about", "über uns", "ueber-uns", "uber-uns", "company", "unternehmen"),
    "customers": ("customers", "kunden", "case-stud", "case stud", "referenzen", "success"),
    "pricing": ("pricing", "preise", "plans"),
    "docs": ("docs", "documentation", "help", "support", "hilfe", "faq", "knowledge"),
    "changelog": (
        "changelog",
        "release-notes",
        "release notes",
        "releases",
        "what's new",
        "whats-new",
        "neuigkeiten",
    ),
    "blog": ("blog", "news", "newsroom", "press"),
    "security": ("security", "trust", "sicherheit", "compliance"),
    "privacy": ("privacy", "datenschutz"),
    "careers": ("careers", "jobs", "karriere"),
}
# Tried only when no link of that kind exists on the homepage.
FALLBACK_PATHS: dict[str, tuple[str, ...]] = {
    "legal": ("/impressum", "/imprint", "/legal-notice"),
    "about": ("/about",),
    "changelog": ("/changelog",),
}

_COPYRIGHT = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I
)


class _ClaimOut(BaseModel):
    type: ClaimType
    text: str
    source_url: str
    quote: str


class _ExtractOut(BaseModel):
    vendor_name: str
    product_name: str
    legal_entity: str
    register_court: str
    register_number: str
    country: str
    claims: list[_ClaimOut]


class SiteReport(BaseModel):
    pages: list[SitePage]
    identity: SiteIdentity
    claims: list[Claim]
    facts: list[Fact]
    dropped_claims: int = 0
    errors: list[str] = Field(default_factory=list)


def classify_links(links: list[tuple[str, str]], domain: str) -> dict[str, list[str]]:
    """Same-site links grouped by page kind, first match wins, in document order."""
    found: dict[str, list[str]] = {}
    seen: set[str] = set()
    for url, text in links:
        if not same_site(url, domain) or url in seen:
            continue
        path = urlsplit(url).path.lower()
        if path in ("", "/"):
            continue
        hay = f"{path} {text.lower()}"
        for kind, words in PAGE_KINDS.items():
            if any(w in hay for w in words):
                found.setdefault(kind, []).append(url)
                seen.add(url)
                break
    return found


def plan_pages(
    home_url: str, links: list[tuple[str, str]], domain: str
) -> list[tuple[str, str, bool]]:
    """(kind, url, guessed) to fetch after the homepage: one per kind, then extras, max 14."""
    by_kind = classify_links(links, domain)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(home_url))
    plan: list[tuple[str, str, bool]] = []
    for kind in PAGE_KINDS:
        if by_kind.get(kind):
            plan.append((kind, by_kind[kind][0], False))
        elif kind in FALLBACK_PATHS:
            plan.append((kind, clean_url(origin + FALLBACK_PATHS[kind][0]), True))
    planned = {u for _, u, _ in plan}
    plan.extend((k, u, False) for k, urls in by_kind.items() for u in urls[1:2] if u not in planned)
    return plan[: MAX_PAGES - 1]


def llm_text(raw: str, url: str) -> str:
    """What the LLM may see of a page: main text of the sanitized HTML, scrubbed again."""
    clean = guard.sanitize_html(raw)
    return guard.scrub_text(extract_main_text(clean, url) or html_to_text(clean))


BLOCKED_STATUSES = frozenset({403, 429})
ARCHIVE_PAGE_KINDS = frozenset({"home", "legal", "about", "customers", "pricing"})
# Newest capture that was a real page: Wayback replays the captured status, and a site that
# blocks us often blocked the archive crawler too (its newest captures are 403/429 pages).
WAYBACK_NEWEST_OK = (
    "https://web.archive.org/cdx/search/cdx?url={url}&output=json&fl=timestamp"
    "&filter=statuscode:200&limit=-1"
)
WAYBACK_SNAPSHOT = "https://web.archive.org/web/{ts}/{url}"
_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/(\d{8})\d*[a-z_]*/(https?://.+)$")


def unwrap_wayback(url: str) -> str:
    """The original URL behind a Wayback snapshot link (links in an archived page point there)."""
    m = _WAYBACK.match(url)
    return clean_url(m.group(2)) if m else url


def capture_date(snapshot_url: str) -> str:
    m = _WAYBACK.match(snapshot_url)
    if not m:
        return ""
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


async def _archived(ctx: Context, kind: str, url: str, guessed: bool, why: str) -> SitePage:
    """The newest Wayback capture (with status 200) of a page the live site refused to serve."""
    try:
        rows = await ctx.fetcher.get_json(WAYBACK_NEWEST_OK.format(url=quote(url, safe="")))
        stamps = [r[0] for r in rows[1:]] if isinstance(rows, list) else []
        if not stamps:
            return SitePage(kind=kind, url=url, error=f"{why}; no archived copy", guessed=guessed)
        result = await ctx.fetcher.fetch_api(WAYBACK_SNAPSHOT.format(ts=stamps[-1], url=url))
    except (FetchError, ValueError) as exc:
        return SitePage(kind=kind, url=url, error=f"{why}; archive: {exc}", guessed=guessed)
    date = capture_date(result.final_url)
    if not result.ok or not date:
        return SitePage(
            kind=kind, url=url, error=f"{why}; no archived copy ({result.status})", guessed=guessed
        )
    text = await asyncio.to_thread(llm_text, result.text, result.final_url)
    return SitePage(
        kind=kind,
        url=url,
        final_url=result.final_url,
        status=result.status,
        text=text,
        raw=result.text,
        guessed=guessed,
        archived=date,
    )


async def _fetch(
    ctx: Context, kind: str, url: str, guessed: bool = False, archive_only: bool = False
) -> SitePage:
    if archive_only:
        return await _archived(ctx, kind, url, guessed, "homepage blocked")
    try:
        result = await ctx.fetcher.get_text(url)
    except FetchStatusError as exc:
        if exc.status in BLOCKED_STATUSES and kind in ARCHIVE_PAGE_KINDS:
            return await _archived(ctx, kind, url, guessed, f"HTTP {exc.status}")
        return SitePage(kind=kind, url=url, status=exc.status, error=str(exc), guessed=guessed)
    except FetchError as exc:
        return SitePage(kind=kind, url=url, error=str(exc), guessed=guessed)
    text = await asyncio.to_thread(llm_text, result.text, result.final_url)
    return SitePage(
        kind=kind,
        url=url,
        final_url=result.final_url,
        status=result.status,
        text=text,
        raw=result.text,
        guessed=guessed,
    )


def label_archived(facts: list[Fact], pages: list[SitePage]) -> list[Fact]:
    """Say in the statement when a fact comes from an archived copy."""
    dates = {p.final_url: p.archived for p in pages if p.archived}
    labelled: list[Fact] = []
    for f in facts:
        date = dates.get(f.source_url)
        statement = f"{f.statement} (archived copy of {date})" if date else f.statement
        labelled.append(f.model_copy(update={"statement": statement}) if date else f)
    return labelled


def copyright_fact(home: SitePage, n: int, today: datetime) -> Fact | None:
    years = [(int(m.group(1)), m.group(0)) for m in _COPYRIGHT.finditer(home.raw or home.text)]
    if not years:
        return None
    year, quote = max(years)
    stale = today.year - year >= STALE_COPYRIGHT_YEARS
    return Fact(
        id=fact_id("site", n),
        collector="site_reader",
        statement=f"The homepage footer shows copyright year {year}",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.NEGATIVE if stale else Polarity.NEUTRAL,
        weight=0.2 if stale else 0.05,
        source_url=home.final_url or home.url,
        quote=quote.strip(),
        source_kind=SourceKind.VENDOR,
        fetched_at=today,
        topic="copyright",
        is_filler=True,
    )


def injection_facts(pages: list[SitePage], today: datetime) -> list[Fact]:
    """Guard hits on any fetched page (hard flags, collector 'guard'). The same text in the same
    channel on several pages (a shared footer) is one fact, on the first page found."""
    facts: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        if not page.raw:
            continue
        found, hits = guard.hit_facts(page.final_url or page.url, page.raw, today, len(facts))
        for fact, hit in zip(found, hits, strict=True):
            key = (hit.channel, hit.match.lower())
            if key not in seen:
                seen.add(key)
                facts.append(fact.model_copy(update={"id": f"guard-{len(facts)}"}))
    return facts


def missing_page_facts(pages: list[SitePage], start: int, today: datetime) -> list[Fact]:
    """Linked pages that return 404/410. Computed facts with no quote (not reason material)."""
    facts: list[Fact] = []
    for page in pages:
        if page.status in (404, 410) and not page.guessed and not page.archived:
            facts.append(
                Fact(
                    id=fact_id("site", start + len(facts)),
                    collector="site_reader",
                    statement=f"A {page.kind} page linked from the homepage "
                    f"returns HTTP {page.status}",
                    axis=Axis.REAL_ALIVE,
                    polarity=Polarity.NEGATIVE,
                    weight=0.15,
                    source_url=page.url,
                    quote="",
                    source_kind=SourceKind.VENDOR,
                    fetched_at=today,
                    topic="missing_page",
                )
            )
    return facts


def _llm_input(pages: list[SitePage]) -> str:
    parts: list[str] = []
    budget = TOTAL_CHARS_FOR_LLM
    for page in pages:
        if not page.text or budget <= 0:
            continue
        chunk = truncate(page.text, min(PAGE_CHARS_FOR_LLM, budget))
        budget -= len(chunk)
        parts.append(
            f'<page url="{page.final_url or page.url}" kind="{page.kind}">\n{chunk}\n</page>'
        )
    return untrusted("\n".join(parts))


# Pages about other companies or products: a founding year there is rarely the vendor's own.
NOT_ABOUT_VENDOR_KINDS = frozenset({"blog", "customers", "changelog", "docs"})


def validate_claims(raw: list[_ClaimOut], pages: list[SitePage]) -> tuple[list[Claim], int]:
    """Keep claims whose quote really appears on the page they cite (or any fetched page).
    Founding-year claims from blog, customer, changelog or docs pages are dropped: those
    pages describe customers and partners as often as the vendor."""
    by_url = {p.final_url or p.url: p for p in pages} | {p.url: p for p in pages}
    kept: list[Claim] = []
    dropped = 0
    for c in raw:
        page = by_url.get(clean_url(c.source_url)) or by_url.get(c.source_url)
        candidates = [page] if page else pages
        home = next(
            (
                p
                for p in candidates
                if contains_quote(p.text, c.quote) or contains_quote(p.raw, c.quote)
            ),
            None,
        )
        if home is None or (c.type == "founded_year" and home.kind in NOT_ABOUT_VENDOR_KINDS):
            dropped += 1
            continue
        kept.append(
            Claim(
                id=f"c{len(kept) + 1}",
                type=c.type,
                text=c.text,
                source_url=home.final_url or home.url,
                quote=c.quote,
            )
        )
    return kept, dropped


async def read_site(ctx: Context) -> SiteReport:
    today = datetime.now(UTC)
    home = await _fetch(ctx, "home", ctx.url)
    if not home.raw:
        raise FetchError(f"homepage unreadable: {home.error or home.status}")
    archived = bool(home.archived)
    base = ctx.url if archived else home.final_url or home.url
    links = [(unwrap_wayback(u), t) for u, t in extract_links(home.raw, home.final_url or home.url)]
    plan = plan_pages(base, links, ctx.domain)
    if archived:  # the live site refuses us: read only the key pages, from the archive
        plan = [(k, u, g) for k, u, g in plan if k in ARCHIVE_PAGE_KINDS]
    others = await asyncio.gather(
        *(_fetch(ctx, k, u, guessed, archive_only=archived) for k, u, guessed in plan)
    )
    pages = [home, *others]

    facts: list[Fact] = []
    cf = copyright_fact(home, 1, today)
    if cf:
        facts.append(cf)
    facts += missing_page_facts(others, len(facts) + 1, today)
    facts += injection_facts(pages, today)
    facts = label_archived(facts, pages)

    readable = [p for p in pages if p.text]
    out = await ctx.llm.structured(
        kind="fast",
        system=load_prompt("extract_claims.md"),
        user=_llm_input(readable),
        output=_ExtractOut,
        step="extract_claims",
    )
    claims, dropped = validate_claims(out.claims, readable)
    identity = SiteIdentity(
        vendor_name=out.vendor_name,
        product_name=out.product_name,
        legal_entity=out.legal_entity,
        register_court=out.register_court,
        register_number=out.register_number,
        country=out.country.upper()[:2],
    )
    errors = [f"{p.kind} {p.url}: {p.error}" for p in pages if p.error and not p.guessed]
    errors += [
        f"{p.kind} {p.url}: blocked; read the archived copy of {p.archived} ({p.final_url})"
        for p in pages
        if p.archived
    ]
    logger.info(
        "site %s: %d pages, %d claims (%d dropped)", ctx.domain, len(readable), len(claims), dropped
    )
    return SiteReport(
        pages=pages,
        identity=identity,
        claims=claims,
        facts=facts,
        dropped_claims=dropped,
        errors=errors,
    )
