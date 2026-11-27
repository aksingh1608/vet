"""Identity and social-proof audit of the vendor's own pages.

Reads what the site reader already fetched (``ctx.pages`` with raw HTML, ``ctx.identity``,
``ctx.claims``) and makes at most one fast-model call and one Tavily search. Topics:

* ``no_legal_entity``: no legal entity, legal form, address or country on the legal, about,
  privacy or home pages. Only when those pages were fetched: a page we could not read is not
  evidence. ``legal_id`` looks names up in registers; this only states the absence on the site.
* ``traction_inconsistent``: two clearly different figures for the same thing (users,
  customers, downloads) on the vendor's pages.
* ``logo_wall_not_customers``: in a "trusted by" / customers section, most logos are partner,
  programme or certification badges, or investors (fast model, from alt text and file name).
* ``policy_as_file``: the privacy policy or terms link is a .pdf/.doc/.docx download.
* ``coming_soon``: "coming soon" or waitlist wording on a site that shows customer logos.
* ``company_size``: follower count or employee range, only as the LinkedIn search snippet
  states it (LinkedIn is snippet-only and never fetched).

Calibration: unsourced customer logos are normal on real vendors and are never a negative on
their own. All negatives here are vendor-page observations with low weight.

Structure is parsed from the guard-sanitized HTML (no hidden text, no AI-addressed attributes);
every quote is checked against the raw page so the verifier finds it again.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel

from vet import guard
from vet.collectors.base import Context, SitePage, fact_id
from vet.collectors.footprint import search_name
from vet.llm import load_prompt, untrusted
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.sources import source_type_for
from vet.tavily import TavilyResult
from vet.text import contains_quote, normalize, truncate

logger = logging.getLogger(__name__)

NAME = "identity"
LLM_TIMEOUT_S = 20.0
TAVILY_TIMEOUT_S = 20.0
LINKEDIN_DOMAINS = ["linkedin.com"]
LINKEDIN_MAX_RESULTS = 3
MIN_LOGOS = 3
MAX_LOGOS = 40
MAX_QUOTE_CHARS = 160
MIN_TRACTION_VALUE = 100
TRACTION_RATIO = 1.25  # below this, two figures are rounding or a recent update
MAX_POLICY_FACTS = 2

# Weights: vendor-page observations, deliberately low (they support, never decide, a verdict).
W_NO_LEGAL_ENTITY = 0.3
W_TRACTION = 0.3
W_LOGO_WALL = 0.25
W_POLICY_FILE = 0.15
W_COMING_SOON = 0.3
W_COMPANY_SIZE = 0.15

IDENTITY_KINDS = ("legal", "about", "privacy", "home")
TRACTION_SKIP_KINDS = frozenset({"pricing", "docs", "blog", "changelog"})
LOGO_KINDS = ("home", "customers")
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
PROMINENT = frozenset({"h1", "h2", "h3", "button"})
SECTION_END = frozenset({*HEADINGS, "footer"})
FILE_EXTENSIONS = (".pdf", ".doc", ".docx")
NOT_CUSTOMER = frozenset({"partner", "programme", "certification", "investor"})

TRUST_HEADING = re.compile(
    r"\btrusted by\b|\bused by\b|\bloved by\b|\bour (?:customers|clients)\b"
    r"|\bcustomers include\b|\bcompanies (?:that|who) (?:trust|use|rely)"
    r"|\bvertrauen (?:uns|auf)\b|\bunsere kunden\b|\breferenzen\b",
    re.I,
)
POLICY_LINK = re.compile(
    r"privacy|datenschutz|terms|conditions|\bagb\b|nutzungsbedingungen|\btos\b", re.I
)
WAITLIST = re.compile(r"\bwait[- ]?list\b|\bwarteliste\b", re.I)
COMING_SOON = re.compile(
    r"\bcoming soon\b|\blaunching soon\b|\bdemnächst verfügbar\b|\bin kürze verfügbar\b", re.I
)
LEGAL_FORM = re.compile(
    r"(?<![\w.])(?:GmbH|gGmbH|UG|AG|KG|OHG|e\.K\.|Ltd\.?|Limited|LLC|L\.L\.C\.|LLP|PLC|Inc\.?"
    r"|Incorporated|Corp\.?|Corporation|S\.A\.S\.?|SAS|SARL|S\.?r\.?l\.?|B\.V\.|BV|N\.V\.|ApS"
    r"|Oy|Pty|S\.L\.|S\.p\.A\.|sp\. z o\.o\.)(?![\w])"
)
ADDRESS = re.compile(
    r"\b\d{4,5}\s+[A-ZÄÖÜ][a-zäöüß]+"  # postcode + town (DE/AT/CH/NL/FR ...)
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"  # UK postcode
    r"|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"  # US state + ZIP
    r"|(?i:\b(?:street|str\.|straße|strasse|avenue|ave\.|suite|blvd|boulevard|allee)(?!\w))",
)
COUNTRY = re.compile(
    r"\b(?:Germany|Deutschland|Austria|Österreich|Switzerland|Schweiz|United States|USA"
    r"|United Kingdom|UK|England|Ireland|France|Netherlands|Belgium|Luxembourg|Spain|Portugal"
    r"|Italy|Denmark|Sweden|Norway|Finland|Estonia|Poland|Czech Republic|Canada|Australia"
    r"|India|Israel|Singapore)\b"
)
TRACTION = re.compile(
    r"(?<![\d.,%$€£])(\d{1,3}(?:[,.]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(k|m|mio\.?|million|thousand|tausend)?\s*\+?\s+"
    r"(?:(?:happy|active|satisfied|monthly|daily|registered|paying|global|zufriedene|aktive)\s+)"
    r"{0,2}(users|customers|clients|companies|businesses|organi[sz]ations|downloads|installs"
    r"|nutzer|kunden|unternehmen)\b",
    re.I,
)
TRACTION_LIMIT_BEFORE = re.compile(r"(?:up to|bis zu|max\.?|maximum|per|for|für)\s*$", re.I)
CATEGORY = {
    "users": "users",
    "nutzer": "users",
    "downloads": "downloads",
    "installs": "downloads",
}  # everything else counts as customers
MULTIPLIER = {"k": 1e3, "thousand": 1e3, "tausend": 1e3, "m": 1e6, "mio": 1e6, "million": 1e6}
FOLLOWERS = re.compile(r"\b\d[\d,.]*\s*[KkMm]?\s+followers\b")
EMPLOYEES = re.compile(r"\b\d[\d,.]*\s*[-–]\s*\d[\d,.]*\s+employees\b|\b\d[\d,.]*\+?\s+employees\b")


# ---- HTML structure --------------------------------------------------------------------------


@dataclass
class _Img:
    alt: str
    file: str


@dataclass
class _Link:
    href: str
    url: str
    text: str


@dataclass
class _Text:
    text: str
    tag: str  # innermost heading or button tag around it, "" otherwise


@dataclass
class _Doc:
    title: str = ""
    events: list[_Img | _Link | _Text | str] = field(default_factory=list)  # str: section end

    @property
    def texts(self) -> list[_Text]:
        return [e for e in self.events if isinstance(e, _Text)]

    @property
    def links(self) -> list[_Link]:
        return [e for e in self.events if isinstance(e, _Link)]


class _Parser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.doc = _Doc()
        self._stack: list[str] = []
        self._in_title = False
        self._link: _Link | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag in SECTION_END:
            self.doc.events.append(tag)
        if tag in PROMINENT:
            self._stack.append(tag)
        elif tag == "img":
            self.doc.events.append(_Img(alt=a.get("alt", "").strip(), file=_file_name(a)))
        elif tag == "a" and a.get("href") and not a["href"].startswith(("mailto:", "tel:", "#")):
            self._link = _Link(href=a["href"], url=urljoin(self.base, a["href"]), text="")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in PROMINENT and tag in self._stack:
            del self._stack[len(self._stack) - 1 - self._stack[::-1].index(tag)]
        elif tag == "a" and self._link is not None:
            self._link.text = " ".join(self._link.text.split())
            self.doc.events.append(self._link)
            self._link = None

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.doc.title = f"{self.doc.title} {text}".strip()
            return
        if self._link is not None:
            self._link.text += " " + text
        self.doc.events.append(_Text(text=text, tag=self._stack[-1] if self._stack else ""))


def _file_name(attrs: dict[str, str]) -> str:
    src = attrs.get("src") or attrs.get("data-src") or ""
    if src.startswith("data:"):
        return ""
    return urlsplit(src).path.rsplit("/", 1)[-1]


def parse(page: SitePage) -> _Doc:
    parser = _Parser(page.final_url or page.url)
    parser.feed(guard.sanitize_html(page.raw))
    return parser.doc


def fetched(page: SitePage) -> bool:
    return bool(page.raw) and not page.error and (page.status == 0 or page.status < 400)


def page_url(page: SitePage) -> str:
    return page.final_url or page.url


def _fact(n: int, topic: str, statement: str, page: str, quote: str, today: datetime, **kw) -> Fact:  # noqa: ANN003
    data: dict[str, object] = {
        "id": fact_id(NAME, n),
        "collector": NAME,
        "statement": statement,
        "axis": Axis.TRUST,
        "polarity": Polarity.NEGATIVE,
        "source_url": page,
        "quote": quote,
        "source_kind": SourceKind.VENDOR,
        "source_type": "vendor",
        "fetched_at": today,
        "topic": topic,
        "code_worded": True,
    }
    data.update(kw)
    return Fact.model_validate(data)


def _quote(raw: str, text: str) -> str:
    """``text`` shortened to a quotable length, or "" if it is not on the raw page."""
    text = text.strip()
    if len(text) > MAX_QUOTE_CHARS:
        text = text[:MAX_QUOTE_CHARS].rsplit(" ", 1)[0]
    return text if text and contains_quote(raw, text) else ""


# ---- 1. no_legal_entity ----------------------------------------------------------------------


def no_legal_entity(ctx: Context, docs: dict[str, _Doc], today: datetime) -> list[Fact]:
    ident = ctx.identity
    if ident.legal_entity or ident.register_number or ident.country:
        return []
    if any(c.type in ("legal_name", "register_number") for c in ctx.claims):
        return []
    pages = [p for p in ctx.pages if p.kind in IDENTITY_KINDS]
    if any(not fetched(p) and not p.guessed for p in pages):
        logger.info(
            "identity: an identity page was not fetched on %s; no absence claim", ctx.domain
        )
        return []
    checked = [p for p in pages if fetched(p)]
    if not any(p.kind == "home" for p in checked):
        return []
    for p in checked:
        text = " ".join(t.text for t in docs[p.url].texts)
        if LEGAL_FORM.search(text) or ADDRESS.search(text) or COUNTRY.search(text):
            return []
    order = {k: i for i, k in enumerate(IDENTITY_KINDS)}
    source = min(checked, key=lambda p: order[p.kind])
    doc = docs[source.url]
    candidates = [
        doc.title,
        *(t.text for t in doc.texts if t.tag in HEADINGS),
        *(t.text for t in doc.texts),
    ]
    quote = next((q for c in candidates if (q := _quote(source.raw, c))), "")
    if not quote:
        return []
    named = ", ".join(f"{p.kind} ({page_url(p)})" for p in checked)
    return [
        _fact(
            0,
            "no_legal_entity",
            f"No legal entity, legal form, address or country on the pages checked: {named}",
            page_url(source),
            quote,
            today,
            weight=W_NO_LEGAL_ENTITY,
        )
    ]


# ---- 2. traction_inconsistent ----------------------------------------------------------------


@dataclass
class _Figure:
    value: float
    quote: str
    url: str


def _value(number: str, unit: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", number):
        base = float(re.sub(r"[,.]", "", number))
    else:
        base = float(number.replace(",", "."))
    return base * MULTIPLIER.get(unit.lower().rstrip("."), 1.0)


def figures(page: SitePage, doc: _Doc) -> dict[str, list[_Figure]]:
    out: dict[str, list[_Figure]] = {}
    for t in doc.texts:
        for m in TRACTION.finditer(t.text):
            if TRACTION_LIMIT_BEFORE.search(t.text[: m.start()]):
                continue  # "up to 50 users": a plan limit, not traction
            value = _value(m.group(1), m.group(2) or "")
            quote = _quote(page.raw, m.group(0))
            if value < MIN_TRACTION_VALUE or not quote:
                continue
            category = CATEGORY.get(m.group(3).lower(), "customers")
            out.setdefault(category, []).append(_Figure(value, quote, page_url(page)))
    return out


def traction_inconsistent(ctx: Context, docs: dict[str, _Doc], today: datetime) -> list[Fact]:
    found: dict[str, list[_Figure]] = {}
    for p in ctx.pages:
        if fetched(p) and p.kind not in TRACTION_SKIP_KINDS:
            for cat, figs in figures(p, docs[p.url]).items():
                found.setdefault(cat, []).extend(figs)
    facts: list[Fact] = []
    for cat, figs in found.items():
        low = min(figs, key=lambda f: f.value)
        high = max(figs, key=lambda f: f.value)
        if high.value < low.value * TRACTION_RATIO:
            continue
        facts.append(
            _fact(
                10 + len(facts),
                "traction_inconsistent",
                f'The vendor\'s pages give different {cat} figures: "{low.quote}" ({low.url}) '
                f'and "{high.quote}" ({high.url})',
                high.url,
                high.quote,
                today,
                weight=W_TRACTION,
            )
        )
    return facts


# ---- 3. logo_wall_not_customers --------------------------------------------------------------


class _Logo(BaseModel):
    index: int
    kind: Literal["customer", "partner", "programme", "certification", "investor", "unknown"]


class _Logos(BaseModel):
    logos: list[_Logo]


@dataclass
class _Wall:
    page: SitePage
    heading: str
    logos: list[_Img]


def logo_wall(ctx: Context, docs: dict[str, _Doc]) -> _Wall | None:
    """The first "trusted by" / customers section with at least MIN_LOGOS images."""
    pages = sorted(
        (p for p in ctx.pages if fetched(p) and p.kind in LOGO_KINDS),
        key=lambda p: LOGO_KINDS.index(p.kind),
    )
    for p in pages:
        events = docs[p.url].events
        for i, ev in enumerate(events):
            if not isinstance(ev, _Text) or not TRUST_HEADING.search(ev.text):
                continue
            logos: list[_Img] = []
            for nxt in events[i + 1 :]:
                if isinstance(nxt, str):
                    break  # next heading or footer: another section
                if isinstance(nxt, _Img):
                    logos.append(nxt)
            heading = _quote(p.raw, ev.text)
            if len(logos) >= MIN_LOGOS and heading:
                return _Wall(page=p, heading=heading, logos=logos[:MAX_LOGOS])
    return None


async def classify_logos(ctx: Context, wall: _Wall) -> list[str]:
    payload = [
        {"index": i, "alt": guard.scrub_text(img.alt), "file": guard.scrub_text(img.file)}
        for i, img in enumerate(wall.logos)
    ]
    async with asyncio.timeout(LLM_TIMEOUT_S):
        out = await ctx.llm.structured(
            kind="fast",
            system=load_prompt("logo_wall.md"),
            user=untrusted(json.dumps(payload, ensure_ascii=False)),
            output=_Logos,
            step="classify_logos",
        )
    kinds = ["unknown"] * len(wall.logos)
    for logo in out.logos:
        if 0 <= logo.index < len(kinds):
            kinds[logo.index] = logo.kind
    return kinds


def logo_wall_fact(wall: _Wall, kinds: list[str], today: datetime) -> list[Fact]:
    """Most logos (more than half, unknowns counted in the total) are not customers."""
    others = [i for i, k in enumerate(kinds) if k in NOT_CUSTOMER]
    if len(others) * 2 <= len(kinds):
        return []
    examples = [wall.logos[i].alt for i in others if wall.logos[i].alt][:3]
    shown = f" (e.g. {', '.join(examples)})" if examples else ""
    return [
        _fact(
            20,
            "logo_wall_not_customers",
            f'In the "{truncate(wall.heading, 60)}" section of {page_url(wall.page)}, '
            f"{len(others)} of {len(kinds)} logos are partner, programme or certification "
            f"badges or investors, not customers{shown}",
            page_url(wall.page),
            wall.heading,
            today,
            weight=W_LOGO_WALL,
        )
    ]


# ---- 4. policy_as_file -----------------------------------------------------------------------


def policy_as_file(ctx: Context, docs: dict[str, _Doc], today: datetime) -> list[Fact]:
    facts: list[Fact] = []
    seen: set[str] = set()
    for p in ctx.pages:
        if not fetched(p):
            continue
        for link in docs[p.url].links:
            path = urlsplit(link.url).path.lower()
            if not path.endswith(FILE_EXTENSIONS) or link.url in seen:
                continue
            if not POLICY_LINK.search(f"{link.text} {path}"):
                continue
            quote = _quote(p.raw, link.text) or _quote(p.raw, link.href)
            if not quote:
                continue
            seen.add(link.url)
            ext = path.rsplit(".", 1)[-1]
            facts.append(
                _fact(
                    30 + len(facts),
                    "policy_as_file",
                    f'The "{truncate(link.text or link.href, 60)}" link on {page_url(p)} is a '
                    f".{ext} file download ({link.url}), not a web page",
                    page_url(p),
                    quote,
                    today,
                    weight=W_POLICY_FILE,
                )
            )
            if len(facts) >= MAX_POLICY_FACTS:
                return facts
    return facts


# ---- 5. coming_soon --------------------------------------------------------------------------


def coming_soon(
    ctx: Context, docs: dict[str, _Doc], wall: _Wall | None, today: datetime
) -> list[Fact]:
    """Waitlist wording on the homepage, or "coming soon" in a heading/button or next to the
    product name, on a site that shows a customer-logo section."""
    if wall is None:
        return []
    home = next((p for p in ctx.pages if p.kind == "home" and fetched(p)), None)
    if home is None:
        return []
    names = [normalize(n) for n in (ctx.identity.product_name, ctx.identity.vendor_name) if n]
    for t in docs[home.url].texts:
        soon = COMING_SOON.search(t.text)
        about_product = t.tag in PROMINENT or any(n in normalize(t.text) for n in names)
        if not (WAITLIST.search(t.text) or (soon and about_product)):
            continue
        quote = _quote(home.raw, t.text)
        if not quote:
            continue
        return [
            _fact(
                40,
                "coming_soon",
                f'The homepage {page_url(home)} says "{truncate(quote, 80)}" while showing a '
                f'"{truncate(wall.heading, 40)}" logo section',
                page_url(home),
                quote,
                today,
                axis=Axis.REAL_ALIVE,
                weight=W_COMING_SOON,
            )
        ]
    return []


# ---- 6. company_size -------------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text))


def is_company_page(result: TavilyResult, name: str) -> bool:
    parts = urlsplit(result.url)
    if not (parts.hostname or "").endswith("linkedin.com") or "/company/" not in parts.path:
        return False
    slug = _key(parts.path.split("/company/", 1)[1].split("/", 1)[0])
    want = _key(name)
    return bool(want) and want in (slug, _key(result.title.split("|", 1)[0]))


def company_size_facts(results: list[TavilyResult], name: str, today: datetime) -> list[Fact]:
    page = next((r for r in results if r.content and is_company_page(r, name)), None)
    if page is None:
        return []
    facts: list[Fact] = []
    for pattern in (EMPLOYEES, FOLLOWERS):
        m = pattern.search(page.content)
        if m is None:
            continue
        facts.append(
            _fact(
                50 + len(facts),
                "company_size",
                f"The LinkedIn company page of {name} states: {m.group(0)}",
                page.url,
                m.group(0),
                today,
                axis=Axis.REAL_ALIVE,
                polarity=Polarity.NEUTRAL,
                weight=W_COMPANY_SIZE,
                source_kind=SourceKind.INDEPENDENT,
                source_type=source_type_for(page.url),
            )
        )
    return facts


async def company_size(ctx: Context, today: datetime) -> list[Fact]:
    if ctx.tavily is None:
        return []
    name = search_name(ctx)
    async with asyncio.timeout(TAVILY_TIMEOUT_S):
        results = await ctx.tavily.search(
            f'"{name}" LinkedIn company',
            include_domains=LINKEDIN_DOMAINS,
            max_results=LINKEDIN_MAX_RESULTS,
        )
    return company_size_facts(results, name, today)


# ---- collector -------------------------------------------------------------------------------


async def _logo_facts(ctx: Context, wall: _Wall | None, today: datetime) -> list[Fact]:
    if wall is None:
        return []
    return logo_wall_fact(wall, await classify_logos(ctx, wall), today)


async def collect(ctx: Context) -> list[Fact]:
    today = ctx.now()
    docs = {p.url: parse(p) for p in ctx.pages if fetched(p)}
    facts: list[Fact] = []
    wall: _Wall | None = None
    for name, part in (
        ("no_legal_entity", lambda: no_legal_entity(ctx, docs, today)),
        ("traction", lambda: traction_inconsistent(ctx, docs, today)),
        ("policy_as_file", lambda: policy_as_file(ctx, docs, today)),
    ):
        try:
            facts += part()
        except Exception as exc:  # noqa: BLE001 - one check failing must not lose the others
            logger.warning("identity/%s failed for %s: %s", name, ctx.domain, exc)
    try:
        wall = logo_wall(ctx, docs)
        facts += coming_soon(ctx, docs, wall, today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("identity/logo_wall failed for %s: %s", ctx.domain, exc)
    remote = await asyncio.gather(
        _logo_facts(ctx, wall, today), company_size(ctx, today), return_exceptions=True
    )
    for part_name, result in zip(("classify_logos", "company_size"), remote, strict=True):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            logger.warning("identity/%s failed for %s: %s", part_name, ctx.domain, result)
        else:
            facts += result
    logger.info("identity %s: %d facts", ctx.domain, len(facts))
    return facts
