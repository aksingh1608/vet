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
            finding_fact_ids=["made-up-2", "a1"],
        )

    result = await judge(FakeLLM({JudgeOut: respond}), [], [*SOLID, breach, vendor])
    assert result.trust == Trust.CAUTION  # nearest allowed label
    assert result.real_alive == RealAlive.YES
    assert result.confidence == 1.0
    assert result.ranked_fact_ids[:2] == ["b1", "a1"]
    assert "made-up-1" not in result.ranked_fact_ids and "v1" not in result.ranked_fact_ids
    # the breach is a finding even though the judge did not pick it; invented ids are dropped
    assert result.finding_fact_ids[0] == "b1"
    assert "made-up-2" not in result.finding_fact_ids
    assert any("clamped" in n for n in result.notes)


async def test_rules_only_fallback_when_llm_fails() -> None:
    result = await judge(FakeLLM(), [], SOLID)  # FakeLLM without responder raises LLMError
    assert not result.used_llm
    assert result.real_alive == RealAlive.YES
    assert result.ranked_fact_ids[0] in {"a1", "t1"}


# ---- trust calibration ------------------------------------------------------------------


def old_domain(years_ago: int = 30) -> Fact:
    return fact(
        "age-0",
        Axis.REAL_ALIVE,
        Polarity.POSITIVE,
        0.5,
        collector="age",
        source_kind=SourceKind.RECORD,
        topic="domain_registered",
        observed_at=NOW.replace(year=NOW.year - years_ago),
        source_url="https://lookup.icann.org/en/lookup?name=basecamp.com",
    )


def independent(fid: str, site: str, axis: Axis = Axis.REAL_ALIVE) -> Fact:
    return fact(fid, axis, Polarity.POSITIVE, 0.3, source_url=f"https://www.{site}/x")


US_VENDOR = [old_domain(), independent("fp-0", "g2.com"), independent("fp-1", "nytimes.com")]


def test_long_history_and_independent_sources_mean_trust_without_any_register() -> None:
    # A US vendor: no register record at all, which is missing evidence, never negative.
    rules = apply_rules(US_VENDOR, today=NOW)
    assert rules.allowed_trust == [Trust.TRUST]
    assert rules.scores["history_years"] > 10
    assert any("trust floor" in n for n in rules.notes)


def test_floor_needs_two_distinct_independent_sites() -> None:
    same_site = [old_domain(), independent("fp-0", "g2.com"), independent("fp-1", "g2.com")]
    assert apply_rules(same_site, today=NOW).allowed_trust != [Trust.TRUST]


def test_floor_needs_more_than_ten_years() -> None:
    young = [old_domain(years_ago=6), *US_VENDOR[1:]]
    assert apply_rules(young, today=NOW).allowed_trust != [Trust.TRUST]


def test_real_negative_allows_downgrade_below_floor() -> None:
    lawsuit = fact("risk-10", Axis.TRUST, Polarity.NEGATIVE, 0.4, collector="risk", topic="lawsuit")
    rules = apply_rules([*US_VENDOR, lawsuit], today=NOW)
    assert rules.allowed_trust == [Trust.CAUTION, Trust.TRUST]


def test_hard_flag_blocks_the_floor() -> None:
    breach = fact(
        "risk-0", Axis.TRUST, Polarity.NEGATIVE, 0.8, collector="risk", topic="breach",
        is_hard_flag=True, source_kind=SourceKind.RECORD,
    )  # fmt: skip
    rules = apply_rules([*US_VENDOR, breach], today=NOW)
    assert Trust.TRUST not in rules.allowed_trust


def test_mixed_reviews_are_minor_and_do_not_move_the_verdict() -> None:
    poor = fact("fp-3", Axis.TRUST, Polarity.NEGATIVE, 0.15, topic="reviews")
    with_reviews = apply_rules([*US_VENDOR, poor], today=NOW)
    assert with_reviews.allowed_trust == [Trust.TRUST]  # not a real negative
    assert with_reviews.scores["trust_neg"] == 0.0  # and not scored
    assert poor in with_reviews.eligible  # but it may still be cited


async def test_judge_cannot_go_below_the_floor() -> None:
    def respond(user: str) -> JudgeOut:
        return JudgeOut(
            real_alive=RealAlive.YES,
            trust=Trust.CAUTION,
            confidence=0.6,
            summary="s",
            reason_fact_ids=["age-0"],
            finding_fact_ids=[],
        )

    result = await judge(FakeLLM({JudgeOut: respond}), [], US_VENDOR)
    assert result.trust == Trust.TRUST


