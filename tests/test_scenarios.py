"""End-to-end calibration scenarios, no network: facts as the collectors would produce them,
fed through claims_check and the judge. Each scenario is judged three ways: rules only (LLM
down), an optimistic LLM (says yes / trust) and a pessimistic LLM (says no / do_not_trust).
The rules must land on the expected calls whatever the model says."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import FakeLLM
from vet.claims_check import check_claims
from vet.judge import JudgeOut, apply_rules, judge
from vet.schemas import Axis, Claim, Fact, Polarity, RealAlive, SourceKind, Trust

NOW = datetime(2026, 11, 26, tzinfo=UTC)


def fact(fid: str, collector: str, axis: Axis, polarity: Polarity, weight: float, **kw) -> Fact:  # noqa: ANN003
    data: dict[str, object] = {
        "id": fid,
        "collector": collector,
        "statement": f"statement {fid}",
        "axis": axis,
        "polarity": polarity,
        "weight": weight,
        "source_url": f"https://{collector}.example/{fid}",
        "quote": "q",
        "source_kind": SourceKind.RECORD,
        "fetched_at": NOW,
    }
    data.update(kw)
    return Fact.model_validate(data)


def age_records(domain: str, registered: datetime, weight_polarity: tuple[Polarity, float]):
    """RDAP, first certificate and first capture, all at the same date (as for a new domain)."""
    polarity, weight = weight_polarity
    return [
        fact("age-0", "age", Axis.REAL_ALIVE, polarity, weight, topic="domain_registered",
             observed_at=registered, source_url=f"https://lookup.icann.org/en/lookup?name={domain}"),
        fact("age-1", "age", Axis.REAL_ALIVE, polarity, weight, topic="first_certificate",
             observed_at=registered + timedelta(days=1), source_url=f"https://crt.sh/?q={domain}"),
        fact("age-2", "age", Axis.REAL_ALIVE, polarity, weight, topic="first_capture",
             observed_at=registered + timedelta(days=5),
             source_url=f"https://web.archive.org/web/2026/{domain}/", quote=""),
    ]  # fmt: skip


def claim(cid: str, ctype: str, text: str, quote: str, page: str) -> Claim:
    return Claim(id=cid, type=ctype, text=text, source_url=page, quote=quote)  # type: ignore[arg-type]


def llm_saying(real_alive: RealAlive, trust: Trust) -> FakeLLM:
    def respond(user: str) -> JudgeOut:
        return JudgeOut(
            real_alive=real_alive,
            trust=trust,
            confidence=0.8,
            summary="s",
            reason_fact_ids=[],
            finding_fact_ids=[],
        )

    return FakeLLM({JudgeOut: respond})


JUDGES = {
    "rules only": FakeLLM(),  # no responder: the LLM judge fails, rules decide
    "optimistic LLM": llm_saying(RealAlive.YES, Trust.TRUST),
    "pessimistic LLM": llm_saying(RealAlive.NO, Trust.DO_NOT_TRUST),
}


# ---- hollow: a polished site with nothing behind it -------------------------------------


def hollow() -> tuple[list[Claim], list[Fact]]:
    site = "https://hollow.example"
    claims = [
        claim("c1", "founded_year", "Vendor says it was founded in 2015",
              "Trusted since 2015 by teams worldwide", f"{site}/about"),
        claim("c2", "customer", "Vendor names Globex as a customer", "Globex runs on Hollow",
              f"{site}/customers"),
        claim("c3", "customer", "Vendor names Initech as a customer", "Initech runs on Hollow",
              f"{site}/customers"),
        claim("c4", "customer", "Vendor names Umbrella as a customer", "Umbrella runs on Hollow",
              f"{site}/customers"),
    ]  # fmt: skip
    facts = [
        *age_records("hollow.example", NOW - timedelta(weeks=6), (Polarity.NEGATIVE, 0.5)),
        fact("age-3", "age", Axis.REAL_ALIVE, Polarity.NEUTRAL, 0.1, topic="capture_history",
             observed_at=datetime(2026, 1, 1, tzinfo=UTC), quote=""),
        fact("site-2", "site_reader", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.15,
             topic="missing_page", source_kind=SourceKind.VENDOR, quote="",
             source_url=f"{site}/help/getting-started"),
        fact("looks-2", "looks", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.1, topic="dead_help",
             source_kind=SourceKind.VENDOR, is_filler=True, quote=""),
    ]  # fmt: skip
    # no legal_id facts: no register entry; no footprint facts: no independent mentions
    return claims, facts


def test_hollow_claims_check_records_the_founding_gap_and_unconfirmed_customers() -> None:
    claims, facts = hollow()
    checks, new = check_claims(claims, facts, "hollow.example", {"hollow"}, NOW)
    assert [f.topic for f in new] == ["founding_year_gap"]
    assert [c.status for c in checks] == ["unconfirmed"] * 4  # customers: no independent mention


@pytest.mark.parametrize("name", JUDGES)
async def test_hollow_vendor_is_no_and_do_not_trust(name: str) -> None:
    claims, facts = hollow()
    checks, new = check_claims(claims, facts, "hollow.example", {"hollow"}, NOW)
    result = await judge(JUDGES[name], claims, facts + new, checks)
    assert (result.real_alive, result.trust) == (RealAlive.NO, Trust.DO_NOT_TRUST)
    assert any("hollow pattern" in n for n in result.notes)


# ---- real but tiny: new, small, honest ------------------------------------------------------


def tiny() -> tuple[list[Claim], list[Fact]]:
    site = "https://tiny.example"
    claims = [
        claim("c1", "founded_year", "Vendor says it was founded in 2026",
              "We started building Tiny in spring 2026", f"{site}/about"),
        claim("c2", "register_number", "Vendor gives HRB 999999", "HRB 999999",
              f"{site}/impressum"),
    ]  # fmt: skip
    facts = [
        *age_records("tiny.example", NOW - timedelta(days=150), (Polarity.NEGATIVE, 0.5)),
        fact("footprint-0", "footprint", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.3,
             topic="news", source_kind=SourceKind.INDEPENDENT, source_type="social",
             source_url="https://www.producthunt.com/products/tiny"),
        fact("alive-0", "alive", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.2,
             topic="release_activity", source_kind=SourceKind.VENDOR,
             observed_at=NOW - timedelta(days=9), source_url=f"{site}/changelog"),
        fact("legal_id-9", "legal_id", Axis.TRUST, Polarity.POSITIVE, 0.45,
             topic="register_record", quote="",
             source_url="https://www.northdata.de/Tiny%20GmbH/HRB%20999999"),
        fact("legal_id-12", "legal_id", Axis.REAL_ALIVE, Polarity.NEUTRAL, 0.2,
             topic="register_founded", observed_at=datetime(2026, 1, 1, tzinfo=UTC)),
    ]  # fmt: skip
    return claims, facts


@pytest.mark.parametrize("name", JUDGES)
async def test_real_but_tiny_is_never_no_or_do_not_trust(name: str) -> None:
    claims, facts = tiny()
    checks, new = check_claims(claims, facts, "tiny.example", {"tiny"}, NOW)
    assert new == []  # no contradictions: the register confirms the founding year
    result = await judge(JUDGES[name], claims, facts + new, checks)
    assert result.real_alive in (RealAlive.YES, RealAlive.UNCLEAR)
    assert result.trust == Trust.CAUTION


def test_young_domain_alone_is_one_signal_not_three() -> None:
    _, facts = tiny()
    rules = apply_rules(facts, today=NOW)
    assert rules.scores["real_alive_neg"] == 0.5  # not 1.5
    assert rules.scores["real_alive_negative_signals"] == 1
    assert RealAlive.NO not in rules.allowed_real_alive


# ---- dead product: old, and the vendor says it is shut down ----------------------------------


def dead() -> tuple[list[Claim], list[Fact]]:
    site = "https://dead.example"
    facts = [
        *age_records("dead.example", datetime(2009, 3, 1, tzinfo=UTC), (Polarity.POSITIVE, 0.5)),
        fact("risk-10", "risk", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.9, topic="discontinued",
             is_hard_flag=True, source_kind=SourceKind.VENDOR, source_type="vendor",
             source_url=f"{site}/blog/goodbye", quote="Dead will shut down on 1 March 2026"),
        fact("alive-0", "alive", Axis.REAL_ALIVE, Polarity.NEGATIVE, 0.35, topic="frozen",
             source_kind=SourceKind.VENDOR, observed_at=NOW - timedelta(days=3 * 365),
             source_url=f"{site}/changelog", quote="2023-11-20"),
        fact("footprint-0", "footprint", Axis.REAL_ALIVE, Polarity.POSITIVE, 0.4,
             topic="reviews", source_kind=SourceKind.INDEPENDENT, source_type="review",
             source_url="https://www.g2.com/products/dead/reviews"),
    ]  # fmt: skip
    return [], facts


@pytest.mark.parametrize("name", JUDGES)
async def test_dead_product_is_no(name: str) -> None:
    claims, facts = dead()
    result = await judge(JUDGES[name], claims, facts)
    assert result.real_alive == RealAlive.NO
    assert any("shutdown" in n for n in result.notes)


# ---- the hollow pattern must not catch honest vendors ------------------------------------------


def test_new_domain_with_any_positive_evidence_is_not_hollow() -> None:
    claims, facts = hollow()
    checks, new = check_claims(claims, facts, "hollow.example", {"hollow"}, NOW)
    review = fact(
        "footprint-0",
        "footprint",
        Axis.REAL_ALIVE,
        Polarity.POSITIVE,
        0.3,
        source_kind=SourceKind.INDEPENDENT,
        source_url="https://www.g2.com/x",
    )
    rules = apply_rules([*facts, *new, review], today=NOW)
    assert rules.scores["hollow"] == 0.0
    assert Trust.DO_NOT_TRUST not in rules.allowed_trust  # no red flag: never do_not_trust
