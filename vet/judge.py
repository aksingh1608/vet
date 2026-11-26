"""Judge in two stages.

1. Rules in code (deterministic, tested): which labels are allowed, which facts may be cited,
   and the trust calibration:
   * no queryable company register is missing evidence, never negative evidence;
   * domain history over 10 years + at least 2 independent positive facts from distinct
     sources + no hard flags -> trust is at least "trust", lowered only by real negatives
     (breach, lawsuit, fine, insolvency, contradicted claim, frozen or discontinued product);
   * mixed review scores are a minor negative that never enters the scores.
2. LLM judge: picks labels within the allowed set and ranks fact ids (reasons and findings).
   Reasons and findings are built in code from verified facts, so no source can be invented.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

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

TRUST_FLOOR_HISTORY_YEARS = 10.0
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

    @property
    def eligible_ids(self) -> set[str]:
        return {f.id for f in self.eligible}


def _sum(facts: list[Fact], axis: Axis, polarity: Polarity) -> float:
    return round(sum(f.weight for f in facts if f.axis == axis and f.polarity == polarity), 3)


def is_forced(fact: Fact) -> bool:
    return fact.topic in FORCED_TOPICS


def is_minor(fact: Fact) -> bool:
    return fact.topic in MINOR_TOPICS and fact.polarity == Polarity.NEGATIVE


def is_real_negative(fact: Fact) -> bool:
    return fact.polarity == Polarity.NEGATIVE and (
        fact.is_hard_flag or fact.topic in REAL_NEGATIVE_TOPICS
    )


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


def apply_rules(facts: list[Fact], today: datetime | None = None) -> RuleOutcome:
    notes: list[str] = []
    # Rule 1: vendor-sourced facts never count as positive evidence.
    eligible = [
        f
        for f in facts
        if not (f.source_kind == SourceKind.VENDOR and f.polarity == Polarity.POSITIVE)
    ]
    if len(eligible) < len(facts):
        notes.append(f"ignored {len(facts) - len(eligible)} vendor-sourced positive facts")
    # Rule 4: looks facts only break ties, so they do not enter the scores. Mixed review
    # scores are a minor negative and do not enter them either.
    scored = [f for f in eligible if f.collector not in TIE_BREAK_COLLECTORS and not is_minor(f)]
    evidence = [f for f in scored if f.source_kind != SourceKind.VENDOR]

    ra_pos = _sum(evidence, Axis.REAL_ALIVE, Polarity.POSITIVE)
    ra_neg = _sum(scored, Axis.REAL_ALIVE, Polarity.NEGATIVE)
    tr_pos = _sum(evidence, Axis.TRUST, Polarity.POSITIVE)
    tr_neg = _sum(scored, Axis.TRUST, Polarity.NEGATIVE)
    ra_neg_count = sum(
        1 for f in scored if f.axis == Axis.REAL_ALIVE and f.polarity == Polarity.NEGATIVE
    )
    hard = [f for f in eligible if f.is_hard_flag]
    hard_ra = [f for f in hard if f.axis == Axis.REAL_ALIVE]
    hard_tr = [f for f in hard if f.axis == Axis.TRUST]

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
    today = today or max((f.fetched_at for f in facts), default=datetime.now(UTC))
    years = history_years(eligible, today)
    sources = independent_positive_sources(eligible)
    real_negatives = [f for f in eligible if is_real_negative(f)]
    if (
        not hard
        and years > TRUST_FLOOR_HISTORY_YEARS
        and len(sources) >= TRUST_FLOOR_INDEPENDENT_SOURCES
    ):
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
        },
        notes=notes,
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
    year gaps and real negatives (AGENTS.md decision 2)."""
    return [f.id for f in rank_facts(facts) if is_forced(f) or is_real_negative(f)]


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
) -> Judgement:
    rules = apply_rules(facts)
    ranked = [f.id for f in rank_facts(rules.eligible)]
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
            finding_fact_ids=must_find[:MAX_FINDINGS],
            used_llm=False,
            notes=rules.notes,
        )

    allowed_ids = rules.eligible_ids
    picked: list[str] = []
    for fid in out.reason_fact_ids:
        if fid in allowed_ids and fid not in picked:
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
    )
