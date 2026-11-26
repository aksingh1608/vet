"""Small text helpers shared by the site reader, collectors and verifier."""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})

# Second-level public suffixes common for our targets; enough for a hackathon heuristic.
_TWO_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "co.nz",
        "co.jp",
        "com.br",
        "co.in",
        "co.za",
        "com.cn",
        "com.tr",
        "co.at",
        "or.at",
    }
)


def normalize(text: str) -> str:
    """Lower-case, unify quotes/dashes, collapse whitespace."""
    return _WS.sub(" ", html.unescape(text).translate(_QUOTE_CHARS)).strip().lower()


def html_to_text(raw: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", _SCRIPT_STYLE.sub(" ", raw)))).strip()


def contains_quote(haystack: str, quote: str) -> bool:
    """True when the quote appears in the text, ignoring case, whitespace and markup."""
    q = normalize(quote)
    if not q:
        return False
    if q in normalize(haystack):
        return True
    text = normalize(html_to_text(haystack))
    if q in text:
        return True
    # Extraction may join or split words at tag boundaries ("2021</b>founded"): compare
    # without whitespace. Still character-exact for everything else.
    return _WS.sub("", q) in _WS.sub("", text)


def registered_domain(url_or_host: str) -> str:
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


class _LinkParser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
                self._href = urljoin(self.base, href)
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((clean_url(self._href), _WS.sub(" ", "".join(self._text)).strip()))
            self._href = None


def extract_links(raw_html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute url, anchor text) for every <a href>, in document order."""
    parser = _LinkParser(base_url)
    parser.feed(raw_html)
    return parser.links


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"
