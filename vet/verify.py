"""Verify facts, then choose the reasons and findings that ship.

Verification, for each candidate fact:
1. Source check. Quoted facts: the quote must appear on the source page (from cache, else
   fetched politely), or in the machine record named by ``check_url`` (RDAP JSON behind an
   ICANN lookup link, crt.sh JSON, HIBP API). Snippet-only domains (North Data, review sites,
   LinkedIn): the quote must appear in the cached Tavily snippet for that URL. Computed facts
   (no quote): the record must resolve.
2. Support check (fast model, one batch): does the quote support the sentence without
   overclaiming? Facts whose sentence is written by our code from a record or a pattern match
   skip it.
3. Facts built on other facts (claims_check) pass only if those facts pass too.

Selection (``select_reasons``, deterministic):
* Contradicted claims and founding-year gaps always come first.
* At most ONE age reason: RDAP, crt.sh and Wayback merged into one sentence, linked to the
  strongest record; the others are listed in the selection notes and the trace.
* No two reasons from the same collector, unless fewer than 4 collectors have verified facts.
* At least one real_alive and one trust reason when a verified fact exists for each.
* Filler (copyright year, directory listings, site looks) only when nothing better exists.

* Trust axis coverage always holds. With no verified trust fact, the trust reason is the first
  of: an old, disclosed breach kept as a finding (as the record states it, plus the disclosure
  when a disclosure source verified); a company-register record; an established review
  platform with a real review count; an independent press mention; and last a sentence built
  only from the domain record ("Domain registered <date>, under <n> years of history"), linked
  to it. It never states source counts or an absence. The verdict does not change.

Findings: verified, notable facts that did not make the reasons (neither the fact nor the same
event), ranked by strength: contradictions first, then hard flags, breaches and other real
negatives, then strong facts by weight. At most MAX_FINDINGS. One source is enough for a
record, reputable press, a regulator or a court, the vendor's own statement of a negative fact,
or our own checks (guard, claims_check); everything else (social media, forums, blogs,
anonymous sources) needs a second independent site on the same topic.
"""

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field

from pydantic import BaseModel

from vet import trace
from vet.config import API_HOSTS, Settings
from vet.fetch import Fetcher, FetchError, FetchResult, FetchStatusError, host_matches, host_of
from vet.judge import (
    AGE_EVIDENCE_TOPICS,
    history_years,
    is_breach,
    is_forced,
    is_real_negative,
)
from vet.llm import LLMError, StructuredLLM, load_prompt, untrusted
from vet.schemas import (
    MAX_REASONS,
    MIN_REASONS,
    Axis,
    Fact,
    Finding,
    Polarity,
    Reason,
    SourceKind,
)
from vet.sources import MIN_REAL_REVIEWS, REPUTABLE_TYPES, REVIEW_SITE_NAMES, review_count
from vet.text import contains_quote, registered_domain

logger = logging.getLogger(__name__)

FIRST_POOL = 24
MAX_FINDINGS = 3
NOTABLE_WEIGHT = 0.5  # a positive or neutral fact this strong is notable on its own
# Sentences written by our code from a record or a pattern match: no model support check.
CODE_WORDED_COLLECTORS = frozenset(
    {"age", "guard", "claims_check", "alive", "looks", "site_reader"}
)
AGE = "age"
# Order in which age records are preferred as the one link of the merged age reason.
AGE_LINK_PRIORITY = ("domain_registered", "first_capture", "capture_history", "first_certificate")
# Facts whose claim IS that the page is gone: verified by the 404/410.
EXPECTS_MISSING_TOPICS = frozenset({"missing_page", "dead_help"})
MISSING_STATUSES = frozenset({404, 410})
# Pages that refuse us (now or when first fetched): the cached Tavily snippet may stand in.
REFUSED_STATUSES = frozenset({403, 429})
SNIPPET_SOURCED = "snippet-sourced"
# Our own checks: their findings stand on the page or record they quote.
SELF_EVIDENT_COLLECTORS = frozenset({"guard", "claims_check", "site_reader"})


