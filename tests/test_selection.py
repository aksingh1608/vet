"""Reason and finding selection (verify.select_reasons / select_findings): pure, no network."""

from datetime import UTC, datetime

from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.verify import merge_age, select_findings, select_reasons

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def fact(fid: str, collector: str, axis: Axis = Axis.REAL_ALIVE, **kw: object) -> Fact:
    data: dict[str, object] = {
        "id": fid,
        "collector": collector,
        "statement": f"statement {fid}",
        "axis": axis,
        "polarity": Polarity.POSITIVE,
        "weight": 0.5,
        "source_url": f"https://{collector}.example/{fid}",
        "quote": "q",
        "source_kind": SourceKind.INDEPENDENT,
        "fetched_at": NOW,
    }
    data.update(kw)
    return Fact.model_validate(data)


def age_facts() -> list[Fact]:
    return [
        fact(
            "age-0",
            "age",
            topic="domain_registered",
            observed_at=datetime(1994, 7, 30, tzinfo=UTC),
            source_url="https://lookup.icann.org/en/lookup?name=basecamp.com",
            source_kind=SourceKind.RECORD,
        ),
        fact(
            "age-1",
            "age",
            topic="first_certificate",
            observed_at=datetime(2012, 1, 30, tzinfo=UTC),
            source_url="https://crt.sh/?q=basecamp.com",
            source_kind=SourceKind.RECORD,
        ),
        fact(
            "age-2",
            "age",
            topic="first_capture",
            observed_at=datetime(1996, 10, 29, tzinfo=UTC),
            source_url="https://web.archive.org/web/19961029204828/basecamp.com/",
            quote="",
            source_kind=SourceKind.RECORD,
        ),
        fact(
            "age-3",
            "age",
            topic="capture_history",
            statement="The Wayback Machine has captures of basecamp.com in 31 of the 31 years "
            "from 1996 to 2026; captured steadily up to now",
            source_url="https://web.archive.org/web/*/basecamp.com",
            quote="",
            source_kind=SourceKind.RECORD,
        ),
    ]


def ids(reasons) -> list[str]:  # noqa: ANN001
    return [r.fact_id for r in reasons]


def test_age_records_merge_into_one_reason_linked_to_the_strongest() -> None:
    reason, rest = merge_age(age_facts(), "basecamp.com")
    assert reason.text == (
        "Domain registered 1994-07-30; archived in 31 of the 31 years from 1996 to 2026; "
        "first certificate 2012-01-30 (basecamp.com)"
    )
    assert reason.source_url == "https://lookup.icann.org/en/lookup?name=basecamp.com"
    assert [f.id for f in rest] == ["age-1", "age-2", "age-3"]


def test_without_rdap_the_snapshot_is_the_link() -> None:
    reason, _ = merge_age(age_facts()[1:3], "projektron.de")
    assert reason.source_url.startswith("https://web.archive.org/web/1996")
    assert reason.text == "First archived 1996-10-29; first certificate 2012-01-30 (projektron.de)"


def test_at_most_one_age_reason_and_one_per_collector() -> None:
    ranked = [
        *age_facts(),
        fact("fp-0", "footprint"),
        fact("fp-1", "footprint"),
        fact("legal-0", "legal_id", Axis.TRUST, source_kind=SourceKind.RECORD),
        fact("risk-0", "risk", Axis.TRUST),
        fact("alive-0", "alive", source_kind=SourceKind.RECORD),
    ]
    reasons, _ = select_reasons(ranked, {f.id for f in ranked}, "basecamp.com")
    assert ids(reasons) == ["age-0", "fp-0", "legal-0", "risk-0", "alive-0"]


def test_diversity_relaxed_when_fewer_than_four_collectors() -> None:
    ranked = [
        *age_facts(),
        fact("fp-0", "footprint"),
        fact("fp-1", "footprint"),
        fact("fp-2", "footprint", Axis.TRUST),
    ]
    reasons, notes = select_reasons(ranked, {f.id for f in ranked}, "x.example")
    assert ids(reasons) == ["age-0", "fp-0", "fp-1", "fp-2"]
    assert sum(1 for r in reasons if r.fact_id.startswith("age")) == 1  # still one age reason
    assert any("diversity relaxed" in n for n in notes)


