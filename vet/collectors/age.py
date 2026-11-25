"""Age: when did this domain appear? RDAP registration, first logged certificate (crt.sh,
optional) and Wayback Machine history. No LLM. Needs only the domain, so it starts at once."""

import asyncio
import json
import logging
from datetime import UTC, datetime

from vet.collectors.base import Context, fact_id
from vet.fetch import FetchError
from vet.schemas import Axis, Fact, Polarity, SourceKind

logger = logging.getLogger(__name__)

NAME = "age"
RDAP_URL = "https://rdap.org/domain/{domain}"
CRTSH_URL = "https://crt.sh/?q={domain}&output=json"
CDX_FIRST_URL = (
    "https://web.archive.org/cdx/search/cdx?url={domain}&output=json&fl=timestamp&limit=1"
)
CDX_YEARS_URL = (
    "https://web.archive.org/cdx/search/cdx?url={domain}&output=json&fl=timestamp"
    "&collapse=timestamp:4"
)
SNAPSHOT_URL = "https://web.archive.org/web/{ts}/{domain}/"
# crt.sh is optional: rate-limited to 4/min, so cap the total wait including the queue.
CRTSH_DEADLINE_S = 40.0
RECENT_CAPTURE_YEARS = 1


def _years_since(when: datetime, today: datetime) -> float:
    return (today - when).days / 365.25


def age_judgement(years: float) -> tuple[Polarity, float]:
    """Polarity and weight for 'this record first appears N years ago' (real_alive axis)."""
    if years >= 10:
        return Polarity.POSITIVE, 0.5
    if years >= 5:
        return Polarity.POSITIVE, 0.4
    if years >= 2:
        return Polarity.POSITIVE, 0.15
    if years >= 1:
        return Polarity.NEUTRAL, 0.1
    return Polarity.NEGATIVE, 0.5


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


async def rdap_facts(ctx: Context, today: datetime) -> list[Fact]:
    url = RDAP_URL.format(domain=ctx.domain)
    result = await ctx.fetcher.fetch_api(url, headers={"Accept": "application/rdap+json"})
    if not result.ok:
        return []
    data = json.loads(result.text)
    dates = [
        e.get("eventDate", "")
        for e in data.get("events", [])
        if e.get("eventAction") == "registration" and e.get("eventDate")
    ]
    if not dates:  # e.g. .de: DENIC publishes no creation date
        return []
    raw = min(dates)
    created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    polarity, weight = age_judgement(_years_since(created, today))
    return [
        Fact(
            id=fact_id(NAME, 0),
            collector=NAME,
            statement=f"The domain {ctx.domain} was registered on {created.date().isoformat()} "
            f"(RDAP registration record)",
            axis=Axis.REAL_ALIVE,
            polarity=polarity,
            weight=weight,
            source_url=url,
            quote=raw,
            source_kind=SourceKind.RECORD,
            fetched_at=today,
        )
    ]


async def crtsh_facts(ctx: Context, today: datetime) -> list[Fact]:
    url = CRTSH_URL.format(domain=ctx.domain)
    try:
        async with asyncio.timeout(CRTSH_DEADLINE_S):
            result = await ctx.fetcher.fetch_api(url)
    except (TimeoutError, FetchError) as exc:
        logger.info("crt.sh unavailable for %s (%s); continuing without it", ctx.domain, exc)
        return []
    if not result.ok:
        return []
    try:
        rows = json.loads(result.text)
    except json.JSONDecodeError:
        return []
    stamps = [r["not_before"] for r in rows if isinstance(r, dict) and r.get("not_before")]
    if not stamps:
        return []
    raw = min(stamps)
    first = datetime.fromisoformat(raw).replace(tzinfo=UTC)
    polarity, weight = age_judgement(_years_since(first, today))
    return [
        Fact(
            id=fact_id(NAME, 1),
            collector=NAME,
            statement=f"The earliest certificate logged for {ctx.domain} was valid from "
            f"{first.date().isoformat()} (crt.sh certificate transparency log)",
            axis=Axis.REAL_ALIVE,
            polarity=polarity,
            weight=weight,
            source_url=url,
            quote=raw,
            source_kind=SourceKind.RECORD,
            fetched_at=today,
        )
    ]


async def wayback_facts(ctx: Context, today: datetime) -> list[Fact]:
    first_url = CDX_FIRST_URL.format(domain=ctx.domain)
    rows = await ctx.fetcher.get_json(first_url)
    captures = [r[0] for r in rows[1:]] if isinstance(rows, list) else []
    if not captures:
        return [
            Fact(
                id=fact_id(NAME, 2),
                collector=NAME,
                statement=f"The Wayback Machine has no captures of {ctx.domain}",
                axis=Axis.REAL_ALIVE,
                polarity=Polarity.NEGATIVE,
                weight=0.4,
                source_url=first_url,
                quote="",
                source_kind=SourceKind.RECORD,
                fetched_at=today,
            )
        ]
    ts = captures[0]
    first = _parse_ts(ts)
    polarity, weight = age_judgement(_years_since(first, today))
    facts = [
        Fact(
            id=fact_id(NAME, 2),
            collector=NAME,
            statement=f"The earliest Wayback Machine capture of {ctx.domain} is from "
            f"{first.date().isoformat()}; there are no earlier captures",
            axis=Axis.REAL_ALIVE,
            polarity=polarity,
            weight=weight,
            source_url=SNAPSHOT_URL.format(ts=ts, domain=ctx.domain),
            quote="",
            source_kind=SourceKind.RECORD,
            fetched_at=today,
        )
    ]

    years_url = CDX_YEARS_URL.format(domain=ctx.domain)
    year_rows = await ctx.fetcher.get_json(years_url)
    years = sorted({int(r[0][:4]) for r in year_rows[1:]}) if isinstance(year_rows, list) else []
    if years:
        span = years[-1] - years[0] + 1
        latest = years[-1]
        if today.year - latest > RECENT_CAPTURE_YEARS:
            polarity, weight = Polarity.NEGATIVE, 0.3
            note = f"the most recent capture is from {latest}"
        elif span >= 5 and len(years) / span >= 0.7:
            polarity, weight = Polarity.POSITIVE, 0.3
            note = "captured steadily up to now"
        else:
            polarity, weight = Polarity.NEUTRAL, 0.1
            note = f"latest capture year {latest}"
        facts.append(
            Fact(
                id=fact_id(NAME, 3),
                collector=NAME,
                statement=f"The Wayback Machine has captures of {ctx.domain} in "
                f"{len(years)} of the "
                f"{span} years from {years[0]} to {latest}; {note}",
                axis=Axis.REAL_ALIVE,
                polarity=polarity,
                weight=weight,
                source_url=years_url,
                quote="",
                source_kind=SourceKind.RECORD,
                fetched_at=today,
            )
        )
    return facts


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    parts = await asyncio.gather(
        rdap_facts(ctx, today),
        crtsh_facts(ctx, today),
        wayback_facts(ctx, today),
        return_exceptions=True,
    )
    facts: list[Fact] = []
    for name, part in zip(("rdap", "crt.sh", "wayback"), parts, strict=True):
        if isinstance(part, BaseException):
            logger.warning("age/%s failed for %s: %s", name, ctx.domain, part)
            continue
        facts.extend(part)
    return facts
