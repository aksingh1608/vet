"""Categorised buyer view: the model sorts facts into rubric categories, code decides the label.

1. One fast-model call (``categorise.md``) returns categories, each with fact ids and a scope.
2. Code keeps a category only if every cited fact exists and passes verification (cited facts
   not yet checked are verified here, label-driving categories first, at most
   ``MAX_EXTRA_CHECKS``), and its evidence setting (``Settings.category_rules``) is met.
3. Code applies the offsets and the label rule:
   RED if any kept RED category, YELLOW if any kept YELLOW driver, GREEN if the base is met,
   else YELLOW with ``not_enough_evidence``.
4. The judge model writes a 60-100 word reasoning that may cite only the flags; a reasoning that
   names a URL not in the flags is replaced by a template.

If the categoriser fails, the caller keeps the baseline view.
"""

import json
import logging
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from vet.buyer_view import (
    ACTIONS,
    not_checked_clause,
    order_flags,
    source_type_of,
)
from vet.collectors.base import SiteIdentity
from vet.config import CategoryRule, Settings
from vet.fetch import Fetcher, host_matches
from vet.llm import LLMError, StructuredLLM, load_prompt, untrusted
from vet.schemas import (
    BuyerVerdict,
    Fact,
    Flag,
    FlagColour,
    Overall,
    RealAlive,
    SourceKind,
    Verdict,
)
from vet.text import registered_domain
from vet.verify import VerifyResult, fall_back_to_quotes, verify_facts

logger = logging.getLogger(__name__)