def test_contradicted_claims_always_lead() -> None:
    contradiction = fact(
        "claims_check-0",
        "claims_check",
        Axis.TRUST,
        polarity=Polarity.NEGATIVE,
        topic="contradicted_claim",
        statement="The site says the company was founded in 2024; the domain was registered "
        "on 2025-11-22",
        source_kind=SourceKind.VENDOR,
    )
    second = contradiction.model_copy(update={"id": "claims_check-1", "statement": "other"})
    ranked = [*age_facts(), fact("fp-0", "footprint"), contradiction, second]
    reasons, _ = select_reasons(ranked, {f.id for f in ranked}, "x.example")
    # both contradictions, before everything else, despite the same collector
    assert ids(reasons)[:2] == ["claims_check-0", "claims_check-1"]


def test_axis_coverage_pulls_in_a_trust_reason() -> None:
    ranked = [
        *age_facts(),
        *[fact(f"c{i}-0", f"c{i}") for i in range(7)],
        fact("legal-0", "legal_id", Axis.TRUST),
    ]
    reasons, notes = select_reasons(ranked, {f.id for f in ranked}, "x.example")
    assert len(reasons) == 7
    assert any(r.axis == Axis.TRUST for r in reasons)
    assert any(r.axis == Axis.REAL_ALIVE for r in reasons)
    assert any("cover trust" in n for n in notes)


def test_filler_only_when_nothing_better() -> None:
    copyright_ = fact("site-1", "site_reader", is_filler=True, source_kind=SourceKind.VENDOR)
    directory = fact("fp-9", "footprint", is_filler=True)
    rich = [
        copyright_,
        directory,
        *age_facts(),
        *[fact(f"c{i}-0", f"c{i}", Axis.TRUST if i == 0 else Axis.REAL_ALIVE) for i in range(4)],
    ]
    reasons, _ = select_reasons(rich, {f.id for f in rich}, "x.example")
    assert "site-1" not in ids(reasons) and "fp-9" not in ids(reasons)

    thin = [copyright_, *age_facts(), fact("legal-0", "legal_id", Axis.TRUST)]
    reasons, notes = select_reasons(thin, {f.id for f in thin}, "x.example")
    assert "site-1" in ids(reasons)
    assert any("filler used" in n for n in notes)


def test_unverified_facts_are_never_reasons() -> None:
    ranked = [*age_facts(), fact("fp-0", "footprint"), fact("risk-0", "risk", Axis.TRUST)]
    reasons, _ = select_reasons(ranked, {"age-2", "fp-0"}, "x.example")
    assert set(ids(reasons)) == {"age-2", "fp-0"}


def neg(fid: str, collector: str, topic: str, url: str, **kw: object) -> Fact:
    return fact(
        fid, collector, Axis.TRUST, polarity=Polarity.NEGATIVE, topic=topic, source_url=url, **kw
    )


def test_one_source_is_enough_only_for_records_press_and_vendor_negatives() -> None:
    from vet.verify import one_source_enough

    hibp = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
               source_kind=SourceKind.RECORD)  # fmt: skip
    press = neg("risk-10", "risk", "lawsuit", "https://www.reuters.com/a", source_type="press")
    shutdown = neg("risk-11", "risk", "discontinued", "https://x.example/goodbye",
                   source_kind=SourceKind.VENDOR, source_type="vendor")  # fmt: skip
    linkedin = neg("fp-7", "footprint", "layoffs", "https://www.linkedin.com/posts/a",
                   source_type="social")  # fmt: skip
    blog = neg("fp-8", "footprint", "fine", "https://someblog.example/x", source_type="blog")
    assert [one_source_enough(f) for f in (hibp, press, shutdown, linkedin, blog)] == [
        True, True, True, False, False,
    ]  # fmt: skip


