"""What kind of publisher a source is, and how review pages are named and read.

Used by the collectors (to tag facts) and by finding selection (one source is enough only for
records, reputable press, regulators and courts, and the vendor's own negative statements).
"""

import re

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
