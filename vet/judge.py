"""Judge in two stages.

1. Rules in code (deterministic, tested): which labels are allowed, which facts may be cited,
   and the trust calibration:
   * no queryable company register is missing evidence, never negative evidence;
   * domain history over 10 years + at least 2 independent positive facts from distinct
     sources + no hard flags -> trust is at least "trust", lowered only by real negatives
     (breach, lawsuit, fine, insolvency, contradicted claim, frozen or discontinued product);
   * mixed review scores are a minor negative that never enters the scores;
   * breaches: one younger than ``breach_recent_years`` or never disclosed by the vendor makes
     trust "caution". An older, disclosed breach at a vendor that meets the trust floor keeps
     "trust" and becomes a finding, not a reason;
   * the age records (RDAP, crt.sh, Wayback) are ONE signal: they count once, at the weight of
     the strongest, so a young domain is not three pieces of evidence;
   * "no" needs two independent negative signals (age counts as one); "do_not_trust" needs a
     red flag (a trust hard flag, a contradicted claim, or the hollow pattern);
   * a vendor younger than YOUNG_VENDOR_YEARS has no track record: trust at most "caution";
   * hollow pattern (records < 1 year old + founding year claimed far earlier + a checkable dead
     page or contradiction + no positive record or independent evidence) -> "no" and
     "do_not_trust". The absence of evidence only gates the pattern; it is never a reason;
   * a corroborated shutdown ("discontinued" hard flag) -> real_alive "no".
2. LLM judge: picks labels within the allowed set and ranks fact ids (reasons and findings).
   Reasons and findings are built in code from verified facts, so no source can be invented.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from vet.llm import LLMError, StructuredLLM, load_prompt, untrusted
from vet.schemas import Axis, Claim, ClaimCheck, Fact, Polarity, RealAlive, SourceKind, Trust
from vet.text import registered_domain

logger = logging.getLogger(__name__)

REAL_ALIVE_ORDER = [RealAlive.NO, RealAlive.UNCLEAR, RealAlive.YES]
TRUST_ORDER = [Trust.DO_NOT_TRUST, Trust.CAUTION, Trust.TRUST]
NO_NEEDS_NEGATIVE_WEIGHT = 0.8
NO_NEEDS_NEGATIVE_FACTS = 2
YES_NEEDS_POSITIVE_WEIGHT = 0.4
TRUST_NEEDS_POSITIVE_WEIGHT = 0.4
SEVERE_HARD_FLAG_WEIGHT = 0.8
TIE_BREAK_COLLECTORS = frozenset({"looks"})
MAX_CANDIDATES = 10
MAX_FINDINGS = 5

DEFAULT_BREACH_RECENT_YEARS = 3
TRUST_FLOOR_HISTORY_YEARS = 10.0
YOUNG_VENDOR_YEARS = 2.0
HOLLOW_MAX_YEARS = 1.0
AGE_COLLECTOR = "age"
CHECKABLE_HOLLOW_SIGNS = frozenset({"missing_page", "dead_help", "contradicted_claim"})
TRUST_FLOOR_INDEPENDENT_SOURCES = 2
AGE_EVIDENCE_TOPICS = frozenset({"domain_registered", "first_capture", "first_certificate"})
# Negatives that may lower trust below the floor.
REAL_NEGATIVE_TOPICS = frozenset(
    {"breach", "lawsuit", "fine", "insolvency", "contradicted_claim", "frozen", "discontinued"}
)
# Always the top reasons when they verify.
FORCED_TOPICS = frozenset({"contradicted_claim", "founding_year_gap"})
MINOR_TOPICS = frozenset({"reviews"})  # negative review scores: cited, never scored


@dataclass
class RuleOutcome:
    allowed_real_alive: list[RealAlive]
    allowed_trust: list[Trust]
    eligible: list[Fact]  # facts the judge may cite
    scores: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    finding_only: set[str] = field(default_factory=set)  # findings, never reasons

    @property
    def eligible_ids(self) -> set[str]:
        return {f.id for f in self.eligible}


@dataclass
class BreachAssessment:
    blocking: list[Fact]  # recent or undisclosed: trust becomes "caution"
    old_disclosed: list[Fact]  # may keep "trust" at a long-standing, well-evidenced vendor


def assess_breaches(
    facts: list[Fact], today: datetime, recent_years: int, notices: list[Fact] | None = None
) -> BreachAssessment:
    """Sort breach facts into blocking and old-and-disclosed. A breach without a date counts
    as recent. It counts as disclosed when it carries the ``disclosed`` tag, or another fact
    (in ``notices``, default ``facts``) tagged ``disclosed`` dates from the same year (+-1) or
    has no date."""
    breaches = [f for f in facts if f.topic == "breach" and f.polarity == Polarity.NEGATIVE]
    notices = [f for f in (facts if notices is None else notices) if "disclosed" in f.tags]
    years = {f.observed_at.year for f in notices if f.observed_at}
    undated_notice = any(f.observed_at is None for f in notices)
    out = BreachAssessment(blocking=[], old_disclosed=[])
    for b in breaches:
        when = b.observed_at
        recent = when is None or (today - when).days / 365.25 < recent_years
        disclosed = (
            "disclosed" in b.tags
            or undated_notice
            or (when is not None and any(abs(y - when.year) <= 1 for y in years))
        )
        (out.blocking if recent or not disclosed else out.old_disclosed).append(b)
    return out


def _sum(facts: list[Fact], axis: Axis, polarity: Polarity) -> float:
    return round(sum(f.weight for f in facts if f.axis == axis and f.polarity == polarity), 3)


def _signal_sum(facts: list[Fact], axis: Axis, polarity: Polarity) -> float:
    """Like _sum, but all age records together count once (the strongest)."""
    picked = [f for f in facts if f.axis == axis and f.polarity == polarity]
    age = [f.weight for f in picked if f.collector == AGE_COLLECTOR]
    rest = sum(f.weight for f in picked if f.collector != AGE_COLLECTOR)
    return round(rest + max(age, default=0.0), 3)


def _signal_count(facts: list[Fact], axis: Axis, polarity: Polarity) -> int:
    """Independent signals: every non-age fact, plus one for any number of age records."""
    picked = [f for f in facts if f.axis == axis and f.polarity == polarity]
    rest = sum(1 for f in picked if f.collector != AGE_COLLECTOR)
    return rest + (1 if any(f.collector == AGE_COLLECTOR for f in picked) else 0)


def is_hollow(facts: list[Fact], years: float, positive_sources: set[str]) -> bool:
    """A site that presents a long past with nothing behind it: every domain record under a
    year old, a founding year claimed long before that, at least one checkable dead page or
    contradiction, and no positive record or independent evidence at all."""
    young = 0.0 < years < HOLLOW_MAX_YEARS
    gap = any(f.topic in ("founding_year_gap", "contradicted_claim") for f in facts)
    checkable = any(f.topic in CHECKABLE_HOLLOW_SIGNS for f in facts)
    return young and gap and checkable and not positive_sources


def is_forced(fact: Fact) -> bool:
    return fact.topic in FORCED_TOPICS


def is_minor(fact: Fact) -> bool:
    return fact.topic in MINOR_TOPICS and fact.polarity == Polarity.NEGATIVE


def is_real_negative(fact: Fact) -> bool:
    return fact.polarity == Polarity.NEGATIVE and (
        fact.is_hard_flag or fact.topic in REAL_NEGATIVE_TOPICS
    )


def is_breach(fact: Fact) -> bool:
    return fact.topic == "breach" and fact.polarity == Polarity.NEGATIVE


def history_years(facts: list[Fact], today: datetime) -> float:
    """Years since the earliest domain record (registration, first capture, first cert)."""
    dates = [f.observed_at for f in facts if f.topic in AGE_EVIDENCE_TOPICS and f.observed_at]
    if not dates:
        return 0.0
    return round((today - min(dates)).days / 365.25, 1)


def independent_positive_sources(facts: list[Fact]) -> set[str]:
    """Distinct sites with a positive record/independent fact, domain-age records excluded."""
    return {
        registered_domain(f.source_url)
        for f in facts
        if f.polarity == Polarity.POSITIVE
        and f.source_kind != SourceKind.VENDOR
        and f.collector not in ("age", *TIE_BREAK_COLLECTORS)
    }


def apply_rules(
    facts: list[Fact],
    today: datetime | None = None,
    breach_recent_years: int = DEFAULT_BREACH_RECENT_YEARS,
    failed: frozenset[str] = frozenset(),
) -> RuleOutcome:
    """``failed``: negative facts whose source did not verify. They no longer count against
    the vendor (hard flags, breaches, real negatives, scores), but a breach notice among them
    still counts as disclosure."""
    notes: list[str] = []
    # Rule 1: vendor-sourced facts never count as positive evidence.
    eligible = [
        f
        for f in facts
        if not (f.source_kind == SourceKind.VENDOR and f.polarity == Polarity.POSITIVE)
    ]
    if len(eligible) < len(facts):
        notes.append(f"ignored {len(facts) - len(eligible)} vendor-sourced positive facts")
    # Negative facts that failed verification are not held against the vendor.
    dropped = [f for f in eligible if f.id in failed and f.polarity == Polarity.NEGATIVE]
    if dropped:
        notes.append(f"{len(dropped)} unverified negative fact(s) not held against the vendor")
    counted = [f for f in eligible if f not in dropped]
    # Rule 4: looks facts only break ties, so they do not enter the scores. Mixed review
    # scores are a minor negative and do not enter them either.
    scored = [f for f in counted if f.collector not in TIE_BREAK_COLLECTORS and not is_minor(f)]
    evidence = [f for f in scored if f.source_kind != SourceKind.VENDOR]

    ra_pos = _signal_sum(evidence, Axis.REAL_ALIVE, Polarity.POSITIVE)
    ra_neg = _signal_sum(scored, Axis.REAL_ALIVE, Polarity.NEGATIVE)
    tr_pos = _sum(evidence, Axis.TRUST, Polarity.POSITIVE)
    tr_neg = _sum(scored, Axis.TRUST, Polarity.NEGATIVE)
    ra_neg_count = _signal_count(scored, Axis.REAL_ALIVE, Polarity.NEGATIVE)
    # Breaches have their own rule (below); every other hard flag caps as before.
    hard = [f for f in counted if f.is_hard_flag and not is_breach(f)]
    hard_ra = [f for f in hard if f.axis == Axis.REAL_ALIVE]
    hard_tr = [f for f in hard if f.axis == Axis.TRUST]
    today = today or max((f.fetched_at for f in facts), default=datetime.now(UTC))
    breaches = assess_breaches(counted, today, breach_recent_years, notices=eligible)

    allowed_ra = list(REAL_ALIVE_ORDER)
    allowed_tr = list(TRUST_ORDER)

    # Rule 3: "no" needs negative evidence; missing mentions alone give at most "unclear".
    if not hard_ra and (
        ra_neg < NO_NEEDS_NEGATIVE_WEIGHT or ra_neg_count < NO_NEEDS_NEGATIVE_FACTS
    ):
        allowed_ra.remove(RealAlive.NO)
        notes.append("real_alive=no not allowed: not enough negative evidence")
    if ra_pos < YES_NEEDS_POSITIVE_WEIGHT or hard_ra:
        allowed_ra.remove(RealAlive.YES)
        notes.append(
            "real_alive=yes not allowed: no record/independent evidence of life"
            if not hard_ra
            else "real_alive capped by a hard flag"
        )
    # Rule 2: any hard flag caps the verdict.
    if hard:
        if Trust.TRUST in allowed_tr:
            allowed_tr.remove(Trust.TRUST)
        notes.append(f"trust capped at caution by {len(hard)} hard flag(s)")
    if any(f.weight >= SEVERE_HARD_FLAG_WEIGHT for f in hard_tr):
        allowed_tr = [Trust.DO_NOT_TRUST, Trust.CAUTION]
    if tr_pos < TRUST_NEEDS_POSITIVE_WEIGHT and Trust.TRUST in allowed_tr:
        allowed_tr.remove(Trust.TRUST)
        notes.append("trust=trust not allowed: no record/independent trust evidence")

    # Trust floor: a long, consistent history plus independent evidence and no hard flags.
    years = history_years(eligible, today)
    sources = independent_positive_sources(eligible)
    old_ids = {f.id for f in breaches.old_disclosed}
    real_negatives = [f for f in counted if is_real_negative(f) and f.id not in old_ids]
    floor_met = (
        not hard
        and years > TRUST_FLOOR_HISTORY_YEARS
        and len(sources) >= TRUST_FLOOR_INDEPENDENT_SOURCES
    )
    finding_only: set[str] = set()
    if breaches.blocking or (breaches.old_disclosed and not floor_met):
        # Recent or undisclosed breach (or an old one without the floor): "caution", unless
        # another hard flag already allows worse.
        allowed_tr = [t for t in allowed_tr if t != Trust.TRUST] if hard else [Trust.CAUTION]
        named = breaches.blocking or breaches.old_disclosed
        notes.append(
            f"trust capped at caution by {len(named)} breach(es): "
            + ("recent or undisclosed" if breaches.blocking else "old and disclosed, no floor")
        )
    elif floor_met:
        finding_only = old_ids
        if old_ids:
            notes.append(
                f"{len(old_ids)} old, disclosed breach(es) at a vendor meeting the trust floor:"
                " finding, not a downgrade"
            )
        if real_negatives:
            allowed_tr = [Trust.CAUTION, Trust.TRUST]
            notes.append(
                f"trust floor met ({years:g} years, {len(sources)} independent sources) but "
                f"{len(real_negatives)} real negative(s): caution or trust"
            )
        else:
            allowed_tr = [Trust.TRUST]
            notes.append(
                f"trust floor: {years:g} years of domain history, {len(sources)} independent "
                f"sources, no hard flags or real negatives -> trust"
            )

    # Red-flag gates, applied last so they override the softer rules above.
    contradicted = any(f.topic == "contradicted_claim" for f in counted)
    hollow = is_hollow(eligible, years, sources)
    shutdown = [f for f in hard_ra if f.topic == "discontinued"]
    if hollow:
        allowed_ra, allowed_tr = [RealAlive.NO], [Trust.DO_NOT_TRUST]
        notes.append(
            f"hollow pattern: records only {years:g} years old, a founding year claimed long "
            "before, dead pages or contradictions, and no positive evidence -> no, do_not_trust"
        )
    else:
        if shutdown:
            allowed_ra = [RealAlive.NO]
            notes.append("real_alive=no: a corroborated shutdown of this product")
        if 0.0 < years < YOUNG_VENDOR_YEARS and Trust.TRUST in allowed_tr:
            allowed_tr = [t for t in allowed_tr if t != Trust.TRUST] or [Trust.CAUTION]
            notes.append(f"trust capped at caution: only {years:g} years of history")
        if not (hard_tr or contradicted) and len(allowed_tr) > 1:
            allowed_tr = [t for t in allowed_tr if t != Trust.DO_NOT_TRUST]
        if not (hard_tr or contradicted) and allowed_tr == [Trust.DO_NOT_TRUST]:
            allowed_tr = [Trust.CAUTION]

    return RuleOutcome(
        allowed_real_alive=allowed_ra,
        allowed_trust=allowed_tr,
        eligible=eligible,
        scores={
            "real_alive_pos": ra_pos,
            "real_alive_neg": ra_neg,
            "trust_pos": tr_pos,
            "trust_neg": tr_neg,
            "hard_flags": float(len(hard)),
            "history_years": years,
            "independent_sources": float(len(sources)),
            "real_negatives": float(len(real_negatives)),
            "breaches_blocking": float(len(breaches.blocking)),
            "breaches_old_disclosed": float(len(breaches.old_disclosed)),
            "real_alive_negative_signals": float(ra_neg_count),
            "hollow": float(hollow),
        },
        notes=notes,
        finding_only=finding_only,
    )


def clamp(value: str, allowed: list, order: list):  # type: ignore[type-arg]
    """Nearest allowed label to the requested one (ties go to the more cautious label)."""
    try:
        wanted = order.index(type(order[0])(value))
    except ValueError:
        wanted = len(order) // 2
    return min(allowed, key=lambda a: (abs(order.index(a) - wanted), order.index(a)))


def rank_facts(facts: list[Fact]) -> list[Fact]:
    """Deterministic ranking: forced (contradictions), hard flags, non-filler, non-vendor
    sources, then weight."""
    return sorted(
        facts,
        key=lambda f: (
            not is_forced(f),
            not f.is_hard_flag,
            f.is_filler,
            f.source_kind == SourceKind.VENDOR,
            -f.weight,
            f.id,
        ),
    )


def finding_candidates(facts: list[Fact]) -> list[str]:
    """Facts that are findings whether or not the judge picks them: contradictions, founding
    year gaps, breaches and other real negatives (AGENTS.md decision 2)."""
    return [f.id for f in rank_facts(facts) if is_forced(f) or is_real_negative(f) or is_breach(f)]


def rules_only_labels(rules: RuleOutcome) -> tuple[RealAlive, Trust]:
    s = rules.scores
    ra = (
        RealAlive.YES
        if s["real_alive_pos"] > s["real_alive_neg"]
        else (
            RealAlive.NO if s["real_alive_neg"] > s["real_alive_pos"] + 0.5 else RealAlive.UNCLEAR
        )
    )
    tr = (
        Trust.TRUST
        if s["trust_pos"] > s["trust_neg"] + 0.3
        else (Trust.DO_NOT_TRUST if s["trust_neg"] > s["trust_pos"] + 0.5 else Trust.CAUTION)
    )
    return (
        clamp(ra, rules.allowed_real_alive, REAL_ALIVE_ORDER),
        clamp(tr, rules.allowed_trust, TRUST_ORDER),
    )


class JudgeOut(BaseModel):
    real_alive: RealAlive
    trust: Trust
    confidence: float
    summary: str
    reason_fact_ids: list[str]
    finding_fact_ids: list[str]


class Judgement(BaseModel):
    real_alive: RealAlive
    trust: Trust
    confidence: float
    summary: str
    ranked_fact_ids: list[str]
    finding_fact_ids: list[str]
    used_llm: bool
    notes: list[str]
    finding_only_ids: list[str] = Field(default_factory=list)


def _judge_input(
    claims: list[Claim], checks: list[ClaimCheck], facts: list[Fact], rules: RuleOutcome
) -> str:
    payload = {
        "claims": [{"id": c.id, "type": c.type, "text": c.text} for c in claims],
        "claim_checks": [c.model_dump() for c in checks],
        "facts": [
            {
                "id": f.id,
                "statement": f.statement,
                "axis": f.axis,
                "polarity": f.polarity,
                "weight": f.weight,
                "source_kind": f.source_kind,
                "collector": f.collector,
                "topic": f.topic,
                "hard_flag": f.is_hard_flag,
                "filler": f.is_filler,
                "source": f.source_url,
            }
            for f in facts
        ],
    }
    rules_block = {
        "allowed_real_alive": rules.allowed_real_alive,
        "allowed_trust": rules.allowed_trust,
        "scores": rules.scores,
        "notes": rules.notes,
    }
    return (
        untrusted(json.dumps(payload, ensure_ascii=False, default=str))
        + f"\n<rules>\n{json.dumps(rules_block, default=str)}\n</rules>"
    )


async def judge(
    llm: StructuredLLM,
    claims: list[Claim],
    facts: list[Fact],
    checks: list[ClaimCheck] | None = None,
    breach_recent_years: int = DEFAULT_BREACH_RECENT_YEARS,
) -> Judgement:
    rules = apply_rules(facts, breach_recent_years=breach_recent_years)
    only = rules.finding_only
    ranked = [f.id for f in rank_facts(rules.eligible) if f.id not in only]
    must_find = finding_candidates(rules.eligible)
    try:
        out = await llm.structured(
            kind="judge",
            system=load_prompt("judge.md").replace("{rubric}", load_prompt("rubric.md")),
            user=_judge_input(claims, checks or [], rules.eligible, rules),
            output=JudgeOut,
            step="judge",
        )
    except LLMError as exc:
        logger.warning("LLM judge failed (%s); using rules-only verdict", exc)
        ra, tr = rules_only_labels(rules)
        return Judgement(
            real_alive=ra,
            trust=tr,
            confidence=0.3,
            summary="Verdict from deterministic rules only (LLM judge unavailable).",
            ranked_fact_ids=ranked,
            finding_fact_ids=must_find[: MAX_FINDINGS * 2],
            used_llm=False,
            notes=rules.notes,
            finding_only_ids=sorted(only),
        )

    allowed_ids = rules.eligible_ids
    picked: list[str] = []
    for fid in out.reason_fact_ids:
        if fid in allowed_ids and fid not in picked and fid not in only:
            picked.append(fid)
    dropped = len(out.reason_fact_ids) - len(picked)
    notes = list(rules.notes)
    if dropped:
        notes.append(f"judge cited {dropped} unknown or ineligible fact id(s); ignored")
    ra = clamp(out.real_alive, rules.allowed_real_alive, REAL_ALIVE_ORDER)
    tr = clamp(out.trust, rules.allowed_trust, TRUST_ORDER)
    if ra != out.real_alive or tr != out.trust:
        notes.append(f"judge labels clamped by rules: {out.real_alive}->{ra}, {out.trust}->{tr}")
    # Forced facts (contradictions) first, then the judge's order, then the deterministic
    # ranking as backups for the verifier.
    forced = [fid for fid in ranked if fid in {f.id for f in rules.eligible if is_forced(f)}]
    ranked_ids = list(dict.fromkeys(forced + picked + ranked))
    findings = list(
        dict.fromkeys(must_find + [fid for fid in out.finding_fact_ids if fid in allowed_ids])
    )
    return Judgement(
        real_alive=ra,
        trust=tr,
        confidence=min(max(out.confidence, 0.0), 1.0),
        summary=out.summary,
        ranked_fact_ids=ranked_ids,
        finding_fact_ids=findings[: MAX_FINDINGS * 2],
        used_llm=True,
        notes=notes,
        finding_only_ids=sorted(only),
    )


def relabel_after_verification(
    judgement: Judgement,
    facts: list[Fact],
    failed: set[str],
    breach_recent_years: int = DEFAULT_BREACH_RECENT_YEARS,
) -> Judgement:
    """Re-apply the rules without the negative facts that failed verification and move the
    labels to the nearest label the rules now allow."""
    negatives = {f.id for f in facts if f.id in failed and f.polarity == Polarity.NEGATIVE}
    if not negatives:
        return judgement
    rules = apply_rules(facts, breach_recent_years=breach_recent_years, failed=frozenset(negatives))
    ra = clamp(judgement.real_alive, rules.allowed_real_alive, REAL_ALIVE_ORDER)
    tr = clamp(judgement.trust, rules.allowed_trust, TRUST_ORDER)
    notes = [*judgement.notes, *[n for n in rules.notes if "unverified" in n]]
    if (ra, tr) != (judgement.real_alive, judgement.trust):
        notes.append(f"after verification: {judgement.real_alive}->{ra}, {judgement.trust}->{tr}")
    return judgement.model_copy(
        update={
            "real_alive": ra,
            "trust": tr,
            "notes": notes,
        }
    )
