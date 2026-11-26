"""Footprint: what do independent sources say about this vendor and product?

Tavily searches (EN, plus DE for German-speaking vendors), the vendor's own domain excluded,
then the fast model classifies each result: about this vendor? independent? what does it say?
Only results whose quote really appears in the result content become facts.

Results on snippet-only domains (G2, Capterra, Trustpilot, OMR Reviews, Software Advice,
Gartner, LinkedIn, North Data) are kept: those pages are never fetched, and the Tavily result
content is the quote. Directory listings are filler; negative review scores are a minor signal.
"""

import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from vet import guard
from vet.collectors.base import Context, fact_id
from vet.llm import load_prompt, untrusted
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.tavily import TavilyResult
from vet.text import contains_quote, same_site, truncate

logger = logging.getLogger(__name__)

NAME = "footprint"
REVIEW_SITES = [
    "g2.com",
    "capterra.com",
    "capterra.de",
    "omr.com",
    "trustradius.com",
    "getapp.com",
    "softwareadvice.com",
    "trustpilot.com",
]
GERMAN_SPEAKING = {"DE", "AT", "CH"}
MAX_RESULTS_PER_QUERY = 5
MAX_RESULTS_TO_CLASSIFY = 18
SNIPPET_CHARS = 1200
# A mixed or poor review score is a minor negative: it never moves the verdict alone.
MINOR_REVIEW_WEIGHT = 0.15
TOPICS = ("reviews", "customer", "news", "directory", "marketplace", "jobs", "partner", "other")


class _Item(BaseModel):
    # Field order is generation order: the quote is chosen before the sentence is written.
    url: str
    relevant: bool
    independent: bool
    topic: str
    quote: str
    statement: str
    axis: Axis
    polarity: Polarity
    weight: float
    is_hard_flag: bool


class _Classified(BaseModel):
    items: list[_Item]


def search_name(ctx: Context) -> str:
    return ctx.identity.product_name or ctx.identity.vendor_name or ctx.domain.split(".")[0]


def queries(ctx: Context) -> list[tuple[str, dict[str, list[str]]]]:
    name = search_name(ctx)
    exclude = {"exclude_domains": [ctx.domain]}
    plan: list[tuple[str, dict[str, list[str]]]] = [
        (f'"{name}" {ctx.domain}', exclude),
        (f'"{name}" software reviews', {"include_domains": REVIEW_SITES}),
        (f'"{name}" customer case study', exclude),
    ]
    if ctx.identity.country in GERMAN_SPEAKING or ctx.domain.endswith((".de", ".at", ".ch")):
        plan.append((f'"{name}" Software Erfahrungen', exclude))
    return plan


def _keep(result: TavilyResult, ctx: Context) -> bool:
    return bool(result.content) and not same_site(result.url, ctx.domain)


def calibrate(item: _Item) -> tuple[float, bool, bool]:
    """(weight, is_hard_flag, is_filler) after the footprint rules."""
    topic = item.topic if item.topic in TOPICS else "other"
    weight = min(max(item.weight, 0.0), 1.0)
    hard = item.is_hard_flag
    if topic == "reviews" and item.polarity == Polarity.NEGATIVE:
        weight, hard = min(weight, MINOR_REVIEW_WEIGHT), False
    return weight, hard, topic == "directory"


async def collect(ctx: Context) -> list[Fact]:
    if ctx.tavily is None:
        return []
    today = datetime.now(UTC)
    seen: dict[str, TavilyResult] = {}
    for query, params in queries(ctx):
        for r in await ctx.tavily.search(query, max_results=MAX_RESULTS_PER_QUERY, **params):
            if _keep(r, ctx) and r.url not in seen:
                seen[r.url] = r
    results = list(seen.values())[:MAX_RESULTS_TO_CLASSIFY]
    if not results:
        return []

    payload = {
        "vendor": {
            "name": ctx.identity.vendor_name,
            "product": ctx.identity.product_name,
            "domain": ctx.domain,
            "legal_entity": ctx.identity.legal_entity,
            "country": ctx.identity.country,
        },
        "results": [
            {
                "url": r.url,
                "title": guard.scrub_text(r.title),
                "content": guard.scrub_text(truncate(r.content, SNIPPET_CHARS)),
            }
            for r in results
        ],
    }
    out = await ctx.llm.structured(
        kind="fast",
        system=load_prompt("classify_results.md"),
        user=untrusted(json.dumps(payload, ensure_ascii=False)),
        output=_Classified,
        step="classify_footprint",
    )
    facts: list[Fact] = []
    for item in out.items:
        source = seen.get(item.url)
        if source is None or not item.relevant or not item.independent:
            continue
        if not item.quote or not contains_quote(source.content, item.quote):
            continue
        weight, hard, filler = calibrate(item)
        facts.append(
            Fact(
                id=fact_id(NAME, len(facts)),
                collector=NAME,
                statement=item.statement,
                axis=item.axis,
                polarity=item.polarity,
                weight=weight,
                source_url=item.url,
                quote=item.quote,
                source_kind=SourceKind.INDEPENDENT,
                fetched_at=today,
                is_hard_flag=hard,
                topic=item.topic if item.topic in TOPICS else "other",
                is_filler=filler,
            )
        )
    logger.info("footprint %s: %d results, %d facts", ctx.domain, len(results), len(facts))
    return facts
