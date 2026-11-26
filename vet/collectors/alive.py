"""Alive: is the product still being built?

* Changelog / blog / release-notes dates on the vendor's own pages. These are vendor-sourced,
  so a recent date never counts in the vendor's favour; a stale one (nothing for 18 months)
  counts against it as a sign of a frozen product.
* Public records linked from the site: the GitHub account's most recently pushed repository,
  the latest PyPI or npm release, the App Store listing's latest version. Each is linked to the
  public page and verified against the platform's API response.

Only accounts and packages whose name matches the vendor are used, so a site cannot borrow a
famous project's activity by linking to it.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote as urlquote
from urllib.parse import urlsplit

from vet.collectors.base import Context, SitePage, fact_id
from vet.fetch import FetchError
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.text import extract_links, normalize

logger = logging.getLogger(__name__)

NAME = "alive"
RECENT_DAYS = 90
ACTIVE_DAYS = 365
STALE_DAYS = 548  # 18 months
FROZEN_DAYS = 730
DATED_PAGE_KINDS = ("changelog", "blog")

GITHUB_API = "https://api.github.com/users/{owner}/repos?sort=pushed&per_page=10"
PYPI_API = "https://pypi.org/pypi/{name}/json"
NPM_API = "https://registry.npmjs.org/{name}"
ITUNES_API = "https://itunes.apple.com/lookup?id={id}"
GITHUB_RESERVED = frozenset(
    {
        "about",
        "features",
        "pricing",
        "login",
        "join",
        "sponsors",
        "marketplace",
        "orgs",
        "topics",
        "collections",
        "explore",
        "enterprise",
        "security",
        "settings",
        "apps",
        "site",
    }
)

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("january", "jan", "januar", "jän"),
            ("february", "feb", "februar"),
            ("march", "mar", "märz", "maerz"),
            ("april", "apr"),
            ("may", "mai"),
            ("june", "jun", "juni"),
            ("july", "jul", "juli"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct", "oktober", "okt"),
            ("november", "nov"),
            ("december", "dec", "dezember", "dez"),
        ]
    )
    for m in names
}
_MONTH_RE = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
_DATE_PATTERNS = [
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b"), ("d", "m", "y")),
    (re.compile(rf"\b({_MONTH_RE})\.? (\d{{1,2}}),? (20\d{{2}})\b", re.I), ("M", "d", "y")),
    (re.compile(rf"\b(\d{{1,2}})\.? ({_MONTH_RE})\.? (20\d{{2}})\b", re.I), ("d", "M", "y")),
]


def find_dates(text: str, today: datetime) -> list[tuple[datetime, str]]:
    """(date, text as written) for every plausible date up to today."""
    out: list[tuple[datetime, str]] = []
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            parts = dict(zip(order, m.groups(), strict=True))
            month = _MONTHS.get(parts["M"].lower()) if "M" in parts else int(parts["m"])
            try:
                when = datetime(int(parts["y"]), month or 0, int(parts["d"]), tzinfo=UTC)
            except ValueError:
                continue
            if when <= today + timedelta(days=1):
                out.append((when, m.group(0)))
    return out


def _recency(days: int) -> tuple[Polarity, float, str]:
    if days <= RECENT_DAYS:
        return Polarity.POSITIVE, 0.35, "release_activity"
    if days <= ACTIVE_DAYS:
        return Polarity.POSITIVE, 0.2, "release_activity"
    if days >= FROZEN_DAYS:
        return Polarity.NEGATIVE, 0.35, "frozen"
    return Polarity.NEUTRAL, 0.1, "release_activity"


def page_date_facts(pages: list[SitePage], today: datetime) -> list[Fact]:
    facts: list[Fact] = []
    for page in pages:
        if page.kind not in DATED_PAGE_KINDS or not page.text:
            continue
        dates = find_dates(page.text, today)
        if not dates:
            continue
        latest, written = max(dates)
        days = (today - latest).days
        stale = days >= STALE_DAYS
        facts.append(
            Fact(
                id=fact_id(NAME, len(facts)),
                collector=NAME,
                statement=f"The newest date on the vendor's {page.kind} page is {written}",
                axis=Axis.REAL_ALIVE,
                polarity=Polarity.NEGATIVE if stale else Polarity.POSITIVE,
                weight=0.35 if stale else 0.2,
                source_url=page.final_url or page.url,
                quote=written,
                source_kind=SourceKind.VENDOR,
                fetched_at=today,
                topic="frozen" if stale else "release_activity",
                observed_at=latest,
            )
        )
    return facts


def _name_matches(candidate: str, ctx: Context) -> bool:
    """The account or package belongs to this vendor by name."""
    c = re.sub(r"[^a-z0-9]", "", candidate.lower())
    names = {ctx.domain.split(".")[0], ctx.identity.vendor_name, ctx.identity.product_name}
    keys = {re.sub(r"[^a-z0-9]", "", normalize(n)) for n in names if n}
    if len(c) < 3:
        return False
    return any(len(k) >= 3 and (k in c or c in k) for k in keys)


def linked_accounts(pages: list[SitePage], ctx: Context) -> dict[str, str]:
    """{'github': owner, 'pypi': name, 'npm': name, 'appstore': id} found in the site's links."""
    found: dict[str, str] = {}
    for page in pages:
        if not page.raw:
            continue
        for url, _ in extract_links(page.raw, page.final_url or page.url):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().removeprefix("www.")
            segs = [s for s in parts.path.split("/") if s]
            if host == "github.com" and segs and segs[0].lower() not in GITHUB_RESERVED:
                if _name_matches(segs[0], ctx):
                    found.setdefault("github", segs[0])
            elif host == "pypi.org" and len(segs) >= 2 and segs[0] == "project":
                if _name_matches(segs[1], ctx):
                    found.setdefault("pypi", segs[1])
            elif host == "npmjs.com" and len(segs) >= 2 and segs[0] == "package":
                name = "/".join(segs[1:3]) if segs[1].startswith("@") else segs[1]
                if _name_matches(name, ctx):
                    found.setdefault("npm", name)
            elif host == "apps.apple.com":
                m = re.search(r"id(\d{6,})", parts.path)
                if m:
                    found.setdefault("appstore", m.group(1))
                    found.setdefault("appstore_url", url)
    return found


