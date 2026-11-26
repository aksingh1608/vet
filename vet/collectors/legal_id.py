"""Legal identity: does the legal entity named on the site exist in a company register?

* UK entities: Companies House API (key from .env).
* Everyone else with a named entity: North Data, found only through Tavily
  (include_domains). North Data pages are never fetched; the Tavily snippet is the quote.
"""

import base64
import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from pydantic import BaseModel

from vet.collectors.base import Context, fact_id
from vet.llm import load_prompt
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.text import contains_quote, normalize

logger = logging.getLogger(__name__)

NAME = "legal_id"
CH_API = "https://api.company-information.service.gov.uk"
CH_PAGE = "https://find-and-update.company-information.service.gov.uk/company/{number}"
NORTHDATA_DOMAINS = ["northdata.com", "northdata.de"]
NORTHDATA_MAX_RESULTS = 5
UK_NUMBER = re.compile(r"\b((?:SC|NI|OC|SO|NC|R0)?\d{6,8})\b", re.I)
UK_FORMS = (" ltd", " limited", " plc", " llp")
BAD_STATUSES = (
    "liquidation",
    "insolvency",
    "dissolved",
    "administration",
    "receivership",
    "converted-closed",
    "removed",
)


class _Record(BaseModel):
    url: str
    same_entity: bool
    register_number: str
    register_court: str
    founded: str
    status: str
    quote: str


class _Records(BaseModel):
    records: list[_Record]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def is_uk(ctx: Context) -> bool:
    ident = ctx.identity
    entity = f" {ident.legal_entity.lower()}"
    return (
        ident.country == "GB"
        or ctx.domain.endswith(".uk")
        or entity.endswith(UK_FORMS)
        or "companies house" in ident.register_court.lower()
    )


def _year(text: str) -> int | None:
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", text)
    return int(m.group(1)) if m else None


# ---- Companies House ----------------------------------------------------------------------