class VerifyResult(BaseModel):
    fact_id: str
    source_ok: bool
    supported: bool | None = None
    note: str = ""
    snippet_sourced: bool = False  # the page refused us (403/429); the quote is in the snippet

    @property
    def passed(self) -> bool:
        return self.source_ok and self.supported is not False


class _Support(BaseModel):
    id: str
    supported: bool


class _SupportOut(BaseModel):
    items: list[_Support]


def reason_from(fact: Fact) -> Reason:
    return Reason(
        text=fact.statement,
        axis=fact.axis,
        polarity=fact.polarity,
        source_url=fact.source_url,
        quote=fact.quote,
        fact_id=fact.id,
    )


def _target(fact: Fact) -> str:
    return fact.check_url or fact.source_url


async def _load(fetcher: Fetcher, fact: Fact) -> FetchResult | None:
    url = _target(fact)
    cached = await fetcher.cached(url)
    if cached is not None:
        return cached
    if host_matches(host_of(url), API_HOSTS):  # records, and Wayback copies of blocked pages
        return await fetcher.fetch_api(url)
    return await fetcher.get_text(url)


def snippet_check(fact: Fact, snippets: dict[str, str], status: int) -> VerifyResult:
    """The page answered 403/429: accept the quote if the cached Tavily snippet holds it."""
    ok = bool(fact.quote) and contains_quote(snippets.get(fact.source_url, ""), fact.quote)
    return VerifyResult(
        fact_id=fact.id,
        source_ok=ok,
        snippet_sourced=ok,
        note=f"{SNIPPET_SOURCED}: page answered {status}, quote found in cached Tavily snippet"
        if ok
        else f"page answered {status}; quote not in Tavily snippet",
    )


async def check_source(
    fact: Fact, fetcher: Fetcher, snippets: dict[str, str], settings: Settings
) -> VerifyResult:
    host = host_of(fact.source_url)
    if not fact.check_url and host_matches(host, settings.blocked_domains):
        if not fact.quote:  # computed from the result URL: Tavily must have returned it
            ok = fact.source_url in snippets
            return VerifyResult(
                fact_id=fact.id,
                source_ok=ok,
                note="URL returned by Tavily" if ok else "URL not among Tavily results",
            )
        snippet = snippets.get(fact.source_url, "")
        ok = contains_quote(snippet, fact.quote)
        return VerifyResult(
            fact_id=fact.id,
            source_ok=ok,
            note="quote found in cached Tavily snippet" if ok else "quote not in Tavily snippet",
        )
    expects_missing = fact.topic in EXPECTS_MISSING_TOPICS
    try:
        result = await _load(fetcher, fact)
    except FetchStatusError as exc:
        if expects_missing and exc.status in MISSING_STATUSES:
            return VerifyResult(fact_id=fact.id, source_ok=True, note=f"page still {exc.status}")
        if exc.status in REFUSED_STATUSES and not fact.check_url:
            return snippet_check(fact, snippets, exc.status)
        return VerifyResult(fact_id=fact.id, source_ok=False, note=f"source unreachable: {exc}")
    except FetchError as exc:
        return VerifyResult(fact_id=fact.id, source_ok=False, note=f"source unreachable: {exc}")
    if expects_missing:
        status = result.status if result else 0
        ok = status in MISSING_STATUSES
        return VerifyResult(
            fact_id=fact.id,
            source_ok=ok,
            note=f"page still {status}" if ok else f"page now answers {status}",
        )
    if result is not None and result.status in REFUSED_STATUSES and not fact.check_url:
        return snippet_check(fact, snippets, result.status)  # a stored 403 from an earlier run
    if result is None or not result.ok:
        status = result.status if result else "none"
        return VerifyResult(fact_id=fact.id, source_ok=False, note=f"source status {status}")
    where = "record" if fact.check_url else "source page"
    if not fact.quote:
        return VerifyResult(
            fact_id=fact.id, source_ok=True, supported=None, note=f"computed fact; {where} resolves"
        )
    if contains_quote(result.text, fact.quote):
        return VerifyResult(fact_id=fact.id, source_ok=True, note=f"quote found in {where}")
    snippet = snippets.get(fact.source_url, "")
    if snippet and contains_quote(snippet, fact.quote):
        # The page changed or renders client-side; the quote is only in the search snippet.
        return VerifyResult(
            fact_id=fact.id, source_ok=False, note="quote only in search snippet, not on the page"
        )
    return VerifyResult(fact_id=fact.id, source_ok=False, note=f"quote not found in {where}")


