"""What kind of publisher a source is, and how review pages are named and read.

Used by the collectors (to tag facts) and by finding selection (one source is enough only for
records, reputable press, regulators and courts, and the vendor's own negative statements).
"""

import re

from vet.schemas import Fact, SourceKind
from vet.text import registered_domain

# Publishers whose single report may stand as a finding.
REPUTABLE_TYPES = frozenset({"record", "press", "regulator", "court"})
# Record publishers: whatever a classifier calls them, they are records.
RECORD_DOMAINS = frozenset(
    {
        "haveibeenpwned.com",
        "northdata.com",
        "northdata.de",
        "service.gov.uk",  # Companies House (find-and-update.company-information.service.gov.uk)
    }
)
# Search tools and search APIs. Their own pages (e.g. Tavily's generated company profiles) are not
# sources: no reason, finding or claim record may link to them.
SEARCH_TOOL_DOMAINS = frozenset({"tavily.com", "serpapi.com", "serper.dev"})


def is_search_tool_url(url: str) -> bool:
    host = re.sub(r"^[a-z]+://", "", url.lower()).split("/", 1)[0].split(":", 1)[0]
    return bool(host) and registered_domain(host) in SEARCH_TOOL_DOMAINS


# Publishers that always need a second, independent source.
SOCIAL_DOMAINS = frozenset(
    {
        "linkedin.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "reddit.com",
        "youtube.com",
        "threads.net",
        "medium.com",
        "substack.com",
        "quora.com",
        "ycombinator.com",
        "producthunt.com",
    }
)
SOURCE_TYPES = frozenset(
    {
        "record",
        "press",
        "regulator",
        "court",
        "vendor",
        "review",
        "social",
        "forum",
        "blog",
        "directory",
        "other",
    }
)
REVIEW_SITE_NAMES: dict[str, str] = {
    "trustpilot.com": "Trustpilot",
    "g2.com": "G2",
    "capterra.com": "Capterra",
    "capterra.de": "Capterra",
    "capterra.co.uk": "Capterra",
    "softwareadvice.com": "Software Advice",
    "gartner.com": "Gartner Peer Insights",
    "omr.com": "OMR Reviews",
    "getapp.com": "GetApp",
    "trustradius.com": "TrustRadius",
    "apple.com": "App Store",
}

_COUNT = re.compile(
    r"(\d{1,3}(?:[,.]\d{3})+|\d+)\s+(?:verified\s+|user\s+)?(reviews?|ratings|bewertungen)\b",
    re.I,
)
_SCORE = re.compile(r"\b(\d(?:[.,]\d{1,2})?)\s*(?:/|out of|von)\s*(5|10)\b", re.I)
_SCORE_AFTER = re.compile(
    r"\b(?:rated|rating|score|trustscore)\s*(?:of|is|:)?\s*(\d(?:[.,]\d{1,2})?)\b", re.I
)
_SCORE_BEFORE = re.compile(r"\b(\d[.,]\d{1,2})\s*(?:stars?|rating)\b", re.I)
_WORD = re.compile(r"\b(excellent|great|good|average|poor|bad)\b(?!\s+rating)", re.I)


def source_type_for(url: str, declared: str = "") -> str:
    """The declared type, unless the domain is a known social site or record publisher."""
    domain = registered_domain(url)
    if domain in SOCIAL_DOMAINS:
        return "social"
    if domain in RECORD_DOMAINS or domain.endswith(".service.gov.uk"):
        return "record"
    declared = declared.lower().strip()
    return declared if declared in SOURCE_TYPES else "other"


def site_name(url: str) -> str:
    domain = registered_domain(url)
    return REVIEW_SITE_NAMES.get(domain, domain)