RED_CATEGORIES = frozenset(
    {
        "regulator_finding_core",
        "undisclosed_exposure",
        "discontinued",
        "misleading_social_proof",
        "governance_gap_regulated",
        "no_entity_plus_claims",
        "not_real_or_dead",
    }
)
YELLOW_DRIVERS = frozenset(
    {
        "ownership_churn",
        "owner_squeeze_record",
        "legacy_or_superseded",
        "licensing_ambiguity",
        "recent_incident_quiet",
        "continuity_small_vendor",
        "weak_trust_artefacts",
        "implausible_traction",
    }
)
CONTEXT = frozenset(
    {
        "dated_website",
        "single_maintainer_established",
        "acquired_by_strategic_parent",
        "pe_owned_or_carve_out",
        "leadership_change",
        "past_vulnerabilities",
        "past_incident",
        "large_confusing_portfolio",
        "docs_not_machine_readable",
        "startup_pivot",
        "parent_incident",
        "lawsuit_filed",
        "no_legal_entity_alone",
        "unrelated_fine_or_lawsuit",
        "domain_older_than_vendor",
    }
)
GREEN_SIGNALS = frozenset(
    {
        "wide_adoption",
        "active_releases",
        "long_track_record",
        "registered_entity",
        "credible_backing",
        "independent_audit_or_analyst",
        "commercial_ecosystem",
        "transparent_incident_response",
    }
)
BASE_SIGNALS = frozenset(
    {
        "wide_adoption",
        "long_track_record",
        "credible_backing",
        "independent_audit_or_analyst",
        "commercial_ecosystem",
    }
)
KNOWN = RED_CATEGORIES | YELLOW_DRIVERS | CONTEXT | GREEN_SIGNALS
STRONG_SOURCE_TYPES = frozenset(
    {"regulator", "court", "record", "register", "press", "analyst", "encyclopedia"}
)
DOMAIN_DATE_TOPICS = frozenset(
    {"domain_registered", "first_certificate", "first_capture", "capture_history"}
)
# Registration, certificate and archive dates: an old domain can be bought (plan v3).
DOMAIN_DATE_COLLECTORS = frozenset({"age"})
# Categories about the owner by definition: a parent/owner scope does not make them context.
OWNER_CATEGORIES = frozenset({"ownership_churn", "owner_squeeze_record"})
AGAINST_INTEREST_CATEGORIES = frozenset({"discontinued", "legacy_or_superseded"})
STATEMENT_AGAINST_INTEREST = "statement_against_interest"
# Where a dropped label-driving category lands instead (context, never a label change).
DOWNGRADE: dict[str, str] = {
    "regulator_finding_core": "unrelated_fine_or_lawsuit",
    "recent_incident_quiet": "past_incident",
    "ownership_churn": "pe_owned_or_carve_out",
    "continuity_small_vendor": "single_maintainer_established",
    "no_entity_plus_claims": "no_legal_entity_alone",
    "discontinued": "legacy_or_superseded",
}
NO_ENTITY_TOPIC = "no_legal_entity"
# With no legal entity, these turn into no_entity_plus_claims (RED); alone it is context.
CLAIM_TOPICS = frozenset({"traction_inconsistent", "logo_wall_not_customers"})
# Facts of these topics support the category even when the model did not cite them.
CATEGORY_TOPICS: dict[str, frozenset[str]] = {
    "regulator_finding_core": frozenset({"regulator_finding"}),
    "ownership_churn": frozenset({"ownership_change", "divestment_non_core"}),
    "licensing_ambiguity": frozenset({"licensing_change", "rename"}),
    "undisclosed_exposure": frozenset({"sanctions_exposure"}),
    "misleading_social_proof": frozenset({"logo_wall_not_customers"}),
    "implausible_traction": frozenset({"traction_inconsistent"}),
    "weak_trust_artefacts": frozenset({"policy_as_file", "coming_soon"}),
    "no_entity_plus_claims": frozenset({NO_ENTITY_TOPIC, *CLAIM_TOPICS}),
    "discontinued": frozenset({"lifecycle_end", "lifecycle_statement"}),
    "legacy_or_superseded": frozenset({"lifecycle_end", "lifecycle_statement"}),
    "long_track_record": frozenset({"same_vendor_since"}),
}
# Topics the collectors already classified: code proposes the category when the model did not.
# corporate: regulator and sanctions events. identity (Step 5): vendor-page contradictions.
PROPOSE_FROM_TOPIC: dict[str, str] = {
    "regulator_finding": "regulator_finding_core",
    "sanctions_exposure": "undisclosed_exposure",
    "logo_wall_not_customers": "misleading_social_proof",
    "traction_inconsistent": "implausible_traction",
    "policy_as_file": "weak_trust_artefacts",
    "coming_soon": "weak_trust_artefacts",
    # archive (Step 5, v3): read by topic; both facts are NEUTRAL with weight 0 on purpose.
    "same_vendor_since": "long_track_record",
    "domain_older_than_vendor": "domain_older_than_vendor",
}
DIFFERENT_EARLY_SITE = "domain_older_than_vendor"
# Publisher types that may support a label-driving category but never carry it alone.
WEAK_SOURCE_TYPES = frozenset({"directory", "social", "forum"})
MAX_EXTRA_CHECKS = 12
MIN_OWNERS_FOR_CHURN = 4
INCIDENT_RECENT_DAYS = 365
MIN_ADOPTION_PUBLISHERS = 2
REASONING_MIN_WORDS = 40
REASONING_MAX_WORDS = 130
_URL = re.compile(r"https?://[^\s)\]>\"']+")


class _Owner(BaseModel):
    name: str
    year: str = ""
    fact_id: str = ""


class _Category(BaseModel):
    category: str
    fact_ids: list[str]
    scope: str = "product"
    owners: list[_Owner] = Field(default_factory=list)


class _CategoriseOut(BaseModel):
    identifiable_vendor: bool
    operating_now: bool
    categories: list[_Category]


class _ReasoningOut(BaseModel):
    # Not named "reasoning": the judge model refuses a structured field with that name.
    paragraph: str


class Kept(BaseModel):
    category: str
    fact_ids: list[str]
    scope: str


def fact_payload(facts: list[Fact], results: dict[str, VerifyResult]) -> list[dict[str, object]]:
    def status(f: Fact) -> object:
        r = results.get(f.id)
        return "not yet" if r is None else r.passed

    return [
        {
            "id": f.id,
            "statement": f.statement,
            "source_type": source_type_of(f),
            "date": f.observed_at.date().isoformat() if f.observed_at else "",
            "topic": f.topic,
            "tags": f.tags,
            "verified": status(f),
        }
        for f in facts
    ]


async def ask_categories(
    llm: StructuredLLM, facts: list[Fact], results: dict[str, VerifyResult], identity: SiteIdentity
) -> _CategoriseOut:
    payload = {
        "vendor": identity.model_dump(),
        "facts": fact_payload(facts, results),
    }
    return await llm.structured(
        kind="fast",
        system=load_prompt("categorise.md"),
        user=untrusted(json.dumps(payload, ensure_ascii=False, default=str)),
        output=_CategoriseOut,
        step="categorise",
    )