def needs_support_check(fact: Fact) -> bool:
    """Human-written quotes behind a model-written sentence get the model check. Sentences our
    code writes from a record value or a pattern match do not."""
    if not fact.quote or fact.code_worded or fact.collector in CODE_WORDED_COLLECTORS:
        return False
    is_api_record = fact.source_kind == SourceKind.RECORD and host_matches(
        host_of(_target(fact)), API_HOSTS
    )
    return not is_api_record


async def check_support(llm: StructuredLLM, facts: list[Fact]) -> dict[str, bool]:
    items = [{"id": f.id, "sentence": f.statement, "quote": f.quote} for f in facts if f.quote]
    if not items:
        return {}
    out = await llm.structured(
        kind="fast",
        system=load_prompt("verify_reason.md"),
        user=untrusted(json.dumps(items, ensure_ascii=False)),
        output=_SupportOut,
        step="verify_support",
    )
    return {i.id: i.supported for i in out.items}


async def verify_facts(
    facts: list[Fact],
    fetcher: Fetcher,
    llm: StructuredLLM,
    snippets: dict[str, str],
    settings: Settings,
) -> dict[str, VerifyResult]:
    checks = await asyncio.gather(*(check_source(f, fetcher, snippets, settings) for f in facts))
    results = {r.fact_id: r for r in checks}
    to_judge = [f for f in facts if results[f.id].source_ok and needs_support_check(f)]
    try:
        support = await check_support(llm, to_judge)
    except LLMError as exc:
        logger.warning("support check failed (%s); keeping source-verified reasons", exc)
        support = {}
    for f in to_judge:
        verdict = support.get(f.id)
        results[f.id].supported = verdict
        if verdict is False:
            results[f.id].note += "; quote does not support the sentence"
    return results


# Facts whose sentence the model wrote from a quote. If the sentence overclaims but the quote
# is verified, the reason falls back to the quote itself, attributed to its site.
QUOTE_FALLBACK_COLLECTORS = frozenset({"footprint", "risk", "legal_id"})


def quoted_statement(fact: Fact) -> str:
    return f'{registered_domain(fact.source_url)}: "{fact.quote.strip()}"'


def fall_back_to_quotes(
    facts: dict[str, Fact], results: dict[str, VerifyResult]
) -> dict[str, Fact]:
    """Rewrite facts whose quote is on the page but whose sentence overclaims; mark them
    passed. Returns the rewritten facts by id."""
    rewritten: dict[str, Fact] = {}
    for fid, result in results.items():
        fact = facts.get(fid)
        if (
            fact is not None
            and result.source_ok
            and result.supported is False
            and fact.quote
            and fact.collector in QUOTE_FALLBACK_COLLECTORS
            and not fact.is_hard_flag  # a hard flag must stand on its own sentence
        ):
            rewritten[fid] = fact.model_copy(
                update={"statement": quoted_statement(fact), "code_worded": True}
            )
            result.supported = None
            result.note += "; sentence replaced by the verbatim quote"
    return rewritten


def apply_dependencies(facts: dict[str, Fact], results: dict[str, VerifyResult]) -> None:
    """A fact built on other facts passes only when every one of them passed."""
    for fid, result in results.items():
        fact = facts.get(fid)
        if fact is None or not fact.related_fact_ids or not result.passed:
            continue
        failed = [r for r in fact.related_fact_ids if not (r in results and results[r].passed)]
        if failed:
            result.source_ok = False
            result.note += f"; underlying record(s) not verified: {', '.join(failed)}"


