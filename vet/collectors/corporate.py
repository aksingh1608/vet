"""Corporate: regulator and court findings, ownership and names, licensing clarity, lifecycle
statements and sanctions exposure. Evidence a reader of the vendor's site alone would miss.

Searches run in parallel through Tavily; results are classified in chunks by the fast model
(``corporate_results.md``) and kept only when the quote is on the result and names this
product or vendor. German variants run for German-speaking vendors.

Lifecycle needs no search: the pages the site reader already fetched are scanned for
statements against interest (legacy, successor, end of life, discontinued, coming soon). Those
are vendor statements and count as evidence because they are against the vendor's interest.

These facts feed the buyer view only. The orchestrator keeps them away from the two-axis judge,
so today's two-axis labels cannot move.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime

from pydantic import BaseModel

from vet.collectors.base import Context, fact_id
from vet.collectors.classify import classify_in_chunks
from vet.collectors.footprint import entity_names, search_name, vendor_payload
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.sources import names_entity, source_type_for
from vet.tavily import TavilyResult
from vet.text import contains_quote, registered_domain, same_site

logger = logging.getLogger(__name__)

NAME = "corporate"
MAX_RESULTS_PER_QUERY = 5
MAX_RESULTS_TO_CLASSIFY = 24
SNIPPET_CHARS = 1200
GERMAN_SPEAKING = {"DE", "AT", "CH"}
LIFECYCLE_START = 50  # fact numbering offset for page-scan facts
MAX_LIFECYCLE_FACTS = 3
SENTENCE_CHARS = 300

# event -> polarity. Every corporate fact has weight 0: the two-axis scorer never sees them.
EVENTS: dict[str, Polarity] = {
    "regulator_finding": Polarity.NEGATIVE,
    "complaint_or_lawsuit": Polarity.NEUTRAL,
    "ownership_change": Polarity.NEUTRAL,
    "divestment_non_core": Polarity.NEGATIVE,
    "rename": Polarity.NEUTRAL,
    "licensing_change": Polarity.NEGATIVE,
    "lifecycle_end": Polarity.NEGATIVE,
    "sanctions_exposure": Polarity.NEGATIVE,
    "sanctions_exit": Polarity.NEUTRAL,
    "security_incident": Polarity.NEGATIVE,
    "layoffs_or_price_squeeze": Polarity.NEGATIVE,
}
NEGATIVE_NEEDS_NAME = frozenset(
    {"regulator_finding", "lifecycle_end", "sanctions_exposure", "security_incident"}
)

LIFECYCLE_WORDING = re.compile(
    r"\bend[\s-]of[\s-]life\b|\bend[\s-]of[\s-](?:support|sale)\b|\bdiscontinued\b"
    r"|\bno\s+longer\s+(?:be\s+)?(?:supported|developed|maintained|available|sold)\b"
    r"|\b(?:has\s+been|was|is\s+being|will\s+be)\s+(?:replaced|superseded|retired|sunset)\b"
    r"|\bsuccessor\s+(?:product|to)\b|\blegacy\s+(?:product|version|edition)\b"
    r"|\bcoming\s+soon\b|\bshutting\s+down\b|\bwill\s+shut\s+down\b"
    r"|abkündigung|\beingestellt\b|supportende|\bnachfolger\b|\bnicht\s+mehr\s+unterstützt\b",
    re.I,
)
# Of the lifecycle wording, only this says the product ends (the rest says it was superseded).
END_WORDING = re.compile(
    r"\bend[\s-]of[\s-]life\b|\bend[\s-]of[\s-](?:support|sale)\b|\bdiscontinued\b"
    r"|\bno\s+longer\s+(?:be\s+)?(?:supported|developed|maintained|available|sold)\b"
    r"|\bshutting\s+down\b|\bwill\s+shut\s+down\b|\bretired\b"
    r"|abkündigung|\beingestellt\b|supportende|\bnicht\s+mehr\s+unterstützt\b",
    re.I,
)
# Ending support for OLD versions is normal maintenance, not the product's end of life.
OLD_VERSIONS = re.compile(
    r"\b(?:older|old|previous|earlier|prior)\s+(?:\w+\s+){0,3}?"
    r"(?:versions?|releases?|editions?|generations?)\b"
    r"|\b(?:ältere|frühere|vorherige)n?\s+(?:\w+\s+){0,2}?(?:versionen?|releases?)\b",
    re.I,
)
_SENTENCE = re.compile(r"[^.!?\n]*(?:[.!?]|$)")


class _Item(BaseModel):
    url: str
    about_this_vendor: bool
    independent: bool
    event: str
    concerns_product: bool
    year: str
    counterparty: str
    source_type: str
    quote: str
    statement: str


class _Classified(BaseModel):
    items: list[_Item]


def queries(ctx: Context) -> list[str]:
    name = search_name(ctx)
    plan = [
        f'"{name}" FTC OR regulator OR "data protection authority" OR "consent order" OR ruling '
        "OR fined OR deceptive",
        f'"{name}" "acquired by" OR owner OR "private equity" OR divested OR carve-out '
        "OR formerly OR renamed",
        f'"{name}" renamed OR rebranded OR "licensing change" OR "pricing change" '
        'OR "difference between"',
        f'"{name}" Russia OR sanctions OR "still operating" OR "continues business"',
    ]
    if ctx.identity.country in GERMAN_SPEAKING or ctx.domain.endswith((".de", ".at", ".ch")):
        plan += [
            f'"{name}" Bußgeld OR Datenschutzbehörde OR Urteil OR Verbraucherzentrale',
            f'"{name}" übernommen OR umbenannt OR Abkündigung OR eingestellt OR Supportende',
        ]
    return plan


def _year(text: str) -> int | None:
    m = re.search(r"\b(19[89]\d|20\d{2})\b", text)
    return int(m.group(1)) if m else None


def facts_from(
    items: list[_Item], seen: dict[str, TavilyResult], ctx: Context, today: datetime
) -> list[Fact]:
    facts: list[Fact] = []
    names = entity_names(ctx)
    for item in items:
        source = seen.get(item.url)
        if source is None or not item.about_this_vendor or item.event not in EVENTS:
            continue
        if not item.quote or not contains_quote(source.content, item.quote):
            continue
        own = same_site(item.url, ctx.domain)
        if item.event in NEGATIVE_NEEDS_NAME and not names_entity(
            item.quote, ctx.domain, names, own_site=own
        ):
            continue  # about a namesake, a sister brand or a parent, not this product
        year = _year(item.year) or _year(item.quote)
        kind = "vendor" if own else source_type_for(item.url, item.source_type)
        tags = ["scope:product" if item.concerns_product else "scope:parent"]
        if item.counterparty.strip():
            tags.append(f"counterparty:{item.counterparty.strip()[:80]}")
        if not own and not item.independent:
            tags.append("vendor_origin")
        if item.event == "lifecycle_end":
            end = END_WORDING.search(item.quote) and not OLD_VERSIONS.search(item.quote)
            tags.append("lifecycle:end" if end else "lifecycle:superseded")
        facts.append(
            Fact(
                id=fact_id(NAME, len(facts)),
                collector=NAME,
                statement=item.statement,
                axis=Axis.TRUST,
                polarity=EVENTS[item.event],
                weight=0.0,
                source_url=item.url,
                quote=item.quote,
                source_kind=SourceKind.VENDOR if own else SourceKind.INDEPENDENT,
                fetched_at=today,
                topic=item.event,
                source_type=kind,
                tags=tags,
                observed_at=datetime(year, 1, 1, tzinfo=UTC) if year else None,
            )
        )
    return facts


def lifecycle_facts(ctx: Context, today: datetime) -> list[Fact]:
    """Statements against interest on pages already read (no new request)."""
    facts: list[Fact] = []
    seen: set[str] = set()
    for page in ctx.pages:
        if not page.text or page.archived:
            continue
        for match in LIFECYCLE_WORDING.finditer(page.text):
            sentence = _sentence_around(page.text, match.start())
            key = sentence.lower()
            if not sentence or key in seen or OLD_VERSIONS.search(sentence):
                continue
            seen.add(key)
            domain = registered_domain(page.final_url or page.url)
            facts.append(
                Fact(
                    id=fact_id(NAME, LIFECYCLE_START + len(facts)),
                    collector=NAME,
                    statement=f'{domain} ({page.kind} page): "{sentence}"',
                    code_worded=True,
                    axis=Axis.REAL_ALIVE,
                    polarity=Polarity.NEGATIVE,
                    weight=0.0,
                    source_url=page.final_url or page.url,
                    quote=sentence,
                    source_kind=SourceKind.VENDOR,
                    fetched_at=today,
                    topic="lifecycle_statement",
                    source_type="vendor",
                    tags=[
                        "statement_against_interest",
                        "scope:product",
                        "lifecycle:end" if END_WORDING.search(sentence) else "lifecycle:superseded",
                    ],
                )
            )
            if len(facts) >= MAX_LIFECYCLE_FACTS:
                return facts
    return facts


def _sentence_around(text: str, pos: int) -> str:
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos)) + 1
    m = _SENTENCE.match(text, start)
    sentence = (m.group(0) if m else text[start : pos + SENTENCE_CHARS]).strip()
    return sentence[:SENTENCE_CHARS]


async def search_facts(ctx: Context, today: datetime) -> list[Fact]:
    if ctx.tavily is None:
        return []
    answers = await asyncio.gather(
        *(ctx.tavily.search(q, max_results=MAX_RESULTS_PER_QUERY) for q in queries(ctx)),
        return_exceptions=True,
    )
    seen: dict[str, TavilyResult] = {}
    for answer in answers:
        if isinstance(answer, BaseException):
            if isinstance(answer, asyncio.CancelledError):
                raise answer
            logger.warning("corporate search failed for %s: %s", ctx.domain, answer)
            continue
        for r in answer:
            if r.content and r.url not in seen:
                seen[r.url] = r
    results = list(seen.values())[:MAX_RESULTS_TO_CLASSIFY]
    if not results:
        return []
    items: list[_Item] = await classify_in_chunks(
        ctx.llm,
        prompt="corporate_results.md",
        vendor=vendor_payload(ctx),
        results=results,
        output=_Classified,
        step="classify_corporate",
        snippet_chars=SNIPPET_CHARS,
    )
    return facts_from(items, seen, ctx, today)


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    facts = lifecycle_facts(ctx, today)
    facts += await search_facts(ctx, today)
    logger.info("corporate %s: %d facts", ctx.domain, len(facts))
    return facts
