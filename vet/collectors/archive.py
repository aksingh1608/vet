"""Archive track record: what did the domain show when the Wayback Machine first saw it?

``age`` reads dates only, so an old domain earns age credit whatever used to be on it. This
collector opens the earliest Wayback copy of the homepage that had status 200 and real text,
and looks for the vendor, product or legal-entity name from ``ctx.identity``:

* a name is found: ``same_vendor_since`` (the year in the statement, a quote from the copy);
* real text but none of the names: ``domain_older_than_vendor``. A company may have renamed
  itself, so this is never a negative and never a hard flag;
* a thin, parked or unreadable copy, or any fetch problem: no fact.

Requests: one CDX lookup, then at most ``MAX_SNAPSHOTS`` snapshot fetches through the site
reader's archive helper, all through ``vet/fetch.py`` under the Wayback host limit.

Both facts are NEUTRAL with weight 0 and marked filler: only the buyer view reads them, through
``Fact.topic``. A positive polarity would count as an independent source in the trust floor
even at weight 0, so the two-axis labels could move.
"""

import logging
import re
from datetime import UTC, datetime

from vet.collectors.base import Context, SitePage, fact_id
from vet.fetch import FetchError
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.site_reader import _archived
from vet.text import contains_quote, normalize

logger = logging.getLogger(__name__)