# ---- selection ----------------------------------------------------------------------------


@dataclass
class Selection:
    reasons: list[Reason]
    findings: list[Finding]
    notes: list[str] = field(default_factory=list)


def _age_part(fact: Fact) -> str:
    when = fact.observed_at.date().isoformat() if fact.observed_at else ""
    if not when and fact.topic in ("domain_registered", "first_capture", "first_certificate"):
        return fact.statement  # no structured date: keep the record's own sentence
    if fact.topic == "domain_registered":
        return f"domain registered {when}"
    if fact.topic == "first_capture":
        return f"first archived {when}"
    if fact.topic == "first_certificate":
        return f"first certificate {when}"
    if fact.topic == "no_captures":
        return "no Wayback Machine captures"
    if fact.topic == "capture_history":
        # "...captures of x in 31 of the 31 years from 1996 to 2026; ..." -> keep the counts.
        detail = fact.statement.split(" in ", 1)[-1].split(";")[0]
        return f"archived in {detail}"
    return fact.statement


def merge_age(age_facts: list[Fact], domain: str = "") -> tuple[Reason, list[Fact]]:
    """One reason from all verified age facts, linked to the strongest record."""
    by_topic = {f.topic: f for f in age_facts}
    link = next((by_topic[t] for t in AGE_LINK_PRIORITY if t in by_topic), age_facts[0])
    order = ["domain_registered", "capture_history", "first_capture", "no_captures",
             "first_certificate"]  # fmt: skip
    if "capture_history" in by_topic:
        order.remove("first_capture")  # the history already gives the first year
    parts = [_age_part(by_topic[t]) for t in order if t in by_topic]
    strongest = max(age_facts, key=lambda f: f.weight)
    text = "; ".join(parts)
    text = text[0].upper() + text[1:]
    reason = Reason(
        text=f"{text} ({domain})" if domain else text,
        axis=Axis.REAL_ALIVE,
        polarity=strongest.polarity,
        source_url=link.source_url,
        quote=link.quote,
        fact_id=link.id,
    )
    return reason, [f for f in age_facts if f.id != link.id]


@dataclass
class _Pick:
    reason: Reason
    collector: str
    forced: bool


def _trust_fallback(picks: list[_Pick], chain: list[Reason]) -> tuple[Reason | None, bool]:
    """The first usable trust fallback, and whether it was applied in place: a fact that is
    already a real_alive reason moves to the trust axis when real_alive keeps another reason."""
    for r in chain:
        if r.fact_id.startswith(f"{AGE}-"):  # the domain-record sentence: always usable
            return r, False
        same = [i for i, p in enumerate(picks) if p.reason.fact_id == r.fact_id]
        if not same:
            return r, False
        i = same[0]
        axis = picks[i].reason.axis
        if sum(1 for p in picks if p.reason.axis == axis) > 1:
            picks[i] = _Pick(r, picks[i].collector, picks[i].forced)
            return r, True
    return None, False