def _is_strong(fact: Fact) -> bool:
    if "vendor_origin" in fact.tags or fact.source_kind == SourceKind.VENDOR:
        return False
    return source_type_of(fact) in STRONG_SOURCE_TYPES or fact.source_kind == SourceKind.RECORD


def _openable(fact: Fact, settings: Settings) -> bool:
    """A human can open the source without a login (not a snippet-only domain)."""
    host = re.sub(r"^https?://", "", fact.source_url).split("/")[0].lower()
    return not host_matches(host, settings.blocked_domains)


def evidence_ok(
    category: str, cited: list[Fact], rule: CategoryRule, settings: Settings
) -> tuple[bool, str]:
    if len(cited) < rule.min_facts:
        return False, f"needs {rule.min_facts} facts"
    label_driving = category in RED_CATEGORIES or category in YELLOW_DRIVERS
    if label_driving and not any(_openable(f, settings) for f in cited):
        return False, "only snippet-only sources"
    if label_driving and all(source_type_of(f) in WEAK_SOURCE_TYPES for f in cited):
        return False, "only directory, social or forum sources"
    if category == "discontinued" and not any(
        "lifecycle:end" in f.tags or f.topic not in CATEGORY_TOPICS["discontinued"] for f in cited
    ):
        return False, "the wording says superseded, not ended"
    if category == "no_entity_plus_claims" and not (
        any(f.topic == NO_ENTITY_TOPIC for f in cited)
        and any(f.topic in CLAIM_TOPICS for f in cited)
    ):
        return False, "needs both no legal entity and a traction or social-proof contradiction"
    if rule.needs_strong_source:
        against_interest = category in AGAINST_INTEREST_CATEGORIES and any(
            STATEMENT_AGAINST_INTEREST in f.tags or f.source_kind == SourceKind.VENDOR
            for f in cited
        )
        if not against_interest and not any(_is_strong(f) for f in cited):
            return False, "no strong source"
    if category == "long_track_record" and all(
        f.topic in DOMAIN_DATE_TOPICS
        or f.collector in DOMAIN_DATE_COLLECTORS
        or f.topic == DIFFERENT_EARLY_SITE
        for f in cited
    ):
        # Domain dates never count alone; a different early site on the domain cancels them too.
        return False, "domain dates alone are not a track record"
    if category == "wide_adoption":
        publishers = {
            registered_domain(f.source_url)
            for f in cited
            if f.source_kind != SourceKind.VENDOR and "vendor_origin" not in f.tags
        }
        if len(publishers) < MIN_ADOPTION_PUBLISHERS:
            return False, "fewer than two independent publishers"
    if category == "independent_audit_or_analyst" and all(
        f.source_kind == SourceKind.VENDOR for f in cited
    ):
        return False, "a badge on the vendor's site is a claim"
    return True, ""


def topic_support(cat: "_Category", facts: list[Fact]) -> list[str]:
    """Ids of facts of the category's own topics (same scope), cited or not."""
    topics = CATEGORY_TOPICS.get(cat.category, frozenset())
    want_product = cat.category in RED_CATEGORIES
    out = []
    for f in facts:
        if f.topic not in topics or f.id in cat.fact_ids:
            continue
        if want_product and "scope:parent" in f.tags:
            continue
        out.append(f.id)
    return out


def with_support(cat: "_Category", facts: list[Fact]) -> "_Category":
    extra = topic_support(cat, facts)
    return cat.model_copy(update={"fact_ids": [*cat.fact_ids, *extra]}) if extra else cat


def proposed_by_code(out: "_CategoriseOut", facts: list[Fact]) -> list["_Category"]:
    """Categories the collectors' event classification implies, unless the model named them."""
    named = {c.category for c in out.categories}
    grouped: dict[str, list[str]] = {}
    for f in facts:
        category = PROPOSE_FROM_TOPIC.get(f.topic)
        if category and "scope:parent" not in f.tags:
            grouped.setdefault(category, []).append(f.id)
    no_entity = [f.id for f in facts if f.topic == NO_ENTITY_TOPIC]
    if no_entity:
        claims = [f.id for f in facts if f.topic in CLAIM_TOPICS]
        if claims:
            grouped["no_entity_plus_claims"] = [*no_entity, *claims]
        else:
            grouped["no_legal_entity_alone"] = no_entity
    return [
        _Category(category=category, fact_ids=ids, scope="product")
        for category, ids in grouped.items()
        if category not in named
    ]