NAME = "archive"
# Earliest homepage captures that were real pages, one per month, oldest first.
CDX_EARLIEST_OK = (
    "https://web.archive.org/cdx/search/cdx?url={domain}/&output=json&fl=timestamp,length"
    "&filter=statuscode:200&filter=mimetype:text/html&collapse=timestamp:6&limit={limit}"
)
CDX_ROWS = 6
MAX_SNAPSHOTS = 2
MIN_CAPTURE_BYTES = 1000  # compressed record size; parked and error pages are smaller
MIN_WORDS = 40
MIN_COMPACT_NAME_CHARS = 6  # "Quill Nest" may match "Quillnest" only for names this long
MIN_NAME_CHARS = 3
QUOTE_CONTEXT_CHARS = 60
MAX_TITLE_CHARS = 120
PARKED = re.compile(
    r"domain (?:name )?(?:is|may be) for sale|buy this domain|this domain is parked"
    r"|parked (?:free|domain)|domain parking|under construction|website coming soon"
    r"|hugedomains|sedo(?:parking)?\b|dan\.com|afternic|godaddy\.com/domain"
    r"|diese domain (?:steht zum verkauf|wurde registriert)|domain kaufen",
    re.I,
)
LEGAL_SUFFIX = re.compile(
    r"\s+(?:GmbH|gGmbH|UG(?: \(haftungsbeschränkt\))?|AG|KG|SE|Ltd\.?|Limited|LLC|LLP|PLC|Inc\.?"
    r"|Corp\.?|S\.A\.S\.?|SAS|SARL|B\.V\.|BV|N\.V\.|ApS|AB|Oy|Pty\.? Ltd\.?)$"
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HOST = re.compile(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}\b(?:/\S*)?", re.I)


def names(ctx: Context) -> list[str]:
    """The names to look for, longest first, without duplicates or very short ones."""
    ident = ctx.identity
    found = [ident.vendor_name, ident.product_name, ident.legal_entity]
    if ident.legal_entity:
        found.append(LEGAL_SUFFIX.sub("", ident.legal_entity))
    out: list[str] = []
    for n in sorted((n.strip() for n in found if n), key=len, reverse=True):
        if len(n) >= MIN_NAME_CHARS and normalize(n) not in {normalize(o) for o in out}:
            out.append(n)
    return out


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", normalize(text))


def name_on_page(text: str, wanted: list[str]) -> str:
    """The first wanted name the text mentions as a word (or spelled without spaces), or "".
    Host names and URLs are removed first: an old site printing the domain itself (or the
    archive's own links) says nothing about who ran it."""
    text = _HOST.sub(" ", text)
    hay = normalize(text)
    compact = _compact(text)
    for n in wanted:
        if re.search(rf"(?<!\w){re.escape(normalize(n))}(?!\w)", hay):
            return n
        if len(_compact(n)) >= MIN_COMPACT_NAME_CHARS and _compact(n) in compact:
            return n
    return ""


def readable(page: SitePage) -> bool:
    """Status 200, enough words, and not a parked or placeholder page."""
    if page.error or not page.raw or page.status != 200:
        return False
    if len(page.text.split()) < MIN_WORDS:
        return False
    return not PARKED.search(page.text)


def name_quote(page: SitePage, name: str) -> str:
    """A short passage of the copy around the name, or the name as written on the copy."""
    m = re.search(re.escape(name), page.text, re.I)
    if m:
        lo = max(0, m.start() - QUOTE_CONTEXT_CHARS)
        hi = min(len(page.text), m.end() + QUOTE_CONTEXT_CHARS)
        window = page.text[lo:hi]
        # Whole words only: drop a cut-off word at either end.
        if lo > 0:
            window = window.split(" ", 1)[-1]
        if hi < len(page.text):
            window = window.rsplit(" ", 1)[0]
        for candidate in (window.strip(), m.group(0)):
            if candidate and contains_quote(page.raw, candidate):
                return candidate
    return name if contains_quote(page.raw, name) else ""


def site_quote(page: SitePage) -> str:
    """Something on the copy that shows what the site was: its title, else its first words."""
    m = _TITLE.search(page.raw)
    title = " ".join(m.group(1).split())[:MAX_TITLE_CHARS] if m else ""
    if title and contains_quote(page.raw, title):
        return title
    words = " ".join(page.text.split()[:10])
    return words if words and contains_quote(page.raw, words) else ""


def capture_day(page: SitePage) -> datetime | None:
    try:
        return datetime.strptime(page.archived, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def fact_for(ctx: Context, page: SitePage, wanted: list[str], today: datetime) -> Fact | None:
    when = capture_day(page)
    if when is None:
        return None
    common = {
        "collector": NAME,
        "axis": Axis.REAL_ALIVE,
        "polarity": Polarity.NEUTRAL,
        "weight": 0.0,
        "source_url": page.final_url,
        "source_kind": SourceKind.RECORD,
        "source_type": "record",
        "fetched_at": today,
        "observed_at": when,
        "is_filler": True,
        "code_worded": True,
    }
    found = name_on_page(page.text, wanted)
    if found:
        quote = name_quote(page, found)
        if not quote:
            return None
        return Fact(
            id=fact_id(NAME, 0),
            statement=f"The earliest readable Wayback copy of {ctx.domain}, from "
            f"{page.archived}, already names {found}: the same vendor has used the domain "
            f"since at least {when.year}",
            quote=quote,
            topic="same_vendor_since",
            **common,
        )
    quote = site_quote(page)
    if not quote:
        return None
    shown = ", ".join(wanted)
    return Fact(
        id=fact_id(NAME, 1),
        statement=f"The earliest readable Wayback copy of {ctx.domain}, from {page.archived}, "
        f"shows a site that does not name {shown}: the domain is older than its use by this "
        f"vendor (a renamed company looks the same)",
        quote=quote,
        topic="domain_older_than_vendor",
        **common,
    )


async def earliest_stamps(ctx: Context) -> list[str]:
    url = CDX_EARLIEST_OK.format(domain=ctx.domain, limit=CDX_ROWS)
    rows = await ctx.fetcher.get_json(url)
    if not isinstance(rows, list):
        return []
    stamps: list[str] = []
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) < 2:
            continue
        ts, length = str(row[0]), str(row[1])
        if length.isdigit() and int(length) < MIN_CAPTURE_BYTES:
            continue  # a parked or error page, not worth one of our snapshot fetches
        stamps.append(ts)
    return stamps[:MAX_SNAPSHOTS]


async def collect(ctx: Context) -> list[Fact]:
    today = datetime.now(UTC)
    wanted = names(ctx)
    if not wanted:
        logger.info("archive: no vendor, product or entity name for %s", ctx.domain)
        return []
    home = f"https://{ctx.domain}/"
    try:
        stamps = await earliest_stamps(ctx)
    except (FetchError, ValueError) as exc:
        logger.info("archive: CDX lookup failed for %s: %s", ctx.domain, exc)
        return []
    for stamp in stamps:
        page = await _archived(ctx, "home", home, False, "track record", at=stamp)
        if page.error:
            logger.info("archive: snapshot %s of %s: %s", stamp, ctx.domain, page.error)
            return []  # a fetch problem is not evidence: stop, no fact
        if not readable(page):
            logger.info("archive: snapshot %s of %s is thin or parked", stamp, ctx.domain)
            continue
        fact = fact_for(ctx, page, wanted, today)
        return [fact] if fact else []
    return []