def select_reasons(
    ranked: list[Fact],
    passed: set[str],
    domain: str = "",
    finding_only: frozenset[str] = frozenset(),
    trust_fallbacks: list[Reason] | None = None,
) -> tuple[list[Reason], list[str]]:
    """Pick 4-7 reasons from verified facts in rank order. Pure and deterministic. Facts in
    ``finding_only`` (old, disclosed breaches at a trusted vendor) are never reasons."""
    notes: list[str] = []
    verified = [f for f in ranked if f.id in passed and f.id not in finding_only]
    age_facts = [f for f in verified if f.collector == AGE]

    # Candidates in rank order; the merged age reason sits where its best fact ranked.
    candidates: list[tuple[Reason, str, Fact]] = []
    age_done = False
    for f in verified:
        if f.collector == AGE:
            if not age_done:
                reason, rest = merge_age(age_facts, domain)
                candidates.append((reason, AGE, f))
                age_done = True
                if rest:
                    notes.append(
                        "age reason merged; other records: "
                        + ", ".join(f"{r.id} {r.source_url}" for r in rest)
                    )
            continue
        candidates.append((reason_from(f), f.collector, f))

    collectors = {c for _, c, _ in candidates}
    relaxed = len(collectors) < MIN_REASONS
    if relaxed:
        notes.append(f"only {len(collectors)} collectors verified: diversity relaxed")

    picks: list[_Pick] = []
    events: set[tuple[str, int]] = set()

    def used(collector: str) -> bool:
        return any(p.collector == collector for p in picks)

    def repeats_event(fact: Fact) -> bool:
        """A second report of an event already among the reasons (HIBP record + HIBP page)."""
        event = _event(fact) if fact.collector != AGE else None
        return event is not None and event in events

    def add(reason: Reason, collector: str, fact: Fact) -> None:
        picks.append(_Pick(reason, collector, is_forced(fact)))
        event = _event(fact) if fact.collector != AGE else None
        if event is not None:
            events.add(event)

    # 1. Contradicted claims / founding-year gaps: always, regardless of diversity.
    for reason, collector, fact in candidates:
        if is_forced(fact) and len(picks) < MAX_REASONS:
            add(reason, collector, fact)
    # 2. One per collector, non-filler, rank order.
    for reason, collector, fact in candidates:
        if len(picks) >= MAX_REASONS:
            break
        if (
            not is_forced(fact)
            and not fact.is_filler
            and not used(collector)
            and not repeats_event(fact)
        ):
            add(reason, collector, fact)
    # 3. Too few collectors: repeats allowed up to the minimum (still non-filler).
    if relaxed:
        for reason, collector, fact in candidates:
            if len(picks) >= MIN_REASONS:
                break
            if (
                not fact.is_filler
                and reason not in [p.reason for p in picks]
                and not repeats_event(fact)
            ):
                add(reason, collector, fact)
    # 4. Filler only when nothing better exists.
    for reason, collector, fact in candidates:
        if len(picks) >= MIN_REASONS:
            break
        if fact.is_filler and reason not in [p.reason for p in picks]:
            add(reason, collector, fact)
            notes.append(f"filler used: {fact.id}")

    # 5. Axis coverage: at least one reason per axis when a verified fact exists for it.
    for axis in (Axis.REAL_ALIVE, Axis.TRUST):
        if any(p.reason.axis == axis for p in picks):
            continue
        spare = next(
            (
                (r, c, f)
                for r, c, f in sorted(candidates, key=lambda x: x[2].is_filler)
                if r.axis == axis
            ),
            None,
        )
        if spare is None and axis == Axis.TRUST:
            fallback, moved = _trust_fallback(picks, trust_fallbacks or [])
            if moved and fallback is not None:
                notes.append(f"reason {fallback.fact_id} moved to the trust axis")
                continue
            if fallback is not None:
                if len(picks) < MAX_REASONS:
                    picks.append(_Pick(fallback, "fallback", False))
                else:  # replace the lowest unforced reason on an axis that keeps another
                    for i in range(len(picks) - 1, -1, -1):
                        p = picks[i]
                        same_axis = sum(1 for q in picks if q.reason.axis == p.reason.axis)
                        if not p.forced and same_axis > 1:
                            picks[i] = _Pick(fallback, "fallback", False)
                            break
                notes.append(
                    f"fallback {fallback.fact_id} used as the trust reason: {fallback.text[:60]}"
                )
                continue
        if spare is None:
            notes.append(f"no verified {axis} fact: axis not covered")
            continue
        if len(picks) < MAX_REASONS:
            add(*spare)
        else:
            # Replace the lowest-ranked unforced reason on an axis that keeps another reason.
            for i in range(len(picks) - 1, -1, -1):
                p = picks[i]
                same_axis = sum(1 for q in picks if q.reason.axis == p.reason.axis)
                if not p.forced and same_axis > 1:
                    picks[i] = _Pick(spare[0], spare[1], False)
                    break
        notes.append(f"added {spare[2].id} to cover {axis}")

    return [p.reason for p in picks][:MAX_REASONS], notes