def count_owners(cat: _Category, by_id: dict[str, Fact]) -> int:
    names = {o.name.strip().lower() for o in cat.owners if o.name.strip() and o.fact_id in by_id}
    return len(names)


async def verify_cited(
    categories: list[_Category],
    by_id: dict[str, Fact],
    results: dict[str, VerifyResult],
    fetcher: Fetcher,
    llm: StructuredLLM,
    snippets: dict[str, str],
    settings: Settings,
) -> dict[str, VerifyResult]:
    """Verify cited facts that were not checked yet: label-driving categories first."""
    order = sorted(
        categories,
        key=lambda c: (
            0 if c.category in RED_CATEGORIES else 1 if c.category in YELLOW_DRIVERS else 2
        ),
    )
    todo: list[Fact] = []
    for cat in order:
        for fid in cat.fact_ids:
            fact = by_id.get(fid)
            if fact is not None and fid not in results and fact not in todo:
                todo.append(fact)
    todo = todo[:MAX_EXTRA_CHECKS]
    if not todo:
        return results
    extra = await verify_facts(todo, fetcher, llm, snippets, settings)
    return {**results, **extra}


def keep_categories(
    out: _CategoriseOut,
    by_id: dict[str, Fact],
    results: dict[str, VerifyResult],
    settings: Settings,
    today: datetime,
    notes: list[str],
) -> list[Kept]:
    kept: list[Kept] = []
    for cat in out.categories:
        name = cat.category
        if name not in KNOWN:
            notes.append(f"unknown category {name} dropped")
            continue
        missing = [i for i in cat.fact_ids if i not in by_id]
        if missing or not cat.fact_ids:
            notes.append(f"{name}: cites missing fact ids {missing or '(none)'}; dropped")
            continue
        failed = [i for i in cat.fact_ids if i not in results or not results[i].passed]
        cited_ids = [i for i in cat.fact_ids if i not in failed]
        if not cited_ids:
            notes.append(f"{name}: no cited fact passed verification; dropped")
            continue
        cited = [by_id[i] for i in cited_ids]
        rule = settings.category_rules.get(name, settings.category_rules.get("_default"))
        if rule is None:
            rule = CategoryRule(needs_strong_source=name in RED_CATEGORIES)
        ok, why = evidence_ok(name, cited, rule, settings)
        if not ok:
            target = DOWNGRADE.get(name)
            notes.append(f"{name}: {why}" + (f"; kept as {target}" if target else "; dropped"))
            if target:
                kept.append(Kept(category=target, fact_ids=cited_ids, scope=cat.scope))
            continue
        if name == "ownership_churn":
            owners = count_owners(cat, by_id)
            non_core = any(by_id[i].topic == "divestment_non_core" for i in cited_ids)
            if owners < MIN_OWNERS_FOR_CHURN and not non_core:
                notes.append(f"ownership_churn: {owners} owners counted; kept as context")
                kept.append(
                    Kept(category="pe_owned_or_carve_out", fact_ids=cited_ids, scope=cat.scope)
                )
                continue
        if name == "regulator_finding_core":
            years = settings.regulator_recent_years
            dates = [by_id[i].observed_at for i in cited_ids if by_id[i].observed_at]
            if dates and max(dates) < today - timedelta(days=365 * years):
                notes.append(f"regulator finding older than {years} years; kept as context")
                kept.append(
                    Kept(category="unrelated_fine_or_lawsuit", fact_ids=cited_ids, scope=cat.scope)
                )
                continue
        if name == "recent_incident_quiet":
            dates = [by_id[i].observed_at for i in cited_ids if by_id[i].observed_at]
            if dates and max(dates) < today - timedelta(days=INCIDENT_RECENT_DAYS):
                notes.append("incident older than 12 months; kept as context")
                kept.append(Kept(category="past_incident", fact_ids=cited_ids, scope=cat.scope))
                continue
        label_driving = name in RED_CATEGORIES or name in YELLOW_DRIVERS
        if label_driving and cat.scope == "parent" and name not in OWNER_CATEGORIES:
            target = "parent_incident" if "incident" in name else "unrelated_fine_or_lawsuit"
            notes.append(f"{name}: scope parent; kept as context {target}")
            kept.append(Kept(category=target, fact_ids=cited_ids, scope=cat.scope))
            continue
        kept.append(Kept(category=name, fact_ids=cited_ids, scope=cat.scope))
    return kept