def _record_fact(
    n: int, statement: str, when: datetime, raw: str, url: str, check: str, today: datetime
) -> Fact:
    polarity, weight, topic = _recency((today - when).days)
    return Fact(
        id=fact_id(NAME, n),
        collector=NAME,
        statement=statement,
        axis=Axis.REAL_ALIVE,
        polarity=polarity,
        weight=weight,
        source_url=url,
        check_url=check,
        quote=raw,
        source_kind=SourceKind.RECORD,
        fetched_at=today,
        topic=topic,
        observed_at=when,
    )


def _iso(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


async def github_fact(ctx: Context, owner: str, n: int, today: datetime) -> Fact | None:
    api = GITHUB_API.format(owner=owner)
    repos = await ctx.fetcher.get_json(api, headers={"Accept": "application/vnd.github+json"})
    live = [r for r in repos if not r.get("fork") and not r.get("archived") and r.get("pushed_at")]
    if not live:
        return None
    repo = max(live, key=lambda r: r["pushed_at"])
    when = _iso(repo["pushed_at"])
    return _record_fact(
        n,
        f"The most recently updated public repository of github.com/{owner} "
        f"({repo['name']}) was last pushed on {when.date().isoformat()}",
        when,
        repo["pushed_at"],
        f"https://github.com/{owner}/{repo['name']}",
        api,
        today,
    )


async def pypi_fact(ctx: Context, name: str, n: int, today: datetime) -> Fact | None:
    api = PYPI_API.format(name=urlquote(name))
    data = await ctx.fetcher.get_json(api)
    files = data.get("urls") or []
    version = data.get("info", {}).get("version", "")
    if not files or not version:
        return None
    raw = files[0]["upload_time_iso_8601"]
    when = _iso(raw)
    return _record_fact(
        n,
        f"The latest PyPI release of {name} ({version}) was published on {when.date().isoformat()}",
        when,
        raw,
        f"https://pypi.org/project/{name}/",
        api,
        today,
    )


async def npm_fact(ctx: Context, name: str, n: int, today: datetime) -> Fact | None:
    api = NPM_API.format(name=urlquote(name, safe="@"))
    data = await ctx.fetcher.get_json(api)
    version = data.get("dist-tags", {}).get("latest", "")
    raw = data.get("time", {}).get(version, "")
    if not version or not raw:
        return None
    when = _iso(raw)
    return _record_fact(
        n,
        f"The latest npm release of {name} ({version}) was published on {when.date().isoformat()}",
        when,
        raw,
        f"https://www.npmjs.com/package/{name}",
        api,
        today,
    )


async def appstore_fact(
    ctx: Context, app_id: str, url: str, n: int, today: datetime
) -> Fact | None:
    api = ITUNES_API.format(id=app_id)
    data = await ctx.fetcher.get_json(api)
    results = data.get("results") or []
    if not results or not results[0].get("currentVersionReleaseDate"):
        return None
    app = results[0]
    owner = f"{app.get('trackName', '')} {app.get('sellerName', '')}"
    if not _name_matches(owner, ctx):
        return None
    raw = app["currentVersionReleaseDate"]
    when = _iso(raw)
    return _record_fact(
        n,
        f"The App Store lists version {app.get('version', '?')} of "
        f"{app.get('trackName', 'the app')}, released on {when.date().isoformat()}",
        when,
        raw,
        url,
        api,
        today,
    )


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    facts = page_date_facts(ctx.pages, today)
    accounts = linked_accounts(ctx.pages, ctx)
    jobs = []
    n = 10
    if "github" in accounts:
        jobs.append(github_fact(ctx, accounts["github"], n, today))
    if "pypi" in accounts:
        jobs.append(pypi_fact(ctx, accounts["pypi"], n + 1, today))
    if "npm" in accounts:
        jobs.append(npm_fact(ctx, accounts["npm"], n + 2, today))
    if "appstore" in accounts:
        app_url = accounts["appstore_url"]
        jobs.append(appstore_fact(ctx, accounts["appstore"], app_url, n + 3, today))
    for result in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(result, Fact):
            facts.append(result)
        elif isinstance(result, (FetchError, ValueError, KeyError, TypeError)):
            logger.info("alive: a record lookup failed for %s: %s", ctx.domain, result)
        elif isinstance(result, BaseException):
            raise result
    logger.info("alive %s: %d facts (%s)", ctx.domain, len(facts), ", ".join(accounts) or "none")
    return facts