def test_single_source_social_and_blogs_need_a_second_site() -> None:
    linkedin = neg("fp-7", "footprint", "layoffs", "https://www.linkedin.com/posts/a",
                   source_type="social")  # fmt: skip
    blog = neg("fp-8", "footprint", "fine", "https://someblog.example/x", source_type="blog")
    blog_too = neg("risk-12", "risk", "unconfirmed_fine", "https://other.example/y",
                   source_type="blog")  # fmt: skip
    unverified = neg("risk-13", "risk", "insolvency", "https://www.ft.com/z", source_type="press")
    facts = {f.id: f for f in (linkedin, blog, blog_too, unverified)}
    findings, notes = select_findings(list(facts), facts, set(facts) - {"risk-13"})
    assert {f.fact_id for f in findings} == {"fp-8", "risk-12"}  # two blogs corroborate
    assert any("fp-7" in n and "second independent source" in n for n in notes)
    assert any("risk-13" in n for n in notes)


def test_findings_are_capped_at_three_strongest_first_contradictions_always() -> None:
    contradiction = neg(
        "claims_check-0",
        "claims_check",
        "contradicted_claim",
        "https://x.example/about",
        source_kind=SourceKind.VENDOR,
        weight=0.3,
    )
    breach = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
                 source_kind=SourceKind.RECORD, is_hard_flag=True, weight=0.8)  # fmt: skip
    lawsuit = neg("risk-10", "risk", "lawsuit", "https://www.reuters.com/a",
                  source_type="press", weight=0.4)  # fmt: skip
    strong_review = fact("fp-1", "footprint", weight=0.6, source_type="press",
                         source_url="https://www.heise.de/a")  # fmt: skip
    weak = fact("fp-2", "footprint", weight=0.2, source_url="https://other.example/b")
    facts = {f.id: f for f in (weak, strong_review, lawsuit, breach, contradiction)}
    findings, _ = select_findings([], facts, set(facts))
    # contradiction first despite its low weight; then hard flag; then real negative; max 3
    assert [f.fact_id for f in findings] == ["claims_check-0", "risk-0", "risk-10"]


def test_findings_never_repeat_a_reason() -> None:
    contradiction = neg(
        "claims_check-0", "claims_check", "contradicted_claim", "https://x.example/about",
        source_kind=SourceKind.VENDOR,
    )  # fmt: skip
    other = neg(
        "claims_check-1", "claims_check", "founding_year_gap", "https://x.example/team",
        source_kind=SourceKind.VENDOR,
    )  # fmt: skip
    facts = {f.id: f for f in (contradiction, other)}
    findings, _ = select_findings(list(facts), facts, set(facts), frozenset({"claims_check-0"}))
    assert [f.fact_id for f in findings] == ["claims_check-1"]


def test_finding_only_facts_are_never_reasons() -> None:
    old_breach = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
                     source_kind=SourceKind.RECORD)  # fmt: skip
    ranked = [old_breach, *age_facts(), fact("fp-0", "footprint"), fact("legal-0", "legal_id")]
    reasons, _ = select_reasons(ranked, {f.id for f in ranked}, "x.example", frozenset({"risk-0"}))
    assert "risk-0" not in ids(reasons)


def test_finding_only_facts_are_still_verified() -> None:
    from vet.verify import candidate_pool

    old_breach = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
                     source_kind=SourceKind.RECORD)  # fmt: skip
    ranked = [*age_facts(), fact("fp-0", "footprint")]  # the reason ranking leaves it out
    everything = {f.id: f for f in [*ranked, old_breach]}
    pool = candidate_pool(ranked, ["risk-0"], 24, everything)
    assert "risk-0" in {f.id for f in pool}


def test_two_reports_of_the_same_event_are_one_finding() -> None:
    from datetime import UTC, datetime

    when = datetime(2019, 5, 24, tzinfo=UTC)
    hibp = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
               source_kind=SourceKind.RECORD, observed_at=when)  # fmt: skip
    notice = neg(
        "risk-11",
        "risk",
        "breach",
        "https://x.example/incident",
        source_kind=SourceKind.VENDOR,
        observed_at=when.replace(month=1, day=1),
    )
    facts = {f.id: f for f in (hibp, notice)}
    findings, notes = select_findings(list(facts), facts, set(facts))
    assert [f.fact_id for f in findings] == ["risk-0"]
    assert any("same event" in n for n in notes)


