"""Buyer-view label rule: scenario tests with invented companies. The model's categories are
given; code keeps, downgrades or drops them and decides the label."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import FakeLLM
from tests.test_schemas import make_verdict
from vet.buyer_view import baseline_view
from vet.categorise import (
    _CategoriseOut,
    _Category,
    _Owner,
    _ReasoningOut,
    apply_offsets,
    base_is_met,
    build_flags,
    categorised_view,
    decide,
    keep_categories,
    write_reasoning,
)
from vet.collectors.base import SiteIdentity
from vet.config import Settings
from vet.schemas import Fact, FlagColour, Overall, Polarity, RealAlive, SourceKind, Trust
from vet.verify import VerifyResult

TODAY = datetime(2026, 11, 27, tzinfo=UTC)


def fact(
    fid: str,
    *,
    url: str = "https://news.example-press.com/a",
    source_type: str = "press",
    kind: SourceKind = SourceKind.INDEPENDENT,
    topic: str = "",
    tags: list[str] | None = None,
    when: datetime | None = None,
) -> Fact:
    return Fact(
        id=fid,
        collector="corporate",
        statement=f"Statement {fid}",
        axis="trust",
        polarity=Polarity.NEGATIVE,
        weight=0.0,
        source_url=url,
        quote="quote",
        source_kind=kind,
        fetched_at=TODAY,
        topic=topic,
        source_type=source_type,
        tags=tags or [],
        observed_at=when,
    )


def label(
    facts: list[Fact],
    cats: list[_Category],
    *,
    failed: frozenset[str] = frozenset(),
    identifiable: bool = True,
    operating: bool = True,
    settings: Settings | None = None,
) -> tuple[Overall, list[str]]:
    settings = settings or Settings()
    by_id = {f.id: f for f in facts}
    results = {f.id: VerifyResult(fact_id=f.id, source_ok=f.id not in failed) for f in facts}
    out = _CategoriseOut(identifiable_vendor=identifiable, operating_now=operating, categories=cats)
    notes: list[str] = []
    kept = apply_offsets(keep_categories(out, by_id, results, settings, TODAY, notes), notes)
    verdict = make_verdict().model_copy(update={"real_alive": RealAlive.YES})
    return decide(kept, base_is_met(out, kept, verdict))


def cat(name: str, *ids: str, scope: str = "product", owners: list[_Owner] | None = None):
    return _Category(category=name, fact_ids=list(ids), scope=scope, owners=owners or [])


ADOPTION = [
    fact("g1", url="https://reviews-one.example/p", source_type="review"),
    fact("g2", url="https://reviews-two.example/p", source_type="review"),
]
GREEN_CATS = [cat("wide_adoption", "g1", "g2")]


def test_regulator_order_on_core_promise_is_red() -> None:
    facts = [
        fact(
            "r1",
            url="https://regulator.example.gov/order",
            source_type="regulator",
            when=TODAY - timedelta(days=400),
        )
    ]
    assert label(facts, [cat("regulator_finding_core", "r1")])[0] == Overall.RED


def test_lawsuit_only_filed_stays_context() -> None:
    facts = [*ADOPTION, fact("l1", topic="complaint_or_lawsuit")]
    overall, names = label(facts, [*GREEN_CATS, cat("lawsuit_filed", "l1")])
    assert overall == Overall.GREEN and "lawsuit_filed" in names


def test_unrelated_fine_against_large_vendor_stays_context() -> None:
    facts = [*ADOPTION, fact("f1", source_type="regulator")]
    overall, names = label(
        facts, [*GREEN_CATS, cat("regulator_finding_core", "f1", scope="parent")]
    )
    assert overall == Overall.GREEN and "unrelated_fine_or_lawsuit" in names


def test_old_regulator_finding_is_context() -> None:
    facts = [*ADOPTION, fact("f1", source_type="regulator", when=TODAY - timedelta(days=365 * 9))]
    assert label(facts, [*GREEN_CATS, cat("regulator_finding_core", "f1")])[0] == Overall.GREEN


def test_announced_end_of_life_is_red_from_a_vendor_statement() -> None:
    facts = [
        fact(
            "e1",
            url="https://acme-tools.example/eol",
            source_type="vendor",
            kind=SourceKind.VENDOR,
            tags=["statement_against_interest"],
        )
    ]
    assert label(facts, [cat("discontinued", "e1")])[0] == Overall.RED


def test_four_owners_plus_non_core_is_yellow() -> None:
    facts = [*ADOPTION, *(fact(f"o{i}") for i in range(4))]
    owners = [_Owner(name=f"Owner {i}", year=str(2010 + i), fact_id=f"o{i}") for i in range(4)]
    cats = [*GREEN_CATS, cat("ownership_churn", "o0", "o1", "o2", "o3", owners=owners)]
    assert label(facts, cats)[0] == Overall.YELLOW


def test_one_strategic_acquisition_stays_green() -> None:
    facts = [*ADOPTION, fact("o1")]
    owners = [_Owner(name="Big Parent", year="2021", fact_id="o1")]
    cats = [*GREEN_CATS, cat("ownership_churn", "o1", owners=owners)]
    overall, names = label(facts, cats)
    assert overall == Overall.GREEN and "pe_owned_or_carve_out" in names


def test_legacy_page_is_yellow() -> None:
    facts = [
        *ADOPTION,
        fact(
            "v1", source_type="vendor", kind=SourceKind.VENDOR, tags=["statement_against_interest"]
        ),
    ]
    assert label(facts, [*GREEN_CATS, cat("legacy_or_superseded", "v1")])[0] == Overall.YELLOW


def test_established_single_maintainer_stays_green() -> None:
    facts = [
        *ADOPTION,
        fact("m1", source_type="record"),
        fact("t1", source_type="record"),
        fact("a1", source_type="record"),
    ]
    cats = [
        *GREEN_CATS,
        cat("continuity_small_vendor", "m1"),
        cat("long_track_record", "t1"),
        cat("active_releases", "a1"),
    ]
    overall, names = label(facts, cats)
    assert overall == Overall.GREEN and "single_maintainer_established" in names


def test_single_maintainer_with_recent_compromise_is_yellow() -> None:
    facts = [*ADOPTION, fact("i1", when=TODAY - timedelta(days=60)), fact("m1")]
    cats = [*GREEN_CATS, cat("continuity_small_vendor", "m1"), cat("recent_incident_quiet", "i1")]
    assert label(facts, cats)[0] == Overall.YELLOW


def test_incident_with_open_write_up_stays_green() -> None:
    facts = [*ADOPTION, fact("i1", when=TODAY - timedelta(days=60)), fact("w1")]
    cats = [
        *GREEN_CATS,
        cat("recent_incident_quiet", "i1"),
        cat("transparent_incident_response", "w1"),
    ]
    assert label(facts, cats)[0] == Overall.GREEN


def test_quiet_recent_incident_is_yellow() -> None:
    facts = [*ADOPTION, fact("i1", when=TODAY - timedelta(days=90))]
    assert label(facts, [*GREEN_CATS, cat("recent_incident_quiet", "i1")])[0] == Overall.YELLOW


def test_no_entity_plus_implausible_traction_is_red() -> None:
    page = "https://acme-tools.example/about"
    facts = [
        fact("n1", url=page, source_type="vendor", kind=SourceKind.VENDOR, topic="no_legal_entity"),
        fact(
            "t1",
            url=page,
            source_type="vendor",
            kind=SourceKind.VENDOR,
            topic="traction_inconsistent",
        ),
    ]
    assert label(facts, [cat("no_entity_plus_claims", "n1", "t1")])[0] == Overall.RED


def test_identity_topics_are_proposed_by_code() -> None:
    from vet.categorise import proposed_by_code

    out = _CategoriseOut(identifiable_vendor=True, operating_now=True, categories=[])
    alone = proposed_by_code(out, [fact("n1", topic="no_legal_entity")])
    assert [c.category for c in alone] == ["no_legal_entity_alone"]
    both = proposed_by_code(
        out,
        [
            fact("n1", topic="no_legal_entity"),
            fact("w1", topic="logo_wall_not_customers"),
            fact("c1", topic="coming_soon"),
        ],
    )
    assert {c.category for c in both} == {
        "no_entity_plus_claims",
        "misleading_social_proof",
        "weak_trust_artefacts",
    }


def test_logo_wall_of_badges_on_the_vendor_page_is_red() -> None:
    facts = [
        fact(
            "w1",
            url="https://acme-tools.example/",
            source_type="vendor",
            kind=SourceKind.VENDOR,
            topic="logo_wall_not_customers",
        )
    ]
    assert label(facts, [cat("misleading_social_proof", "w1")])[0] == Overall.RED


def test_no_entity_alone_is_not_red() -> None:
    facts = [
        *ADOPTION,
        fact("n1", url="https://acme-tools.example/", source_type="vendor", kind=SourceKind.VENDOR),
    ]
    overall, names = label(facts, [*GREEN_CATS, cat("no_entity_plus_claims", "n1")])
    assert overall != Overall.RED and "no_legal_entity_alone" in names


def test_dated_site_with_strong_adoption_stays_green() -> None:
    facts = [*ADOPTION, fact("d1", source_type="vendor", kind=SourceKind.VENDOR)]
    assert label(facts, [*GREEN_CATS, cat("dated_website", "d1")])[0] == Overall.GREEN


def test_base_not_met_is_yellow_not_enough_evidence() -> None:
    overall, names = label([], [])
    assert overall == Overall.YELLOW and "not_enough_evidence" in names


def test_cited_fact_that_does_not_exist_drops_the_category() -> None:
    assert label(ADOPTION, [*GREEN_CATS, cat("discontinued", "ghost-1")])[0] == Overall.GREEN


def test_failed_verification_drops_the_category() -> None:
    facts = [*ADOPTION, fact("r1", source_type="regulator")]
    cats = [*GREEN_CATS, cat("regulator_finding_core", "r1")]
    assert label(facts, cats, failed=frozenset({"r1"}))[0] == Overall.GREEN


def test_five_reprints_of_one_release_do_not_make_wide_adoption() -> None:
    facts = [
        fact(f"p{i}", url=f"https://wire-{i}.example/r", tags=["vendor_origin"]) for i in range(5)
    ]
    overall, names = label(facts, [cat("wide_adoption", *(f"p{i}" for i in range(5)))])
    assert overall == Overall.YELLOW and "wide_adoption" not in names


def test_soc2_badge_alone_is_not_a_green_signal() -> None:
    facts = [
        fact(
            "b1",
            url="https://acme-tools.example/trust",
            source_type="vendor",
            kind=SourceKind.VENDOR,
        )
    ]
    overall, names = label(facts, [cat("independent_audit_or_analyst", "b1")])
    assert overall == Overall.YELLOW and "independent_audit_or_analyst" not in names


def test_unsourced_logos_alone_are_no_flag() -> None:
    # The model may not invent a logo category; an unknown category is dropped.
    overall, names = label(ADOPTION, [*GREEN_CATS, cat("customer_logos_unsourced", "g1")])
    assert overall == Overall.GREEN and "customer_logos_unsourced" not in names


def test_domain_dates_alone_earn_no_track_record() -> None:
    facts = [
        fact("d1", source_type="record", kind=SourceKind.RECORD, topic="domain_registered"),
        fact("d2", source_type="record", kind=SourceKind.RECORD, topic="first_capture"),
    ]
    overall, names = label(facts, [cat("long_track_record", "d1", "d2")])
    assert overall == Overall.YELLOW and "long_track_record" not in names


def test_old_domain_with_a_different_early_site_earns_no_track_record() -> None:
    facts = [
        fact("w1", source_type="record", kind=SourceKind.RECORD, topic="domain_older_than_vendor")
    ]
    assert "long_track_record" not in label(facts, [cat("long_track_record", "w1")])[1]


def test_new_domain_with_old_register_entry_and_adoption_stays_green() -> None:
    facts = [
        *ADOPTION,
        fact(
            "reg",
            source_type="record",
            kind=SourceKind.RECORD,
            topic="register_entry",
            when=datetime(2005, 1, 1, tzinfo=UTC),
        ),
    ]
    cats = [*GREEN_CATS, cat("long_track_record", "reg"), cat("registered_entity", "reg")]
    assert label(facts, cats)[0] == Overall.GREEN


def test_snippet_only_source_cannot_carry_a_red_alone() -> None:
    facts = [fact("s1", url="https://www.linkedin.com/posts/x", source_type="press")]
    assert label(facts, [cat("regulator_finding_core", "s1")])[0] != Overall.RED


def test_red_flag_comes_first_and_is_decisive() -> None:
    facts = [*ADOPTION, fact("r1", source_type="regulator")]
    by_id = {f.id: f for f in facts}
    from vet.categorise import Kept

    kept = [
        Kept(category="wide_adoption", fact_ids=["g1", "g2"], scope="product"),
        Kept(category="regulator_finding_core", fact_ids=["r1"], scope="product"),
    ]
    flags = build_flags(kept, by_id, Overall.RED)
    assert flags[0].colour == FlagColour.RED and flags[0].decisive


async def test_reasoning_with_a_foreign_url_is_replaced_by_the_template() -> None:
    from vet.schemas import Flag

    flags = [
        Flag(
            text="Fined in 2025",
            colour=FlagColour.RED,
            source_url="https://regulator.example.gov/o",
            decisive=True,
        )
    ]
    words = " ".join(["word"] * 70)
    llm = FakeLLM(
        {
            _ReasoningOut: lambda u: _ReasoningOut(
                paragraph=f"Red. {words} see https://elsewhere.example/x"
            )
        }
    )
    text = await write_reasoning(
        llm, Overall.RED, flags, ["regulator_finding_core"], ["docs (blocked by robots.txt)"]
    )
    assert "elsewhere.example" not in text
    assert text.startswith("Red") and "Not checked: docs (blocked by robots.txt)" in text


async def test_categorised_view_end_to_end_with_fake_model(settings) -> None:
    facts = [
        fact(
            "r1",
            url="https://regulator.example.gov/order",
            source_type="regulator",
            when=TODAY - timedelta(days=100),
        )
    ]
    verdict = make_verdict().model_copy(update={"real_alive": RealAlive.YES, "trust": Trust.TRUST})
    base = baseline_view(verdict, facts, [])
    llm = FakeLLM(
        {
            _CategoriseOut: lambda u: _CategoriseOut(
                identifiable_vendor=True,
                operating_now=True,
                categories=[cat("regulator_finding_core", "r1")],
            ),
        }
    )
    view, notes, extra = await categorised_view(
        verdict=verdict,
        facts=facts,
        verification=[VerifyResult(fact_id="r1", source_ok=True)],
        identity=SiteIdentity(vendor_name="Acme Tools"),
        not_checked=[],
        llm=llm,
        fetcher=None,
        snippets={},
        settings=settings,
        base=base,
        today=TODAY,
    )  # type: ignore[arg-type]
    assert view.overall == Overall.RED and view.method == "categorised"
    assert view.flags[0].source_url == "https://regulator.example.gov/order"
    assert view.reasoning.startswith("Red")  # template: the fake has no reasoning responder
    assert extra == []


@pytest.mark.parametrize("bad", ["", "ghost"])
def test_category_without_valid_ids_is_dropped(bad: str) -> None:
    ids = [bad] if bad else []
    assert (
        label(ADOPTION, [*GREEN_CATS, _Category(category="discontinued", fact_ids=ids)])[0]
        == Overall.GREEN
    )


def _against(fid: str, wording: str) -> Fact:
    return fact(
        fid,
        url="https://acme-tools.example/p",
        source_type="vendor",
        kind=SourceKind.VENDOR,
        topic="lifecycle_statement",
        tags=["statement_against_interest", f"lifecycle:{wording}"],
    )


def test_superseded_wording_is_legacy_not_discontinued() -> None:
    facts = [*ADOPTION, _against("v1", "superseded")]
    overall, names = label(facts, [*GREEN_CATS, cat("discontinued", "v1")])
    assert overall == Overall.YELLOW and "legacy_or_superseded" in names


def test_end_wording_stays_discontinued() -> None:
    assert label([_against("v1", "end")], [cat("discontinued", "v1")])[0] == Overall.RED


def test_directory_and_social_sources_cannot_carry_a_yellow_alone() -> None:
    facts = [
        *ADOPTION,
        fact("d1", url="https://dir.example/x", source_type="directory"),
        fact("s1", url="https://social.example/x", source_type="social"),
    ]
    assert label(facts, [*GREEN_CATS, cat("licensing_ambiguity", "d1", "s1")])[0] == Overall.GREEN


def test_uncited_fact_of_the_category_topic_supports_it() -> None:
    from vet.categorise import with_support

    facts = [fact("o1", topic="ownership_change"), fact("nc", topic="divestment_non_core")]
    supported = with_support(cat("ownership_churn", "o1"), facts)
    assert supported.fact_ids == ["o1", "nc"]
    assert label(facts, [supported])[0] == Overall.YELLOW


def test_named_vendor_counts_as_identifiable() -> None:
    out = _CategoriseOut(identifiable_vendor=False, operating_now=True, categories=[])
    from vet.categorise import Kept

    kept = [Kept(category="wide_adoption", fact_ids=["g1"], scope="product")]
    verdict = make_verdict().model_copy(update={"real_alive": RealAlive.YES})
    assert not base_is_met(out, kept, verdict)
    assert base_is_met(out, kept, verdict, SiteIdentity(vendor_name="Acme Tools"))


def test_code_proposes_a_regulator_category_the_model_missed() -> None:
    from vet.categorise import proposed_by_code

    facts = [
        fact("r1", topic="regulator_finding", source_type="regulator", tags=["scope:product"]),
        fact("p1", topic="regulator_finding", tags=["scope:parent"]),
    ]
    out = _CategoriseOut(identifiable_vendor=True, operating_now=True, categories=[])
    (proposed,) = proposed_by_code(out, facts)
    assert proposed.category == "regulator_finding_core" and proposed.fact_ids == ["r1"]


def test_overclaiming_corporate_sentence_falls_back_to_its_quote() -> None:
    from vet.verify import fall_back_to_quotes

    f = fact("r1", url="https://regulator.example.gov/o", source_type="regulator")
    results = {"r1": VerifyResult(fact_id="r1", source_ok=True, supported=False)}
    rewritten = fall_back_to_quotes({"r1": f}, results)
    assert results["r1"].passed and rewritten["r1"].statement.endswith('"quote"')


def test_archive_capture_history_alone_earns_no_track_record() -> None:
    f1 = fact("a1", source_type="record", kind=SourceKind.RECORD, topic="capture_history")
    f2 = fact("a2", source_type="record", kind=SourceKind.RECORD, topic="first_capture")
    f1 = f1.model_copy(update={"collector": "age"})
    f2 = f2.model_copy(update={"collector": "age"})
    assert "long_track_record" not in label([f1, f2], [cat("long_track_record", "a1", "a2")])[1]


def test_non_core_divestment_with_owner_scope_stays_ownership_churn() -> None:
    facts = [*ADOPTION, fact("d1", topic="divestment_non_core")]
    cats = [*GREEN_CATS, cat("ownership_churn", "d1", scope="parent")]
    overall, names = label(facts, cats)
    assert overall == Overall.YELLOW and "ownership_churn" in names


def _archive(fid: str, topic: str) -> Fact:
    f = fact(
        fid,
        url="https://web.archive.org/web/2012/https://acme-tools.example/",
        source_type="record",
        kind=SourceKind.RECORD,
        topic=topic,
    )
    return f.model_copy(update={"collector": "archive", "polarity": Polarity.NEUTRAL})


def test_same_vendor_since_gives_track_record_by_topic_not_polarity() -> None:
    from vet.categorise import proposed_by_code

    facts = [*ADOPTION, _archive("ar", "same_vendor_since")]
    out = _CategoriseOut(identifiable_vendor=True, operating_now=True, categories=[])
    (proposed,) = proposed_by_code(out, facts)
    assert proposed.category == "long_track_record"
    overall, names = label(facts, [*GREEN_CATS, proposed])
    assert overall == Overall.GREEN and "long_track_record" in names


def test_different_early_site_cancels_domain_dates_but_not_a_register_date() -> None:
    age = fact("d1", source_type="record", kind=SourceKind.RECORD, topic="first_capture")
    age = age.model_copy(update={"collector": "age"})
    other = _archive("ar", "domain_older_than_vendor")
    reg = fact("reg", source_type="record", kind=SourceKind.RECORD, topic="register_entry")
    assert "long_track_record" not in label([age, other], [cat("long_track_record", "d1", "ar")])[1]
    names = label([age, other, reg], [cat("long_track_record", "d1", "ar", "reg")])[1]
    assert "long_track_record" in names


def test_licensing_ambiguity_alone_is_context_and_green_stays_green() -> None:
    facts = [
        *ADOPTION,
        fact("l1", url="https://press-one.example/a"),
        fact("l2", url="https://press-two.example/b"),
    ]
    overall, names = label(facts, [*GREEN_CATS, cat("licensing_ambiguity", "l1", "l2")])
    assert overall == Overall.GREEN and "licensing_ambiguity" in names
