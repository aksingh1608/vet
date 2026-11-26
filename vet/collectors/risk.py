"""Risk: breaches, lawsuits, fines, insolvency, acquisitions and shutdowns.

* Have I Been Pwned: ``GET /api/v3/breaches?domain={domain}`` only (no key, 1 request per 2 s,
  descriptive User-Agent). Breaches listed there are records.
* Tavily news searches in English, plus German for German-speaking vendors. The fast model
  checks each result is about THIS company (not a namesake) and what event it reports.

A negative event counts only if its quote names the exact product or domain: a sister brand
(cal.eu when judging cal.com), a parent company or a subsidiary does not count.

One report is enough when it comes from reputable press, a regulator or a court, or when the
vendor itself announces the negative fact (shutdown, breach notice, acquisition). Anything else
(social media, forums, blogs) needs a second independent site; until then it is never a hard
flag, its weight is halved and its topic is ``unconfirmed_<event>``.

Breaches carry their year (``observed_at``) and a ``disclosed`` tag when the vendor announced
them, so the judge can tell recent or hidden breaches from old, disclosed ones.
"""

import json
import logging
import re
from datetime import UTC, datetime

from pydantic import BaseModel

from vet.collectors.base import Context, fact_id
from vet.collectors.classify import classify_in_chunks
from vet.collectors.footprint import entity_names, search_name, vendor_payload
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.sources import REPUTABLE_TYPES, names_entity, source_type_for
from vet.tavily import TavilyResult
from vet.text import contains_quote, registered_domain, same_site

logger = logging.getLogger(__name__)

NAME = "risk"
HIBP_URL = "https://haveibeenpwned.com/api/v3/breaches?domain={domain}"
HIBP_PAGE = "https://haveibeenpwned.com/PwnedWebsites#{name}"
MAX_BREACHES = 3
SERIOUS_BREACH_ACCOUNTS = 1_000_000
SERIOUS_DATA = ("Passwords", "Credit cards", "Bank account numbers", "Partial credit card data")
RECENT_BREACH_YEARS = 5
MAX_RESULTS_PER_QUERY = 5
MAX_RESULTS_TO_CLASSIFY = 16
SNIPPET_CHARS = 1200
GERMAN_SPEAKING = {"DE", "AT", "CH"}

# event -> (axis, polarity, base weight, may be a hard flag)
EVENT_RULES: dict[str, tuple[Axis, Polarity, float, bool]] = {
    "breach": (Axis.TRUST, Polarity.NEGATIVE, 0.6, True),
    "lawsuit": (Axis.TRUST, Polarity.NEGATIVE, 0.4, False),
    "fine": (Axis.TRUST, Polarity.NEGATIVE, 0.5, True),
    "insolvency": (Axis.TRUST, Polarity.NEGATIVE, 0.9, True),
    "discontinued": (Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.9, True),
    "acquisition": (Axis.REAL_ALIVE, Polarity.NEUTRAL, 0.2, False),
    "layoffs": (Axis.TRUST, Polarity.NEGATIVE, 0.2, False),
    # Attacks on users, not on the vendor's systems (credential stuffing, phishing, DDoS).
    "security_incident": (Axis.TRUST, Polarity.NEGATIVE, 0.3, False),
}
HARD_SEVERITY = 0.7
EVENTS = tuple(EVENT_RULES)


class _Item(BaseModel):
    url: str
    about_this_vendor: bool
    independent: bool
    event: str
    date: str
    source_type: str
    vendor_disclosed: bool
    # Field order is generation order: the quote is chosen before the sentence is written.
    quote: str
    statement: str
    severity: float


class _Classified(BaseModel):
    items: list[_Item]


# ---- Have I Been Pwned ---------------------------------------------------------------------


def breach_fact(breach: dict[str, object], n: int, url: str, today: datetime) -> Fact | None:
    name = str(breach.get("Name", ""))
    date = str(breach.get("BreachDate", ""))
    if not name or not date:
        return None
    when = datetime.fromisoformat(date).replace(tzinfo=UTC)
    count = int(breach.get("PwnCount", 0) or 0)
    classes = [str(c) for c in breach.get("DataClasses", []) or []]  # type: ignore[union-attr]
    serious = count >= SERIOUS_BREACH_ACCOUNTS or any(c in SERIOUS_DATA for c in classes)
    recent = (today - when).days / 365.25 <= RECENT_BREACH_YEARS
    weight = (0.8 if recent else 0.6) if serious else 0.4
    title = str(breach.get("Title", name))
    shown = ", ".join(classes[:4]).lower()
    return Fact(
        id=fact_id(NAME, n),
        collector=NAME,
        statement=f"Have I Been Pwned lists a breach of {title} dated {date} affecting "
        f"{count:,} accounts" + (f" (exposed: {shown})" if shown else ""),
        axis=Axis.TRUST,
        polarity=Polarity.NEGATIVE,
        weight=weight,
        source_url=HIBP_PAGE.format(name=name),
        check_url=url,
        quote=date,
        source_kind=SourceKind.RECORD,
        fetched_at=today,
        is_hard_flag=serious,
        topic="breach",
        observed_at=when,
        source_type="record",
        code_worded=True,
    )


