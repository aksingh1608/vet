"""Judge in two stages.

1. Rules in code (deterministic, tested): which labels are allowed, which facts may be cited.
2. LLM judge: picks labels within the allowed set and ranks fact ids. Reasons are built in
   code from those facts, so no source can be invented.
"""

import json
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from vet.llm import LLMError, StructuredLLM, load_prompt
from vet.schemas import Axis, Claim, Fact, Polarity, RealAlive, SourceKind, Trust

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


def apply_rules(facts: list[Fact]) -> RuleOutcome:
    notes: list[str] = []
    # Rule 1: vendor-sourced facts never count as positive evidence.
    eligible = [
        f
        for f in facts
        if not (f.source_kind == SourceKind.VENDOR and f.polarity == Polarity.POSITIVE)
    ]
    if len(eligible) < len(facts):
        notes.append(f"ignored {len(facts) - len(eligible)} vendor-sourced positive facts")
    # Rule 4: looks facts only break ties, so they do not enter the scores.
    scored = [f for f in eligible if f.collector not in TIE_BREAK_COLLECTORS]
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
    """Deterministic ranking: hard flags, then non-vendor sources, then weight."""
    return sorted(
        facts,
        key=lambda f: (not f.is_hard_flag, f.source_kind == SourceKind.VENDOR, -f.weight, f.id),
    )


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
    findings: list[str]


class Judgement(BaseModel):
    real_alive: RealAlive
    trust: Trust
    confidence: float
    summary: str
    ranked_fact_ids: list[str]
    findings: list[str]
    used_llm: bool
    notes: list[str]


def _judge_input(claims: list[Claim], facts: list[Fact], rules: RuleOutcome) -> str:
    payload = {
        "claims": [{"id": c.id, "type": c.type, "text": c.text} for c in claims],
        "facts": [
            {
                "id": f.id,
                "statement": f.statement,
                "axis": f.axis,
                "polarity": f.polarity,
                "weight": f.weight,
                "source_kind": f.source_kind,
                "hard_flag": f.is_hard_flag,
                "source": f.source_url,
            }
            for f in facts
        ],
        "rules": {
            "allowed_real_alive": rules.allowed_real_alive,
            "allowed_trust": rules.allowed_trust,
            "scores": rules.scores,
            "notes": rules.notes,
        },
    }
    return f"<data>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</data>"


async def judge(llm: StructuredLLM, claims: list[Claim], facts: list[Fact]) -> Judgement:
    rules = apply_rules(facts)
    ranked = [f.id for f in rank_facts(rules.eligible)]
    try:
        out = await llm.structured(
            kind="judge",
            system=load_prompt("judge.md").replace("{rubric}", load_prompt("rubric.md")),
            user=_judge_input(claims, rules.eligible, rules),
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
            ranked_fact_ids=ranked[:MAX_CANDIDATES],
            findings=[],
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
    # Backups for the verifier: the judge's order first, then the deterministic ranking.
    ranked_ids = picked + [fid for fid in ranked if fid not in picked]
    return Judgement(
        real_alive=ra,
        trust=tr,
        confidence=min(max(out.confidence, 0.0), 1.0),
        summary=out.summary,
        ranked_fact_ids=ranked_ids,
        findings=out.findings[:5],
        used_llm=True,
        notes=notes,
    )