def apply_offsets(kept: list[Kept], notes: list[str]) -> list[Kept]:
    names = {k.category for k in kept}
    out: list[Kept] = []
    for k in kept:
        if (
            k.category == "continuity_small_vendor"
            and {
                "wide_adoption",
                "long_track_record",
                "active_releases",
            }
            <= names
        ):
            notes.append("single maintainer with adoption, track record and releases: context")
            out.append(k.model_copy(update={"category": "single_maintainer_established"}))
        elif k.category == "recent_incident_quiet" and "transparent_incident_response" in names:
            notes.append("incident with a public write-up: context")
            out.append(k.model_copy(update={"category": "past_incident"}))
        else:
            out.append(k)
    return out


def hard_rule_categories(verdict: Verdict, facts: list[Fact], passed: set[str]) -> list[Kept]:
    """Today's rules that stay RED: product dead or not real, insolvency, injection attempts."""
    kept: list[Kept] = []
    if verdict.real_alive == RealAlive.NO:
        ids = [r.fact_id for r in verdict.reasons if r.fact_id] or [
            f.fact_id for f in verdict.findings
        ]
        if ids:
            kept.append(Kept(category="not_real_or_dead", fact_ids=ids[:3], scope="product"))
    for f in facts:
        if f.id in passed and f.is_hard_flag and f.topic in {"prompt_injection", "insolvency"}:
            kept.append(Kept(category="not_real_or_dead", fact_ids=[f.id], scope="product"))
    return kept


def decide(kept: list[Kept], base_met: bool) -> tuple[Overall, list[str]]:
    names = [k.category for k in kept]
    if any(n in RED_CATEGORIES for n in names):
        return Overall.RED, names
    if any(n in YELLOW_DRIVERS for n in names):
        return Overall.YELLOW, names
    if base_met:
        return Overall.GREEN, names
    return Overall.YELLOW, [*names, "not_enough_evidence"]


def base_is_met(
    out: _CategoriseOut, kept: list[Kept], verdict: Verdict, identity: SiteIdentity | None = None
) -> bool:
    names = {k.category for k in kept}
    named = identity is not None and bool(identity.vendor_name or identity.legal_entity)
    identifiable = out.identifiable_vendor or named or "registered_entity" in names
    operating = (
        out.operating_now or "active_releases" in names or verdict.real_alive == RealAlive.YES
    )
    return identifiable and operating and bool(names & BASE_SIGNALS)


def build_flags(kept: list[Kept], by_id: dict[str, Fact], overall: Overall) -> list[Flag]:
    flags: list[Flag] = []
    decided = False
    for k in kept:
        if k.category in RED_CATEGORIES:
            colour = FlagColour.RED
        elif k.category in GREEN_SIGNALS:
            colour = FlagColour.GREEN
        else:
            colour = FlagColour.YELLOW
        drives = (overall == Overall.RED and k.category in RED_CATEGORIES) or (
            overall == Overall.YELLOW and k.category in YELLOW_DRIVERS
        )
        for fid in k.fact_ids:
            fact = by_id.get(fid)
            if fact is None or not fact.source_url.startswith(("http://", "https://")):
                continue
            flags.append(
                Flag(
                    text=_flag_text(fact),
                    colour=colour,
                    source_url=fact.source_url,
                    source_type=_flag_source(fact),
                    category=k.category,
                    fact_id=fid,
                    decisive=drives and not decided,
                )
            )
            decided = decided or drives
    return order_flags(flags)


def _flag_source(fact: Fact) -> str:
    if STATEMENT_AGAINST_INTEREST in fact.tags or fact.source_kind == SourceKind.VENDOR:
        return "vendor statement"
    return source_type_of(fact)