async def hibp_facts(ctx: Context, today: datetime) -> list[Fact]:
    url = HIBP_URL.format(domain=ctx.domain)
    result = await ctx.fetcher.fetch_api(url)
    if result.status == 404 or not result.ok:
        return []
    breaches = json.loads(result.text) or []
    breaches = sorted(breaches, key=lambda b: str(b.get("BreachDate", "")), reverse=True)
    facts: list[Fact] = []
    for breach in breaches[:MAX_BREACHES]:
        f = breach_fact(breach, len(facts), url, today)
        if f is not None:
            facts.append(f)
    return facts


# ---- News ------------------------------------------------------------------------------------


def queries(ctx: Context) -> list[str]:
    name = search_name(ctx)
    plan = [
        f'"{name}" data breach OR hacked OR security incident OR leak',
        f'"{name}" lawsuit OR sued OR fined OR insolvency OR bankruptcy',
        f'"{name}" acquired OR discontinued OR "shutting down" OR "end of life"',
    ]
    if ctx.identity.country in GERMAN_SPEAKING or ctx.domain.endswith((".de", ".at", ".ch")):
        plan.append(
            f'"{name}" Datenleck OR Hackerangriff OR Klage OR Bußgeld OR Insolvenz '
            "OR übernommen OR eingestellt"
        )
    return plan


def news_facts_from(
    items: list[_Item], seen: dict[str, TavilyResult], ctx: Context, today: datetime, start: int
) -> list[Fact]:
    kept: list[tuple[_Item, TavilyResult, bool]] = []
    for item in items:
        source = seen.get(item.url)
        if source is None or not item.about_this_vendor or item.event not in EVENT_RULES:
            continue
        if not item.quote or not contains_quote(source.content, item.quote):
            continue
        own = same_site(item.url, ctx.domain)
        if not own and not item.independent:
            continue
        if EVENT_RULES[item.event][1] == Polarity.NEGATIVE and not names_entity(
            item.quote, ctx.domain, entity_names(ctx), own_site=own
        ):
            continue  # about a sister brand, a parent or a namesake, not this product
        kept.append((item, source, own))

    # Corroboration: distinct independent sites reporting the same kind of event.
    sites: dict[str, set[str]] = {}
    for item, _, own in kept:
        if not own:
            sites.setdefault(item.event, set()).add(registered_domain(item.url))

    facts: list[Fact] = []
    for item, _, own in kept:
        axis, polarity, base, may_be_hard = EVENT_RULES[item.event]
        kind = "vendor" if own else source_type_for(item.url, item.source_type)
        corroborated = own or kind in REPUTABLE_TYPES or len(sites.get(item.event, set())) >= 2
        severity = min(max(item.severity, 0.0), 1.0)
        weight = max(base, severity) if polarity == Polarity.NEGATIVE else base
        hard = may_be_hard and corroborated and severity >= HARD_SEVERITY
        topic = item.event
        year = _year(item.date)
        tags = ["disclosed"] if item.event == "breach" and (own or item.vendor_disclosed) else []
        if not corroborated:
            # One article is a lead: half weight, never a hard flag, never a real negative.
            weight = round(weight / 2, 2)
            topic = f"unconfirmed_{item.event}"
        facts.append(
            Fact(
                id=fact_id(NAME, start + len(facts)),
                collector=NAME,
                statement=item.statement,
                axis=axis,
                polarity=polarity,
                weight=weight,
                source_url=item.url,
                quote=item.quote,
                source_kind=SourceKind.VENDOR if own else SourceKind.INDEPENDENT,
                fetched_at=today,
                is_hard_flag=hard,
                topic=topic,
                source_type=kind,
                tags=tags,
                observed_at=datetime(year, 1, 1, tzinfo=UTC) if year else None,
            )
        )
    return facts


def _year(text: str) -> int | None:
    m = re.search(r"\b(19[89]\d|20\d{2})\b", text)
    return int(m.group(1)) if m else None


async def news_facts(ctx: Context, today: datetime, start: int) -> list[Fact]:
    if ctx.tavily is None:
        return []
    seen: dict[str, TavilyResult] = {}
    for query in queries(ctx):
        for r in await ctx.tavily.search(query, max_results=MAX_RESULTS_PER_QUERY):
            if r.content and r.url not in seen:
                seen[r.url] = r
    results = list(seen.values())[:MAX_RESULTS_TO_CLASSIFY]
    if not results:
        return []
    items: list[_Item] = await classify_in_chunks(
        ctx.llm,
        prompt="risk_results.md",
        vendor=vendor_payload(ctx),
        results=results,
        output=_Classified,
        step="classify_risk",
        snippet_chars=SNIPPET_CHARS,
    )
    return news_facts_from(items, seen, ctx, today, start)


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    facts: list[Fact] = []
    try:
        facts += await hibp_facts(ctx, today)
    except Exception as exc:  # noqa: BLE001 - one source failing must not lose the other
        logger.warning("risk/hibp failed for %s: %s", ctx.domain, exc)
    facts += await news_facts(ctx, today, start=10)
    logger.info("risk %s: %d facts", ctx.domain, len(facts))
    return facts
