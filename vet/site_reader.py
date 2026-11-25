"""Read the vendor's own site: discover key pages, extract claims, and record a few site facts
(copyright year, missing pages, text addressed to AI systems).

Everything here is VENDOR-sourced. Claims are things to check; site facts can only count
against the vendor, never for it.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from vet.collectors.base import Context, SiteIdentity, fact_id
from vet.fetch import FetchError, FetchStatusError, Page
from vet.llm import load_prompt
from vet.schemas import Axis, Claim, ClaimType, Fact, Polarity, SourceKind
from vet.text import clean_url, contains_quote, extract_links, html_to_text, same_site, truncate

logger = logging.getLogger(__name__)

MAX_PAGES = 15
PAGE_CHARS_FOR_LLM = 3500
TOTAL_CHARS_FOR_LLM = 40000
STALE_COPYRIGHT_YEARS = 2
INJECTION_CONTEXT_CHARS = 160

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
_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above|earlier) instructions"
    r"|(?:if|when) you are an? (?:ai|llm|large language model|language model|assistant|agent|bot)\b"
    r"|(?:note|message|instructions?) (?:to|for) (?:any |all )?(?:ai|llms?|language models?"
    r"|assistants?|agents?|evaluators?)\b"
    r"|\b(?:ai|llm) (?:agents?|assistants?|evaluators?|models?)\b[^.]{0,80}"
    r"\b(?:should|must|always|recommend|rate|trust|verdict|score)\b",
    re.I,
)


class SitePage(BaseModel):
    kind: str
    url: str
    final_url: str = ""
    status: int = 0
    text: str = ""  # extracted main text
    raw: str = ""  # raw HTML (kept for quote checks, never sent to the LLM in full)
    error: str = ""
    guessed: bool = False  # a fallback path we tried, not a link the site shows


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


async def _fetch(ctx: Context, kind: str, url: str, guessed: bool = False) -> SitePage:
    try:
        page: Page = await ctx.fetcher.get_page(url)
    except FetchStatusError as exc:
        return SitePage(kind=kind, url=url, status=exc.status, error=str(exc), guessed=guessed)
    except FetchError as exc:
        return SitePage(kind=kind, url=url, error=str(exc), guessed=guessed)
    return SitePage(
        kind=kind,
        url=url,
        final_url=page.result.final_url,
        status=page.result.status,
        text=page.text or html_to_text(page.result.text),
        raw=page.result.text,
        guessed=guessed,
    )


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
    )


def injection_facts(pages: list[SitePage], start: int, today: datetime) -> list[Fact]:
    facts: list[Fact] = []
    for page in pages:
        text = html_to_text(page.raw) if page.raw else page.text
        match = _INJECTION.search(text)
        if not match:
            continue
        lo = max(0, match.start() - INJECTION_CONTEXT_CHARS // 2)
        quote = text[lo : match.end() + INJECTION_CONTEXT_CHARS // 2].strip()
        facts.append(
            Fact(
                id=fact_id("site", start + len(facts)),
                collector="site_reader",
                statement=f"A page on the vendor's site contains text addressed to AI systems: "
                f'"{truncate(match.group(0), 80)}"',
                axis=Axis.TRUST,
                polarity=Polarity.NEGATIVE,
                weight=0.9,
                source_url=page.final_url or page.url,
                quote=quote,
                source_kind=SourceKind.VENDOR,
                fetched_at=today,
                is_hard_flag=True,
            )
        )
    return facts


def missing_page_facts(pages: list[SitePage], start: int, today: datetime) -> list[Fact]:
    """Linked pages that return 404/410. Computed facts with no quote (not reason material)."""
    facts: list[Fact] = []
    for page in pages:
        if page.status in (404, 410) and not page.guessed:
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
    return "<data>\n" + "\n".join(parts) + "\n</data>"


def validate_claims(raw: list[_ClaimOut], pages: list[SitePage]) -> tuple[list[Claim], int]:
    """Keep claims whose quote really appears on the page they cite (or any fetched page)."""
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
        if home is None:
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
    links = extract_links(home.raw, home.final_url or home.url)
    plan = plan_pages(home.final_url or home.url, links, ctx.domain)
    others = await asyncio.gather(*(_fetch(ctx, k, u, guessed) for k, u, guessed in plan))
    pages = [home, *others]

    facts: list[Fact] = []
    cf = copyright_fact(home, 1, today)
    if cf:
        facts.append(cf)
    facts += injection_facts(pages, len(facts) + 1, today)
    facts += missing_page_facts(others, len(facts) + 1, today)

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
    errors = [f"{p.kind} {p.url}: {p.error}" for p in pages if p.error]
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
