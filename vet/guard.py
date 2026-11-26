"""Guard: find text on vendor pages that tries to steer an AI evaluator, and keep it away from
the LLM.

Page text is data, not instructions. The guard looks in every channel a page can use to talk to
a model without a human noticing:

* visible text
* CSS-hidden elements (inline style or a simple <style> rule: display:none, visibility:hidden,
  opacity:0, zero or tiny font size, off-screen positioning, text the same colour as its
  background) and elements with the ``hidden`` attribute
* HTML comments
* ``alt`` and ``title`` attributes
* ``<meta>`` tags

A pattern hit in any channel becomes a hard-flag NEGATIVE trust fact quoting the snippet
(collector ``guard``). ``sanitize_html`` removes hidden elements, comments and attribute values
with a hit before main-text extraction, and ``scrub_text`` removes every paragraph (line) with a
hit from any text bound for the LLM. Whole paragraphs go, not just the matched words: an
instruction split over several sentences must not survive in pieces.
Hidden text without a hit is normal (menus, cookie banners) and is only removed, never flagged.
Ordinary mentions of AI products or of llms.txt files do not match.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from html.parser import HTMLParser

from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.text import contains_quote, truncate

NAME = "guard"
SNIPPET_CONTEXT_CHARS = 80
REMOVED = "[removed: text addressed to AI systems]"
LONG_LINE_CHARS = 600  # longer "lines" are collapsed pages: scrub those sentence by sentence
SCANNED_ATTRIBUTES = ("alt", "title", "aria-label")
TINY_FONT_PX = 2.0
OFFSCREEN_PX = 1000

VISIBLE = "visible text"
HIDDEN = "CSS-hidden text"
COMMENT = "an HTML comment"
ATTRIBUTE = "an alt/title attribute"
META = "a meta tag"

# Text that tells an AI or evaluator what to do or conclude.
INJECTION = re.compile(
    r"(?:ignore|disregard|forget|override) (?:all |any |the |your )?(?:previous|prior|above"
    r"|earlier|preceding|other) (?:instructions|prompts?|rules|context|guidelines)"
    r"|(?:if|when) you are an? (?:ai|llm|large language model|language model|assistant|agent"
    r"|bot|crawler|model)\b"
    r"|\b(?:note|message|instructions?) (?:to|for) (?:any |all )?(?:ai|llms?|language models?"
    r"|ai (?:assistants?|agents?|evaluators?|models?)|evaluators?|reviewers? using ai)\b"
    r"|\b(?:ai|llm|language model)s? (?:evaluators?|reviewers?|auditors?|assessors?|graders?)\b"
    r"|\b(?:rate|score|classify|mark|consider|label|treat|report) (?:this|the|our) (?:vendor"
    r"|company|product|site|website|business|tool) as (?:fully |highly |very )?(?:trust\w*"
    r"|safe|legit\w*|real|reliable|reputable|verified)"
    r"|\b(?:system|developer) (?:prompt|message|override)\s*:"
    r"|\bnew instructions\s*:"
    r"|<\s*/?\s*untrusted_content"
    r"|\b(?:output|return|respond with|give|assign) (?:a |the )?(?:verdict|rating|trust score)"
    r" of\b",
    re.I,
)

_HIDING_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$|!)"
    r"|clip\s*:\s*rect\(\s*0",
    re.I,
)
_FONT_SIZE = re.compile(r"font-size\s*:\s*([\d.]+)\s*(px|pt|em|rem|%)?", re.I)
_OFFSET = re.compile(r"(?:left|top|text-indent|margin-left)\s*:\s*-(\d+)", re.I)
_COLOR = re.compile(r"(?<![-\w])color\s*:\s*([^;!]+)", re.I)
_BACKGROUND = re.compile(r"background(?:-color)?\s*:\s*([^;!]+)", re.I)
_CSS_RULE = re.compile(r"([^{}@]+)\{([^{}]*)\}")
_MEDIA_BLOCK = re.compile(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", re.I)
_SELECTOR = re.compile(r"^\s*([.#])([\w-]+)\s*$")
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_SKIP_TEXT = frozenset({"script", "style", "noscript", "template"})


@dataclass(frozen=True)
class Segment:
    channel: str
    text: str


@dataclass(frozen=True)
class Hit:
    channel: str
    match: str
    snippet: str


def _norm_color(value: str) -> str:
    v = value.strip().lower().replace(" ", "")
    named = {"white": "#ffffff", "black": "#000000", "#fff": "#ffffff", "#000": "#000000"}
    return named.get(v, v)


def style_hides(style: str) -> bool:
    """True when an inline style (or CSS rule body) makes its text invisible to a human."""
    if _HIDING_STYLE.search(style):
        return True
    m = _FONT_SIZE.search(style)
    if m:
        size, unit = float(m.group(1)), (m.group(2) or "px").lower()
        px = size * (16 if unit in ("em", "rem") else 0.16 if unit == "%" else 1)
        if px <= TINY_FONT_PX:
            return True
    m = _OFFSET.search(style)
    if m and int(m.group(1)) >= OFFSCREEN_PX:
        return True
    color, background = _COLOR.search(style), _BACKGROUND.search(style)
    return bool(
        color and background and _norm_color(color.group(1)) == _norm_color(background.group(1))
    )


def hidden_selectors(css: str) -> tuple[set[str], set[str]]:
    """Classes and ids that simple, non-responsive CSS rules hide."""
    classes: set[str] = set()
    ids: set[str] = set()
    for selectors, body in _CSS_RULE.findall(_MEDIA_BLOCK.sub(" ", css)):
        if not style_hides(body):
            continue
        for sel in selectors.split(","):
            m = _SELECTOR.match(sel)
            if m:
                (classes if m.group(1) == "." else ids).add(m.group(2))
    return classes, ids


class _Scanner(HTMLParser):
    """Splits a page into channels, and rebuilds it without hidden elements and comments."""

    def __init__(self, hidden_classes: set[str], hidden_ids: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_classes = hidden_classes
        self.hidden_ids = hidden_ids
        self.segments: list[Segment] = []
        self.visible: list[str] = []
        self.hidden: list[str] = []
        self.clean: list[str] = []
        self._stack: list[tuple[str, bool, bool, bool]] = []  # (tag, hides, skips, emitted)

    @property
    def _in_hidden(self) -> bool:
        return any(e[1] for e in self._stack)

    @property
    def _in_skip(self) -> bool:
        return any(e[2] for e in self._stack)

    def _hides(self, attrs: dict[str, str]) -> bool:
        if "hidden" in attrs:
            return True
        if style_hides(attrs.get("style", "")):
            return True
        classes = attrs.get("class", "").split()
        # Responsive utility classes (md:block) show the element on some screens: not hidden.
        if any(":" in c for c in classes):
            return False
        return bool(self.hidden_classes.intersection(classes)) or (
            attrs.get("id", "") in self.hidden_ids
        )

    def _record_attributes(self, tag: str, attrs: dict[str, str]) -> None:
        for name in SCANNED_ATTRIBUTES:
            if attrs.get(name, "").strip():
                self.segments.append(Segment(ATTRIBUTE, attrs[name].strip()))
        if tag == "meta" and attrs.get("content", "").strip():
            self.segments.append(Segment(META, attrs["content"].strip()))

    def _tag_text(self, tag: str, attrs_list: list[tuple[str, str | None]], close: str) -> str:
        """The tag as written, or rebuilt without attribute values that contain a hit."""
        bad = {
            k
            for k, v in attrs_list
            if v and k.lower() in (*SCANNED_ATTRIBUTES, "content") and INJECTION.search(v)
        }
        if not bad:
            return self.get_starttag_text() or ""
        kept = "".join(
            f' {k}="{escape(v or "")}"' if v is not None else f" {k}"
            for k, v in attrs_list
            if k not in bad
        )
        return f"<{tag}{kept}{close}>"

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        self._record_attributes(tag, attrs)
        hides = self._hides(attrs)
        emitted = not (self._in_hidden or hides)
        if emitted:
            self.clean.append(self._tag_text(tag, attrs_list, ""))
        if tag not in _VOID:
            self._stack.append((tag, hides, tag in _SKIP_TEXT, emitted))

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        self._record_attributes(tag, attrs)
        if not (self._in_hidden or self._hides(attrs)):
            self.clean.append(self._tag_text(tag, attrs_list, " /"))

    def handle_endtag(self, tag: str) -> None:
        if not any(e[0] == tag for e in self._stack):
            return  # stray end tag
        while self._stack:
            t, _, _, emitted = self._stack.pop()
            if t == tag:
                if emitted:
                    self.clean.append(f"</{tag}>")
                break

    def _text(self, data: str) -> None:
        if self._in_hidden:
            if not self._in_skip:
                self.hidden.append(data)
            return
        if self._in_skip:
            self.clean.append(data)  # script/style bodies stay as they are
            return
        self.clean.append(escape(data, quote=False))  # data arrives unescaped
        self.visible.append(data)

    def handle_data(self, data: str) -> None:
        self._text(data)

    def handle_comment(self, data: str) -> None:
        if data.strip():
            self.segments.append(Segment(COMMENT, data.strip()))

    def handle_decl(self, decl: str) -> None:
        self.clean.append(f"<!{decl}>")


def _join(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _scan(raw_html: str) -> _Scanner:
    css = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", raw_html, re.I | re.S))
    classes, ids = hidden_selectors(css)
    scanner = _Scanner(classes, ids)
    scanner.feed(raw_html)
    scanner.close()
    return scanner


def segments(raw_html: str) -> list[Segment]:
    """Every text channel of a page, visible text first."""
    s = _scan(raw_html)
    out = [Segment(VISIBLE, _join(s.visible))]
    if s.hidden:
        out.append(Segment(HIDDEN, _join(s.hidden)))
    return out + s.segments


def _snippet(text: str, match: re.Match[str]) -> str:
    lo = max(0, match.start() - SNIPPET_CONTEXT_CHARS)
    hi = min(len(text), match.end() + SNIPPET_CONTEXT_CHARS)
    return text[lo:hi].strip()


def find_hits(raw_html: str) -> list[Hit]:
    """Injection hits on a page, at most one per channel."""
    hits: list[Hit] = []
    seen: set[str] = set()
    for seg in segments(raw_html):
        if seg.channel in seen:
            continue
        m = INJECTION.search(seg.text)
        if m:
            seen.add(seg.channel)
            hits.append(Hit(channel=seg.channel, match=m.group(0), snippet=_snippet(seg.text, m)))
    return hits


def _scrub_sentences(line: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", line)
    return " ".join(REMOVED if INJECTION.search(p) else p for p in parts)


def scrub_text(text: str) -> str:
    """Remove every paragraph (line) with a hit from text bound for the LLM. Very long lines
    (a page collapsed to one line) are scrubbed sentence by sentence instead."""
    if not INJECTION.search(text):
        return text
    out: list[str] = []
    for line in text.split("\n"):
        if not INJECTION.search(line):
            out.append(line)
        elif len(line) > LONG_LINE_CHARS:
            out.append(_scrub_sentences(line))
        else:
            out.append(REMOVED)
    return "\n".join(out)


def sanitize_html(raw_html: str) -> str:
    """The page without hidden elements, comments, and attribute values that address AI
    systems. Visible text with a hit is left for ``scrub_text`` (paragraph by paragraph)."""
    return "".join(_scan(raw_html).clean)


def hit_facts(
    page_url: str, raw_html: str, today: datetime, start: int = 0
) -> tuple[list[Fact], list[Hit]]:
    """Hard-flag facts for every hit on one vendor page. The quote is checked against the raw
    page, so the verifier can find it again; if the context does not survive markup, the
    matched words alone are quoted."""
    hits = find_hits(raw_html)
    facts: list[Fact] = []
    for hit in hits:
        quote = hit.snippet if contains_quote(raw_html, hit.snippet) else hit.match
        facts.append(
            Fact(
                id=f"{NAME}-{start + len(facts)}",
                collector=NAME,
                statement=f"A page on the vendor's site contains text addressed to AI systems, "
                f'in {hit.channel}: "{truncate(hit.match, 80)}"',
                axis=Axis.TRUST,
                polarity=Polarity.NEGATIVE,
                weight=0.9,
                source_url=page_url,
                quote=quote,
                source_kind=SourceKind.VENDOR,
                fetched_at=today,
                is_hard_flag=True,
                topic="prompt_injection",
            )
        )
    return facts, hits
