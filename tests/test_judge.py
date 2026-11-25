from datetime import UTC, datetime

from tests.conftest import FakeLLM
from vet.judge import JudgeOut, apply_rules, judge
from vet.schemas import Axis, Fact, Polarity, RealAlive, SourceKind, Trust

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def fact(fid: str, axis: Axis, polarity: Polarity, weight: float, **kw: object) -> Fact:
    data = {
        "id": fid,
        "collector": kw.pop("collector", "footprint"),
        "statement": f"statement {fid}",
        "axis": axis,
        "polarity": polarity,
        "weight": weight,
        "source_url": f"https://src.example/{fid}",
        "quote": "q",
        "source_kind": kw.pop("source_kind", SourceKind.INDEPENDENT),
        "fetched_at": NOW,
    }
    data.update(kw)
    return Fact.model_validate(data)


SOLID = [
    fact("a1", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.5, source_kind=SourceKind.RECORD),
    fact("a2", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.4),
    fact("t1", Axis.TRUST, Polarity.POSITIVE, 0.5, source_kind=SourceKind.RECORD),
]


def test_vendor_positive_facts_never_count() -> None:
    vendor_only = [
        fact("v1", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.9, source_kind=SourceKind.VENDOR),
        fact("v2", Axis.TRUST, Polarity.POSITIVE, 0.9, source_kind=SourceKind.VENDOR),
    ]
    rules = apply_rules(vendor_only)
    assert rules.eligible == []
    assert RealAlive.YES not in rules.allowed_real_alive
    assert Trust.TRUST not in rules.allowed_trust


def test_solid_evidence_allows_everything_but_no() -> None:
    rules = apply_rules(SOLID)
    assert rules.allowed_real_alive == [RealAlive.UNCLEAR, RealAlive.YES]
    assert Trust.TRUST in rules.allowed_trust


def test_no_requires_negative_evidence_not_silence() -> None:
    rules = apply_rules([])  # nothing found at all
    assert rules.allowed_real_alive == [RealAlive.UNCLEAR]


def test_no_allowed_with_enough_negative_evidence() -> None:
    negatives = [
        fact("n1", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.5, source_kind=SourceKind.RECORD),
        fact("n2", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.4, source_kind=SourceKind.RECORD),
    ]
    assert RealAlive.NO in apply_rules(negatives).allowed_real_alive


def test_hard_flag_caps_trust() -> None:
    breach = fact("b1", Axis.TRUST, Polarity.NEGATIVE, 0.8, is_hard_flag=True)
    rules = apply_rules([*SOLID, breach])
    assert rules.allowed_trust == [Trust.DO_NOT_TRUST, Trust.CAUTION]
    mild = fact("b2", Axis.TRUST, Polarity.NEGATIVE, 0.5, is_hard_flag=True)
    assert Trust.TRUST not in apply_rules([*SOLID, mild]).allowed_trust


def test_looks_facts_do_not_enter_scores() -> None:
    looks = fact("l1", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.9, collector="looks")
    assert RealAlive.YES not in apply_rules([looks]).allowed_real_alive


async def test_llm_judge_is_clamped_and_cannot_invent_sources() -> None:
    breach = fact("b1", Axis.TRUST, Polarity.NEGATIVE, 0.8, is_hard_flag=True)
    vendor = fact("v1", Axis.TRUST, Polarity.POSITIVE, 0.9, source_kind=SourceKind.VENDOR)

    def respond(user: str) -> JudgeOut:
        assert "v1" not in user  # vendor positives are not even shown to the judge
        return JudgeOut(
            real_alive=RealAlive.YES,
            trust=Trust.TRUST,  # not allowed with a severe hard flag
            confidence=1.4,
            summary="Looks fine.",
            reason_fact_ids=["made-up-1", "b1", "v1", "a1", "b1"],
            findings=["f"] * 9,
        )

    result = await judge(FakeLLM({JudgeOut: respond}), [], [*SOLID, breach, vendor])
    assert result.trust == Trust.CAUTION  # nearest allowed label
    assert result.real_alive == RealAlive.YES
    assert result.confidence == 1.0
    assert result.ranked_fact_ids[:2] == ["b1", "a1"]
    assert "made-up-1" not in result.ranked_fact_ids and "v1" not in result.ranked_fact_ids
    assert len(result.findings) == 5
    assert any("clamped" in n for n in result.notes)


async def test_rules_only_fallback_when_llm_fails() -> None:
    result = await judge(FakeLLM(), [], SOLID)  # FakeLLM without responder raises LLMError
    assert not result.used_llm
    assert result.real_alive == RealAlive.YES
    assert result.ranked_fact_ids[0] in {"a1", "t1"}