def _base_topic(topic: str) -> str:
    return topic.removeprefix("unconfirmed_")


def one_source_enough(fact: Fact) -> bool:
    """Records, reputable press/regulators/courts, the vendor's own negative statement and our
    own checks may stand alone. Social media, forums, blogs and the rest need a second site."""
    if fact.source_type == "social":
        return False
    return (
        fact.source_kind == SourceKind.RECORD
        or fact.source_type in REPUTABLE_TYPES
        or (fact.source_kind == SourceKind.VENDOR and fact.polarity == Polarity.NEGATIVE)
        or fact.collector in SELF_EVIDENT_COLLECTORS
    )


def _event(fact: Fact) -> tuple[str, int] | None:
    return (_base_topic(fact.topic), fact.observed_at.year) if fact.observed_at else None


def is_notable(fact: Fact) -> bool:
    if fact.is_filler or (
        fact.source_kind == SourceKind.VENDOR and fact.polarity != Polarity.NEGATIVE
    ):
        return False
    return (
        is_forced(fact)
        or fact.is_hard_flag
        or is_breach(fact)
        or is_real_negative(fact)
        or fact.weight >= NOTABLE_WEIGHT
    )


def strength(fact: Fact) -> tuple[bool, bool, bool, float, str]:
    """Sort key, strongest first: contradictions, hard flags, breaches/real negatives, weight."""
    return (
        not is_forced(fact),
        not fact.is_hard_flag,
        not (is_breach(fact) or is_real_negative(fact)),
        -fact.weight,
        fact.id,
    )


def select_findings(
    finding_ids: list[str],
    facts: dict[str, Fact],
    passed: set[str],
    used: frozenset[str] = frozenset(),
) -> tuple[list[Finding], list[str]]:
    """Verified, notable facts that are not reasons (``used``: fact ids behind the reasons,
    merged age records included; also no second report of a reason's event), strongest first,
    at most MAX_FINDINGS. ``finding_ids`` (the judge's picks and the rule candidates) are always
    considered; every other verified notable fact is too. A finding that may not stand on one
    source needs a second verified fact on the same topic from another site."""
    notes: list[str] = []
    pool = dict.fromkeys([*finding_ids, *(fid for fid, f in facts.items() if is_notable(f))])
    verified = sorted(
        (facts[i] for i in pool if i in facts and i in passed and i not in used),
        key=strength,
    )
    out: list[Finding] = []
    seen_events: set[tuple[str, int]] = {
        e for i in used if i in facts and (e := _event(facts[i])) is not None
    }
    for f in verified:
        event = _event(f)
        if event and event in seen_events:
            notes.append(f"finding {f.id} dropped: same event as a reason or earlier finding")
            continue
        if not one_source_enough(f):
            sites = {
                registered_domain(g.source_url)
                for g in facts.values()
                if g.id in passed
                and g.topic
                and _base_topic(g.topic) == _base_topic(f.topic)
                and g.polarity == f.polarity
            }
            if len(sites) < 2:
                notes.append(f"finding {f.id} dropped: needs a second independent source")
                continue
        out.append(Finding(text=f.statement, source_url=f.source_url, fact_id=f.id))
        if event:
            seen_events.add(event)
        if len(out) >= MAX_FINDINGS:
            break
    dropped = [i for i in finding_ids if i in facts and i not in passed]
    if dropped:
        notes.append(f"findings not verified, dropped: {', '.join(dropped)}")
    return out, notes