def test_contradictions_rank_first() -> None:
    from vet.judge import rank_facts

    gap = fact(
        "claims_check-0", Axis.TRUST, Polarity.NEGATIVE, 0.5, collector="claims_check",
        topic="founding_year_gap", source_kind=SourceKind.VENDOR,
    )  # fmt: skip
    hard = fact("guard-0", Axis.TRUST, Polarity.NEGATIVE, 0.9, is_hard_flag=True)
    assert [f.id for f in rank_facts([hard, *SOLID, gap])][:2] == ["claims_check-0", "guard-0"]


# ---- breach weighting --------------------------------------------------------------------


def breach(year: int, fid: str = "risk-0", **kw: object) -> Fact:
    return fact(
        fid,
        Axis.TRUST,
        Polarity.NEGATIVE,
        0.8,
        collector="risk",
        topic="breach",
        is_hard_flag=True,
        source_kind=SourceKind.RECORD,
        observed_at=NOW.replace(year=year),
        source_url="https://haveibeenpwned.com/PwnedWebsites#X",
        **kw,
    )


def notice(year: int) -> Fact:
    return fact(
        "risk-10",
        Axis.TRUST,
        Polarity.NEGATIVE,
        0.6,
        collector="risk",
        topic="breach",
        tags=["disclosed"],
        source_type="press",
        observed_at=NOW.replace(year=year),
        source_url="https://techcrunch.com/x",
    )


def test_recent_breach_makes_trust_caution_even_at_an_old_vendor() -> None:
    rules = apply_rules([*US_VENDOR, breach(2025, tags=["disclosed"])], today=NOW)
    assert rules.allowed_trust == [Trust.CAUTION]
    assert rules.finding_only == set()


def test_undisclosed_old_breach_makes_trust_caution() -> None:
    rules = apply_rules([*US_VENDOR, breach(2019)], today=NOW)
    assert rules.allowed_trust == [Trust.CAUTION]


def test_old_disclosed_breach_keeps_trust_and_becomes_a_finding() -> None:
    rules = apply_rules([*US_VENDOR, breach(2019), notice(2019)], today=NOW)
    assert rules.allowed_trust == [Trust.TRUST]
    assert rules.finding_only == {"risk-0", "risk-10"}
    assert any("finding, not a downgrade" in n for n in rules.notes)


def test_old_disclosed_breach_without_the_floor_is_caution() -> None:
    young_vendor = [old_domain(years_ago=4), *US_VENDOR[1:]]
    rules = apply_rules([*young_vendor, breach(2019), notice(2019)], today=NOW)
    assert rules.allowed_trust == [Trust.CAUTION]


def test_breach_age_threshold_is_configurable() -> None:
    facts = [*US_VENDOR, breach(2019), notice(2019)]
    assert apply_rules(facts, today=NOW, breach_recent_years=10).allowed_trust == [Trust.CAUTION]


async def test_finding_only_breach_is_never_a_reason_but_is_a_finding() -> None:
    def respond(user: str) -> JudgeOut:
        return JudgeOut(
            real_alive=RealAlive.YES,
            trust=Trust.TRUST,
            confidence=0.8,
            summary="s",
            reason_fact_ids=["risk-0", "age-0"],
            finding_fact_ids=[],
        )

    result = await judge(FakeLLM({JudgeOut: respond}), [], [*US_VENDOR, breach(2019), notice(2019)])
    assert result.trust == Trust.TRUST
    assert "risk-0" not in result.ranked_fact_ids
    assert "risk-0" in result.finding_fact_ids and "risk-0" in result.finding_only_ids


def test_unverified_negative_is_not_held_against_the_vendor() -> None:
    from vet.judge import Judgement, relabel_after_verification

    shutdown_rumour = fact(
        "risk-15", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.9, collector="risk",
        topic="discontinued", source_type="press",
    )  # fmt: skip
    facts = [*US_VENDOR, shutdown_rumour]
    assert apply_rules(facts, today=NOW).allowed_trust == [Trust.CAUTION, Trust.TRUST]
    before = Judgement(
        real_alive=RealAlive.YES, trust=Trust.CAUTION, confidence=0.7, summary="s",
        ranked_fact_ids=[], finding_fact_ids=[], used_llm=True, notes=[],
    )  # fmt: skip
    after = relabel_after_verification(before, facts, {"risk-15"})
    assert after.trust == Trust.TRUST  # the floor applies once the failed negative is dropped
    assert any("after verification" in n for n in after.notes)
    assert relabel_after_verification(before, facts, set()) is before


def test_unverified_breach_notice_still_counts_as_disclosure() -> None:
    notice_403 = notice(2019)
    rules = apply_rules(
        [*US_VENDOR, breach(2019), notice_403], today=NOW, failed=frozenset({"risk-10"})
    )
    assert rules.allowed_trust == [Trust.TRUST]
