"""Footprint: what do independent sources say about this vendor and product?

Tavily searches (EN, plus DE for German-speaking vendors), the vendor's own domain excluded,
then the fast model classifies each result: about this vendor? independent? what does it say?
Only results whose quote really appears in the result content become facts.

When the base searches find fewer than MIN_STRONG_SOURCES distinct sites with strong positive
evidence, three targeted searches run in parallel: funding news in independent press,
customers naming the vendor on their own sites, and review platforms with review counts.
Strong-evidence rules are unchanged (sources.evidence_class).

Every result except a review page must name the exact product or domain in its quote, positive
or negative: a namesake's funding round is not evidence for this vendor.

Results on snippet-only domains (G2, Capterra, Trustpilot, OMR Reviews, Software Advice,
Gartner, LinkedIn, North Data) are kept: those pages are never fetched, and the Tavily result
content is the quote. Directory listings are filler; negative review scores are a minor signal.
"""

import asyncio
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from vet.collectors.base import Context, fact_id
from vet.collectors.classify import classify_in_chunks
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.sources import evidence_class, names_entity, review_statement, source_type_for
from vet.tavily import TavilyResult
from vet.text import contains_quote, registered_domain, same_site

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
# Weights are set in code by evidence class, never taken from the model, so the same source
# always weighs the same.
CLASS_WEIGHTS: dict[str, float] = {"strong": 0.5, "normal": 0.3, "weak": 0.1}
MIN_STRONG_SOURCES = 3  # fewer strong sites than this after the base searches: search more
TARGETED_REVIEW_SITES = ["g2.com", "capterra.com", "trustpilot.com", "omr.com"]
TOPICS = ("reviews", "customer", "news", "directory", "marketplace", "jobs", "partner", "other")


class _Item(BaseModel):
    # Field order is generation order: the quote is chosen before the sentence is written.
    url: str
    relevant: bool
    independent: bool
    topic: str
    source_type: str
    quote: str
    statement: str
    axis: Axis
    polarity: Polarity
    weight: float
    is_hard_flag: bool


class _Classified(BaseModel):
    items: list[_Item]


def vendor_payload(ctx: Context) -> dict[str, str]:
    return {
        "name": ctx.identity.vendor_name,
        "product": ctx.identity.product_name,
        "domain": ctx.domain,
        "legal_entity": ctx.identity.legal_entity,
        "country": ctx.identity.country,
    }


def entity_names(ctx: Context) -> set[str]:
    """Names that identify the product being judged (for entity matching of negatives)."""
    names = {ctx.identity.vendor_name, ctx.identity.product_name, ctx.domain.split(".")[0]}
    return {n for n in names if n}


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


def targeted_queries(ctx: Context) -> list[tuple[str, dict[str, list[str]]]]:
    """Searches for strong evidence: funding news, customers naming the vendor on their own
    sites, review platforms with counts."""
    name = search_name(ctx)
    exclude = {"exclude_domains": [ctx.domain]}
    return [
        (f'"{name}" funding OR raises OR Series', exclude),
        (f'"{name}" case study OR "we use {name}"', exclude),
        (f'"{name}" reviews', {"include_domains": TARGETED_REVIEW_SITES}),
    ]


def strong_sites(facts: list[Fact]) -> set[str]:
    return {
        registered_domain(f.source_url)
        for f in facts
        if f.polarity == Polarity.POSITIVE and evidence_class(f) == "strong"
    }


def _keep(result: TavilyResult, ctx: Context) -> bool:
    return bool(result.content) and not same_site(result.url, ctx.domain)


def calibrate(item: _Item, evidence: str = "normal") -> tuple[float, bool, bool]:
    """(weight, is_hard_flag, is_filler) after the footprint rules. The weight comes from the
    evidence class (CLASS_WEIGHTS), not from the model."""
    topic = item.topic if item.topic in TOPICS else "other"
    weight = CLASS_WEIGHTS[evidence]
    hard = item.is_hard_flag
    if topic == "reviews" and item.polarity == Polarity.NEGATIVE:
        weight, hard = min(weight, MINOR_REVIEW_WEIGHT), False
    return weight, hard, topic == "directory"


async def _search_all(
    ctx: Context, plan: list[tuple[str, dict[str, list[str]]]], seen: dict[str, TavilyResult]
) -> list[TavilyResult]:
    """Run the searches in parallel; return the results not seen before (and record them)."""
    if ctx.tavily is None:
        return []
    batches = await asyncio.gather(
        *(ctx.tavily.search(q, max_results=MAX_RESULTS_PER_QUERY, **p) for q, p in plan)
    )
    new: list[TavilyResult] = []
    for batch in batches:
        for r in batch:
            if _keep(r, ctx) and r.url not in seen:
                seen[r.url] = r
                new.append(r)
    return new


async def _classify(ctx: Context, results: list[TavilyResult], step: str) -> list[_Item]:
    if not results:
        return []
    return await classify_in_chunks(
        ctx.llm,
        prompt="classify_results.md",
        vendor=vendor_payload(ctx),
        results=results,
        output=_Classified,
        step=step,
        snippet_chars=SNIPPET_CHARS,
    )


async def collect(ctx: Context) -> list[Fact]:
    if ctx.tavily is None:
        return []
    today = datetime.now(UTC)
    seen: dict[str, TavilyResult] = {}
    base = (await _search_all(ctx, queries(ctx), seen))[:MAX_RESULTS_TO_CLASSIFY]
    facts = facts_from(await _classify(ctx, base, "classify_footprint"), seen, ctx, today, [])
    if len(strong_sites(facts)) < MIN_STRONG_SOURCES:
        more = await _search_all(ctx, targeted_queries(ctx), seen)
        items = await _classify(ctx, more, "classify_footprint_targeted")
        facts = facts_from(items, seen, ctx, today, facts)
    logger.info(
        "footprint %s: %d results, %d facts, %d strong sites",
        ctx.domain,
        len(seen),
        len(facts),
        len(strong_sites(facts)),
    )
    return facts


def facts_from(
    items: list[_Item],
    seen: dict[str, TavilyResult],
    ctx: Context,
    today: datetime,
    facts: list[Fact],
) -> list[Fact]:
    """``facts`` plus the facts from classified items (quote checked against the result)."""
    facts = list(facts)
    for item in items:
        source = seen.get(item.url)
        if source is None or not item.relevant or not item.independent:
            continue
        if not item.quote or not contains_quote(source.content, item.quote):
            continue
        if item.topic != "reviews" and not names_entity(item.quote, ctx.domain, entity_names(ctx)):
            continue  # about a sister brand, a parent or a namesake (either polarity)
        topic = item.topic if item.topic in TOPICS else "other"
        # Review counts and ratings are written as data by our code; the quote stays evidence.
        data = review_statement(item.url, item.quote) if topic == "reviews" else ""
        fact = Fact(
            id=fact_id(NAME, len(facts)),
            collector=NAME,
            statement=data or item.statement,
            code_worded=bool(data),
            source_type=source_type_for(item.url, item.source_type),
            axis=item.axis,
            polarity=item.polarity,
            weight=0.0,
            source_url=item.url,
            quote=item.quote,
            source_kind=SourceKind.INDEPENDENT,
            fetched_at=today,
            topic=topic,
            is_filler=topic == "directory",
        )
        weight, hard, filler = calibrate(item, evidence_class(fact))
        facts.append(
            fact.model_copy(update={"weight": weight, "is_hard_flag": hard, "is_filler": filler})
        )
    return facts