def _ch_headers(ctx: Context) -> dict[str, str]:
    key = ctx.settings.companies_house_api_key
    if key is None:
        raise ValueError("COMPANIES_HOUSE_API_KEY is not set")
    token = base64.b64encode(f"{key.get_secret_value()}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _entity_key(name: str) -> str:
    n = normalize(name).replace(".", "")
    for suffix in (" limited", " ltd", " plc", " llp"):
        n = n.removesuffix(suffix)
    return n.strip()


async def _ch_find_number(ctx: Context) -> str:
    m = UK_NUMBER.search(ctx.identity.register_number)
    if m:
        return m.group(1).upper().zfill(8)
    if not ctx.identity.legal_entity:
        return ""
    query = urlencode({"q": ctx.identity.legal_entity, "items_per_page": 5})
    url = f"{CH_API}/search/companies?{query}"
    data = await ctx.fetcher.get_json(url, headers=_ch_headers(ctx))
    want = _entity_key(ctx.identity.legal_entity)
    for item in data.get("items", []):
        if _entity_key(item.get("title", "")) == want:
            return str(item.get("company_number", ""))
    return ""


async def companies_house_facts(ctx: Context, today: datetime) -> list[Fact]:
    number = await _ch_find_number(ctx)
    if not number:
        return []
    result = await ctx.fetcher.fetch_api(f"{CH_API}/company/{number}", headers=_ch_headers(ctx))
    if result.status == 404:
        if ctx.identity.register_number:
            return [
                Fact(
                    id=fact_id(NAME, 0),
                    collector=NAME,
                    statement=f"Companies House has no company with the number {number} that the "
                    f"site gives",
                    axis=Axis.TRUST,
                    polarity=Polarity.NEGATIVE,
                    weight=0.7,
                    source_url=CH_PAGE.format(number=number),
                    quote="",
                    source_kind=SourceKind.RECORD,
                    fetched_at=today,
                    is_hard_flag=True,
                )
            ]
        return []
    if not result.ok:
        return []
    profile = json.loads(result.text)
    name = profile.get("company_name", "")
    status = str(profile.get("company_status", "unknown"))
    created = profile.get("date_of_creation", "")
    page = CH_PAGE.format(number=number)
    bad = any(b in status for b in BAD_STATUSES)
    facts = [
        Fact(
            id=fact_id(NAME, 1),
            collector=NAME,
            statement=f"Companies House lists {name} (company {number}) with status '{status}'",
            axis=Axis.TRUST,
            polarity=Polarity.NEGATIVE if bad else Polarity.POSITIVE,
            weight=0.9 if bad else 0.5,
            source_url=page,
            quote="",
            source_kind=SourceKind.RECORD,
            fetched_at=today,
            is_hard_flag=bad,
        )
    ]
    if created:
        years = (today - datetime.fromisoformat(created).replace(tzinfo=UTC)).days / 365.25
        facts.append(
            Fact(
                id=fact_id(NAME, 2),
                collector=NAME,
                statement=f"Companies House shows {name} was incorporated on {created}",
                axis=Axis.REAL_ALIVE,
                polarity=Polarity.POSITIVE if years >= 2 else Polarity.NEUTRAL,
                weight=0.4 if years >= 5 else 0.2,
                source_url=page,
                quote="",
                source_kind=SourceKind.RECORD,
                fetched_at=today,
            )
        )
    if ctx.identity.legal_entity and _entity_key(name) != _entity_key(ctx.identity.legal_entity):
        facts.append(
            Fact(
                id=fact_id(NAME, 3),
                collector=NAME,
                statement=f"The site names '{ctx.identity.legal_entity}', but company {number} at "
                f"Companies House is '{name}'",
                axis=Axis.TRUST,
                polarity=Polarity.NEGATIVE,
                weight=0.6,
                source_url=page,
                quote="",
                source_kind=SourceKind.RECORD,
                fetched_at=today,
            )
        )
    return facts


# ---- North Data via Tavily ----------------------------------------------------------------


def northdata_query(ctx: Context) -> str:
    ident = ctx.identity
    return f'"{ident.legal_entity}" {ident.register_number}'.strip()


async def northdata_facts(ctx: Context, today: datetime) -> list[Fact]:
    if ctx.tavily is None:
        return []
    results = await ctx.tavily.search(
        northdata_query(ctx),
        include_domains=NORTHDATA_DOMAINS,
        max_results=NORTHDATA_MAX_RESULTS,
    )
    results = [r for r in results if r.content]
    if not results:
        return []
    payload = {
        "entity": ctx.identity.legal_entity,
        "claimed_register_number": ctx.identity.register_number,
        "claimed_register_court": ctx.identity.register_court,
        "snippets": [{"url": r.url, "title": r.title, "content": r.content} for r in results],
    }
    out = await ctx.llm.structured(
        kind="fast",
        system=load_prompt("legal_record.md"),
        user=f"<data>\n{json.dumps(payload, ensure_ascii=False)}\n</data>",
        output=_Records,
        step="legal_record",
    )
    content = {r.url: r.content for r in results}
    facts: list[Fact] = []
    for rec in out.records:
        snippet = content.get(rec.url)
        if not rec.same_entity or snippet is None:
            continue
        if not rec.quote or not contains_quote(snippet, rec.quote):
            continue
        n = len(facts)
        reg = " ".join(x for x in (rec.register_court, rec.register_number) if x)
        facts.append(
            Fact(
                id=fact_id(NAME, 10 + n),
                collector=NAME,
                statement=f"North Data has a register record for {ctx.identity.legal_entity}"
                + (f" ({reg})" if reg else ""),
                axis=Axis.TRUST,
                polarity=Polarity.POSITIVE,
                weight=0.45,
                source_url=rec.url,
                quote=rec.quote,
                source_kind=SourceKind.RECORD,
                fetched_at=today,
            )
        )
        status = rec.status.lower()
        if any(b in status for b in BAD_STATUSES):
            facts.append(
                Fact(
                    id=fact_id(NAME, 11 + n),
                    collector=NAME,
                    statement=f"North Data shows {ctx.identity.legal_entity} "
                    f"with status '{status}'",
                    axis=Axis.TRUST,
                    polarity=Polarity.NEGATIVE,
                    weight=0.9,
                    source_url=rec.url,
                    quote=rec.quote,
                    source_kind=SourceKind.RECORD,
                    fetched_at=today,
                    is_hard_flag=True,
                )
            )
        year = _year(rec.founded)
        if year and contains_quote(snippet, rec.founded):
            age = today.year - year
            facts.append(
                Fact(
                    id=fact_id(NAME, 12 + n),
                    collector=NAME,
                    statement=f"North Data shows {ctx.identity.legal_entity} registered in {year}",
                    axis=Axis.REAL_ALIVE,
                    polarity=Polarity.POSITIVE if age >= 2 else Polarity.NEUTRAL,
                    weight=0.4 if age >= 5 else 0.2,
                    source_url=rec.url,
                    quote=rec.founded,
                    source_kind=SourceKind.RECORD,
                    fetched_at=today,
                )
            )
        claimed = _digits(ctx.identity.register_number)
        found = _digits(rec.register_number)
        if claimed and found and claimed != found and contains_quote(snippet, rec.register_number):
            facts.append(
                Fact(
                    id=fact_id(NAME, 13 + n),
                    collector=NAME,
                    statement=f"The register number on North Data ({rec.register_number}) differs "
                    f"from the one on the site ({ctx.identity.register_number})",
                    axis=Axis.TRUST,
                    polarity=Polarity.NEGATIVE,
                    weight=0.6,
                    source_url=rec.url,
                    quote=rec.register_number,
                    source_kind=SourceKind.RECORD,
                    fetched_at=today,
                )
            )
        break  # one matching record is enough
    return facts


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    if not ctx.identity.legal_entity and not ctx.identity.register_number:
        logger.info("legal_id: no legal entity named on %s", ctx.domain)
        return []
    if is_uk(ctx) and ctx.settings.companies_house_api_key is not None:
        facts = await companies_house_facts(ctx, today)
        if facts:
            return facts
    if ctx.identity.legal_entity:
        return await northdata_facts(ctx, today)
    return []