def breach_reason(
    finding_only: frozenset[str], facts: dict[str, Fact], passed: set[str]
) -> Reason | None:
    """The trust reason built from an old, disclosed breach kept as a finding: the record's
    own observation (records first), plus the disclosure when a disclosure source verified."""
    breaches = sorted(
        (facts[i] for i in finding_only if i in facts and i in passed),
        key=lambda f: (f.source_kind != SourceKind.RECORD, strength(f)),
    )
    breaches = [f for f in breaches if f.axis == Axis.TRUST]
    if not breaches:
        return None
    breach = breaches[0]
    notice = next((f for f in facts.values() if f.id in passed and "disclosed" in f.tags), None)
    text = breach.statement.rstrip(".")
    if notice is not None:
        text += f"; the vendor disclosed it ({registered_domain(notice.source_url)})"
    return Reason(
        text=text,
        axis=Axis.TRUST,
        polarity=breach.polarity,
        source_url=breach.source_url,
        quote=breach.quote,
        fact_id=breach.id,
    )


def _as_trust(fact: Fact) -> Reason:
    return reason_from(fact).model_copy(update={"axis": Axis.TRUST})


def floor_reason(facts: dict[str, Fact], passed: set[str]) -> Reason | None:
    """The last-resort trust reason, from the verified domain record only: "Domain registered
    <date>, under <n> years of history", linked to that record. No source counts, no absence.
    None without a verified, dated domain-age record."""
    age = [
        f
        for f in facts.values()
        if f.id in passed
        and f.collector == AGE
        and f.topic in AGE_EVIDENCE_TOPICS
        and f.observed_at is not None
    ]
    if not age:
        return None
    first = min(age, key=lambda f: (f.observed_at, AGE_LINK_PRIORITY.index(f.topic)))
    since = first.observed_at
    if since is None:
        return None
    years = history_years(age, max(f.fetched_at for f in age))
    n = math.floor(years) + 1
    what = "Domain registered" if first.topic == "domain_registered" else "First domain record"
    return Reason(
        text=f"{what} {since.date().isoformat()}, under {n} year{'' if n == 1 else 's'} of history",
        axis=Axis.TRUST,
        polarity=Polarity.NEUTRAL,
        source_url=first.source_url,
        quote=first.quote,
        fact_id=first.id,
    )


def trust_reason_chain(
    facts: dict[str, Fact],
    passed: set[str],
    finding_only: frozenset[str] = frozenset(),
    breach: Reason | None = None,
) -> list[Reason]:
    """Trust reasons to use, in order, when no verified trust fact made the reasons: an old
    disclosed breach, a register record, a review platform with a real review count, an
    independent press mention, then the evidence-floor sentence."""
    ok = [
        f
        for f in facts.values()
        if f.id in passed and f.id not in finding_only and f.polarity != Polarity.NEGATIVE
    ]
    register = [f for f in ok if f.collector == "legal_id" and f.source_kind == SourceKind.RECORD]
    reviews = sorted(
        (
            f
            for f in ok
            if registered_domain(f.source_url) in REVIEW_SITE_NAMES
            and review_count(f.quote) >= MIN_REAL_REVIEWS
        ),
        key=lambda f: -review_count(f.quote),
    )
    press = sorted(
        (f for f in ok if f.source_type == "press" and f.source_kind == SourceKind.INDEPENDENT),
        key=lambda f: (-f.weight, f.id),
    )
    chain = [breach] if breach else []
    chain += [_as_trust(f) for f in (*register, *reviews, *press)]
    floor = floor_reason(facts, passed)
    return chain + ([floor] if floor else [])


def used_by_reasons(
    reasons: list[Reason], facts: dict[str, Fact], passed: set[str], finding_only: frozenset[str]
) -> frozenset[str]:
    """Fact ids a finding may not repeat: each reason's fact; every verified age record when
    the merged age reason is used; and, when a breach became the trust reason, every
    finding-only breach fact and disclosure notice (they describe the same incident)."""
    used = {r.fact_id for r in reasons}
    age_ids = {fid for fid, f in facts.items() if f.collector == AGE}
    if used & age_ids:
        used |= age_ids & passed
    if used & finding_only:
        used |= finding_only | {fid for fid, f in facts.items() if "disclosed" in f.tags}
    return frozenset(used)