def test_record_publishers_and_social_sites_are_typed_by_domain() -> None:
    from vet.sources import source_type_for

    assert source_type_for("https://haveibeenpwned.com/Breach/Canva", "other") == "record"
    assert source_type_for("https://www.northdata.de/X", "") == "record"
    assert source_type_for("https://www.youtube.com/watch?v=1", "press") == "social"
    assert source_type_for("https://www.reuters.com/a", "press") == "press"
    assert source_type_for("https://www.reuters.com/a", "nonsense") == "other"


def test_finding_only_breach_becomes_the_trust_reason_when_nothing_else_covers_trust() -> None:
    from vet.verify import breach_reason

    hibp = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
               source_kind=SourceKind.RECORD, statement="Have I Been Pwned lists a breach of X "
               "dated 2019-05-24 affecting 1,000 accounts")  # fmt: skip
    notice = neg("risk-11", "risk", "breach", "https://x.example/incident",
                 source_kind=SourceKind.VENDOR, tags=["disclosed"])  # fmt: skip
    ranked = [*age_facts(), fact("fp-0", "footprint"), fact("fp-1", "c2")]
    facts = {f.id: f for f in [*ranked, hibp, notice]}
    only = frozenset({"risk-0", "risk-11"})

    reason = breach_reason(only, facts, set(facts))
    assert reason is not None and reason.fact_id == "risk-0"  # the record, not the notice
    assert reason.text.endswith("; the vendor disclosed it (x.example)")
    reasons, notes = select_reasons(ranked, set(facts), "x.example", only, [reason])
    assert reasons[-1].fact_id == "risk-0" and reasons[-1].axis == Axis.TRUST
    assert any("used as the trust reason" in n for n in notes)

    # disclosure is only mentioned when the disclosure source itself verified
    unverified = breach_reason(only, facts, set(facts) - {"risk-11"})
    assert unverified is not None and "disclosed" not in unverified.text

    # and the breach is then never a finding as well: neither the record nor the notice
    from vet.verify import used_by_reasons

    used = used_by_reasons(reasons, facts, set(facts), only)
    assert {"risk-0", "risk-11"} <= used
    findings, _ = select_findings(["risk-0", "risk-11"], facts, set(facts), used)
    assert not {"risk-0", "risk-11"} & {f.fact_id for f in findings}


def test_two_reports_of_the_same_event_are_one_reason() -> None:
    from datetime import UTC, datetime

    when = datetime(2019, 5, 24, tzinfo=UTC)
    record = neg("risk-0", "risk", "breach", "https://haveibeenpwned.com/PwnedWebsites#X",
                 source_kind=SourceKind.RECORD, observed_at=when)  # fmt: skip
    page = neg("risk-10", "risk", "breach", "https://haveibeenpwned.com/Breach/X",
               source_kind=SourceKind.RECORD, observed_at=when.replace(month=1, day=1))  # fmt: skip
    ranked = [record, page, *age_facts(), fact("fp-0", "footprint")]  # 3 collectors: relaxed
    reasons, _ = select_reasons(ranked, {f.id for f in ranked}, "x.example")
    assert [r.fact_id for r in reasons].count("risk-10") == 0


# ---- trust reason fallback chain ------------------------------------------------------------


def personio_shaped() -> list[Fact]:
    """The live Personio run: 11 years of records and independent coverage, but every fact is
    on the real_alive axis, so no trust fact exists for axis coverage."""
    reg = datetime(2015, 9, 18, tzinfo=UTC)
    return [
        fact("age-0", "age", topic="first_capture", observed_at=reg, quote="",
             source_url="https://web.archive.org/web/20150918012512/personio.de/",
             source_kind=SourceKind.RECORD),
        fact("age-1", "age", topic="first_certificate",
             observed_at=datetime(2015, 9, 23, tzinfo=UTC),
             source_url="https://crt.sh/?q=personio.de", source_kind=SourceKind.RECORD),
        fact("fp-0", "footprint", polarity=Polarity.NEUTRAL, topic="other", source_type="other",
             source_url="https://en.wikipedia.org/wiki/Personio"),
        fact("fp-1", "footprint", polarity=Polarity.NEUTRAL, topic="news", source_type="press",
             source_url="https://www.siliconrepublic.com/start-ups/personio-acquisition-back"),
        fact("fp-2", "footprint", topic="reviews", source_type="review", weight=0.3,
             source_url="https://www.softwareadvice.com/hr/personio-profile/reviews",
             quote="Personio keeps developing its software and adding new functions"),
        fact("fp-3", "footprint", topic="reviews", source_type="review", weight=0.2,
             statement="Trustpilot: 89 reviews", code_worded=True,
             source_url="https://www.trustpilot.com/review/personio.de",
             quote="89 reviews • Great"),
    ]  # fmt: skip