def _flag_text(fact: Fact) -> str:
    text = fact.statement.strip()
    if fact.observed_at and str(fact.observed_at.year) not in text:
        text = f"{text.rstrip('.')} ({fact.observed_at.year})"
    return text


def template_reasoning(
    overall: Overall, flags: list[Flag], categories: list[str], not_checked: list[str]
) -> str:
    lead = next((f for f in flags if f.decisive), flags[0] if flags else None)
    why = f" The deciding evidence: {lead.text.rstrip('.')}." if lead else ""
    if overall == Overall.RED:
        tail = " A reader of the vendor's site alone would not see this."
    elif overall == Overall.YELLOW and "not_enough_evidence" in categories:
        tail = (
            " Independent evidence of adoption, track record or backing was not found; check "
            "named customer references before shortlisting."
        )
    elif overall == Overall.YELLOW:
        tail = " Check this point with the vendor before buying; a clear answer moves it to Green."
    else:
        tail = " Main limit: the review relies on public sources only."
    return (
        f"{overall.value.capitalize()}: {ACTIONS[overall]}.{why}{tail}"
        f"{not_checked_clause(not_checked)}"
    )


async def write_reasoning(
    llm: StructuredLLM,
    overall: Overall,
    flags: list[Flag],
    categories: list[str],
    not_checked: list[str],
) -> str:
    payload = {
        "call": overall.value,
        "buyer_action": ACTIONS[overall],
        "categories": categories,
        "flags": [
            {"colour": f.colour.value, "text": f.text, "url": f.source_url, "decisive": f.decisive}
            for f in flags
        ],
        "not_checked": not_checked,
    }
    system = load_prompt("buyer_reasoning.md")
    template = template_reasoning(overall, flags, categories, not_checked)
    try:
        out = await llm.structured(
            kind="judge",
            system=system,
            user=untrusted(json.dumps(payload, ensure_ascii=False)),
            output=_ReasoningOut,
            step="buyer_reasoning",
        )
    except LLMError as exc:
        logger.warning("buyer reasoning failed (%s); using the template", exc)
        return template
    text = out.paragraph.strip()
    allowed = {f.source_url for f in flags}
    bad_urls = [u for u in _URL.findall(text) if u.rstrip(".,;") not in allowed]
    words = len(text.split())
    if bad_urls or not REASONING_MIN_WORDS <= words <= REASONING_MAX_WORDS:
        logger.warning("buyer reasoning rejected (urls=%s, words=%d)", bad_urls, words)
        return template
    if not_checked and "Not checked" not in text:
        text += not_checked_clause(not_checked)
    return text


async def categorised_view(
    *,
    verdict: Verdict,
    facts: list[Fact],
    verification: list[VerifyResult],
    identity: SiteIdentity,
    not_checked: list[str],
    llm: StructuredLLM,
    fetcher: Fetcher,
    snippets: dict[str, str],
    settings: Settings,
    base: BuyerVerdict,
    today: datetime,
) -> tuple[BuyerVerdict, list[str], list[VerifyResult]]:
    """The categorised buyer view, the notes on what code dropped, and the extra checks."""
    by_id = {f.id: f for f in facts}
    results = {r.fact_id: r for r in verification}
    notes: list[str] = []
    out = await ask_categories(llm, facts, results, identity)
    categories = [*out.categories, *proposed_by_code(out, facts)]
    out = out.model_copy(update={"categories": [with_support(c, facts) for c in categories]})
    results = await verify_cited(out.categories, by_id, results, fetcher, llm, snippets, settings)
    by_id.update(fall_back_to_quotes(by_id, results))
    extra = [r for fid, r in results.items() if fid not in {v.fact_id for v in verification}]
    passed = {fid for fid, r in results.items() if r.passed}
    kept = keep_categories(out, by_id, results, settings, today, notes)
    kept += hard_rule_categories(verdict, facts, passed)
    kept = apply_offsets(kept, notes)
    overall, categories = decide(kept, base_is_met(out, kept, verdict, identity))
    flags = build_flags(kept, by_id, overall)
    reasoning = await write_reasoning(llm, overall, flags, categories, not_checked)
    view = base.model_copy(
        update={
            "overall": overall,
            "action": ACTIONS[overall],
            "categories": categories,
            "flags": flags,
            "reasoning": reasoning,
            "method": "categorised",
        }
    )
    return view, notes, extra