def candidate_pool(
    ranked: list[Fact], finding_ids: list[str], size: int, all_facts: dict[str, Fact]
) -> list[Fact]:
    """What to verify first: the top of the ranking, every age fact (they merge), the best two
    facts of each collector (diversity), facts that findings need (looked up in all facts:
    finding-only facts are not in the reason ranking), and their dependencies."""
    by_id = all_facts
    pool: dict[str, Fact] = {f.id: f for f in ranked[:size]}
    per_collector: dict[str, int] = {}
    for f in ranked:
        if f.collector == AGE or is_forced(f):
            pool[f.id] = f
        elif per_collector.get(f.collector, 0) < 2:
            pool[f.id] = f
            per_collector[f.collector] = per_collector.get(f.collector, 0) + 1
    for fid in finding_ids:
        fact = by_id.get(fid)
        if fact is not None:
            pool[fid] = fact
    return list(pool.values())


async def verify_and_select(
    ranked: list[Fact],
    all_facts: list[Fact],
    finding_ids: list[str],
    fetcher: Fetcher,
    llm: StructuredLLM,
    snippets: dict[str, str],
    settings: Settings,
    domain: str = "",
    finding_only: frozenset[str] = frozenset(),
) -> tuple[Selection, list[VerifyResult]]:
    by_id = {f.id: f for f in all_facts}
    pool = candidate_pool(ranked, finding_ids, FIRST_POOL, by_id)
    deps = {
        d for f in pool for d in f.related_fact_ids if d in by_id and d not in {p.id for p in pool}
    }
    pool += [by_id[d] for d in sorted(deps)]
    results = await verify_facts(pool, fetcher, llm, snippets, settings)
    by_id |= fall_back_to_quotes(by_id, results)
    ranked = [by_id.get(f.id, f) for f in ranked]
    apply_dependencies(by_id, results)
    passed = {fid for fid, r in results.items() if r.passed}
    chain = trust_reason_chain(
        by_id, passed, finding_only, breach_reason(finding_only, by_id, passed)
    )
    reasons, notes = select_reasons(ranked, passed, domain, finding_only, chain)

    remaining = [f for f in ranked if f.id not in results]
    while len(reasons) < MIN_REASONS and remaining:
        batch, remaining = remaining[: MIN_REASONS * 2], remaining[MIN_REASONS * 2 :]
        more = await verify_facts(batch, fetcher, llm, snippets, settings)
        results |= more
        by_id |= fall_back_to_quotes(by_id, more)
        ranked = [by_id.get(f.id, f) for f in ranked]
        apply_dependencies(by_id, results)
        passed = {fid for fid, r in results.items() if r.passed}
        chain = trust_reason_chain(
            by_id, passed, finding_only, breach_reason(finding_only, by_id, passed)
        )
        reasons, notes = select_reasons(ranked, passed, domain, finding_only, chain)

    used = used_by_reasons(reasons, by_id, passed, finding_only)
    findings, finding_notes = select_findings(finding_ids, by_id, passed, used)
    notes += finding_notes
    trace.emit(
        {
            "event": "selection",
            "reasons": [r.fact_id for r in reasons],
            "findings": [f.fact_id for f in findings],
            "verified": sorted(passed),
            "notes": notes,
        }
    )
    return Selection(reasons=reasons, findings=findings, notes=notes), list(results.values())


async def verify_reasons(
    ranked: list[Fact],
    fetcher: Fetcher,
    llm: StructuredLLM,
    snippets: dict[str, str],
    settings: Settings,
) -> tuple[list[Reason], list[VerifyResult]]:
    """Reasons only (kept for callers that do not need findings)."""
    selection, checks = await verify_and_select(
        ranked, ranked, [], fetcher, llm, snippets, settings
    )
    return selection.reasons, checks