def chain_reasons(facts: list[Fact]) -> tuple[list, list[str]]:  # type: ignore[type-arg]
    from vet.verify import trust_reason_chain

    passed = {f.id for f in facts}
    chain = trust_reason_chain({f.id: f for f in facts}, passed)
    return select_reasons(facts, passed, "personio.de", frozenset(), chain)


def test_personio_shape_gets_a_trust_reason_from_the_review_platform() -> None:
    reasons, notes = chain_reasons(personio_shaped())
    trust = [r for r in reasons if r.axis == Axis.TRUST]
    assert len(trust) == 1
    assert trust[0].text == "Trustpilot: 89 reviews"
    assert trust[0].source_url == "https://www.trustpilot.com/review/personio.de"
    assert any("used as the trust reason" in n for n in notes)


def test_register_record_comes_before_review_platform() -> None:
    register = fact("legal-0", "legal_id", topic="register", source_kind=SourceKind.RECORD,
                    statement="Handelsregister: Personio SE & Co. KG, HRA 112345",
                    source_url="https://www.northdata.de/Personio")  # fmt: skip
    reasons, _ = chain_reasons([*personio_shaped(), register])
    (trust,) = [r for r in reasons if r.axis == Axis.TRUST]
    assert trust.fact_id == "legal-0"


def test_press_mention_when_no_register_or_counted_reviews() -> None:
    facts = [f for f in personio_shaped() if f.id != "fp-3"]
    reasons, _ = chain_reasons(facts)
    (trust,) = [r for r in reasons if r.axis == Axis.TRUST]
    assert trust.fact_id == "fp-1"


def test_evidence_floor_sentence_when_nothing_else_exists() -> None:
    facts = [*age_facts()[:3]]
    reasons, _ = chain_reasons(facts)
    (trust,) = [r for r in reasons if r.axis == Axis.TRUST]
    assert trust.text == "Domain registered 1994-07-30, under 33 years of history"
    assert trust.source_url == "https://lookup.icann.org/en/lookup?name=basecamp.com"
    assert trust.polarity == Polarity.NEUTRAL


def test_negative_or_unverified_facts_are_never_trust_fallbacks() -> None:
    from vet.verify import trust_reason_chain

    facts = personio_shaped()
    facts[-1] = facts[-1].model_copy(update={"polarity": Polarity.NEGATIVE})
    by_id = {f.id: f for f in facts}
    chain = trust_reason_chain(by_id, {f.id for f in facts} - {"fp-1"})
    assert [r.fact_id for r in chain] == ["age-0"]  # record sentence only: the first capture
    assert chain[0].text == "First domain record 2015-09-18, under 12 years of history"


def test_evidence_floor_dates_from_real_records_not_the_yearly_capture_history() -> None:
    reg = datetime(2025, 11, 22, tzinfo=UTC)
    facts = [
        fact("age-0", "age", topic="domain_registered", observed_at=reg,
             source_url="https://lookup.icann.org/en/lookup?name=x.example",
             source_kind=SourceKind.RECORD, fetched_at=datetime(2026, 11, 22, tzinfo=UTC)),
        fact("age-3", "age", topic="capture_history", observed_at=datetime(2025, 1, 1, tzinfo=UTC),
             source_kind=SourceKind.RECORD, fetched_at=datetime(2026, 11, 22, tzinfo=UTC)),
    ]  # fmt: skip
    reasons, _ = chain_reasons(facts)
    (trust,) = [r for r in reasons if r.axis == Axis.TRUST]
    assert trust.text == "Domain registered 2025-11-22, under 2 years of history"
    assert "source" not in trust.text and "no " not in trust.text