def review_statement(url: str, quote: str) -> str:
    """A data-only sentence from a review quote ("Trustpilot: 5,420 reviews, overall rating
    'Great'"), or "" when the quote holds no count and no numeric rating."""
    count = _COUNT.search(quote)
    score = _SCORE.search(quote)
    score_after = None if score else (_SCORE_AFTER.search(quote) or _SCORE_BEFORE.search(quote))
    if not (count or score or score_after):
        return ""
    parts: list[str] = []
    if count:
        noun = "ratings" if count.group(2).lower() == "ratings" else "reviews"
        parts.append(f"{count.group(1)} {noun}")
    if score:
        parts.append(f"average rating {score.group(1)}/{score.group(2)}")
    elif score_after:
        parts.append(f"rating {score_after.group(1)}")
    word = None if (score or score_after) else _WORD.search(quote)
    if word:
        parts.append(f"overall rating '{word.group(1).capitalize()}'")
    return f"{site_name(url)}: " + ", ".join(parts)


# ---- evidence quality ------------------------------------------------------------------------

# Launch sites and "submit your tool" lists: anyone can be listed in an afternoon.
LAUNCH_SITES = frozenset(
    {
        "producthunt.com",
        "betalist.com",
        "launchingnext.com",
        "saashub.com",
        "alternativeto.net",
        "theresanaiforthat.com",
        "futuretools.io",
        "huntscreens.com",
        "productcool.com",
        "microlaunch.net",
        "uneed.best",
        "peerlist.io",
        "toolify.ai",
        "indiehackers.com",
    }
)
# Paid press-release distribution: the vendor writes it.
PR_WIRES = frozenset(
    {
        "prnewswire.com",
        "businesswire.com",
        "globenewswire.com",
        "einpresswire.com",
        "openpr.com",
        "prweb.com",
        "accessnewswire.com",
        "newswire.com",
        "prlog.org",
    }
)
MIN_REAL_REVIEWS = 10


def review_count(quote: str) -> int:
    m = _COUNT.search(quote)
    return int(re.sub(r"[,.]", "", m.group(1))) if m else 0


def evidence_class(fact: Fact) -> str:
    """strong: records, independent press/regulators/courts, customers naming the vendor,
    established review platforms with a real review count, open-source adoption records.
    weak: launch sites, link directories, paid PR wires, social media (including the vendor's
    own accounts), activity the vendor controls (its own pages, its GitHub/package releases).
    normal: everything else."""
    domain = registered_domain(fact.source_url)
    if fact.topic == "open_source" and fact.source_kind == SourceKind.RECORD:
        return "strong"  # stars and contributors from others (thresholds in collectors/alive)
    if (
        fact.source_kind == SourceKind.VENDOR
        or fact.collector == "alive"
        or domain in LAUNCH_SITES
        or domain in PR_WIRES
        or fact.topic == "directory"
        or fact.is_filler
        or fact.source_type in ("social", "directory")
    ):
        return "weak"
    if fact.source_kind == SourceKind.RECORD or fact.source_type in REPUTABLE_TYPES:
        return "strong"
    if fact.topic == "customer":
        return "strong"
    if domain in REVIEW_SITE_NAMES and review_count(fact.quote) >= MIN_REAL_REVIEWS:
        return "strong"
    return "normal"


# ---- entity matching for negatives -----------------------------------------------------------


def names_entity(text: str, domain: str, names: set[str], *, own_site: bool = False) -> bool:
    """True when the text names the exact product or domain being judged. A sister domain
    with the same label (cal.eu when judging cal.com) does not count, and neither does a
    different company (a parent, a subsidiary, a namesake). On the vendor's own site a page
    speaks about the vendor without naming it, so only the sister-brand check applies."""
    low = text.lower()
    label = domain.split(".")[0].lower()
    sister = re.search(
        rf"(?<![\w.]){re.escape(label)}\.(?!{re.escape(domain.split('.', 1)[1])}\b)[a-z]{{2,}}\b",
        low,
    )
    if sister:
        return False
    if own_site or domain.lower() in low:
        return True
    for name in names:
        n = name.strip().lower()
        if len(n) < 3:
            continue
        if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w]|\.[a-z])", low):
            return True
    return False
