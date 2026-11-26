from datetime import UTC, datetime

from vet.claims_check import check_claims
from vet.schemas import Axis, Claim, Fact, Polarity, SourceKind

NOW = datetime(2026, 11, 25, tzinfo=UTC)
ABOUT = "https://acme.example/about"


def claim(year: int, cid: str = "c1", ctype: str = "founded_year") -> Claim:
    return Claim(
        id=cid,
        type=ctype,  # type: ignore[arg-type]
        text=f"Vendor says it was founded in {year}",
        source_url=ABOUT,
        quote=f"Founded in {year}, Acme serves teams worldwide",
    )


def record(fid: str, topic: str, year: int, collector: str = "age") -> Fact:
    return Fact(
        id=fid,
        collector=collector,
        statement=f"{topic} {year}",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.POSITIVE,
        weight=0.4,
        source_url=f"https://records.example/{fid}",
        quote="",
        source_kind=SourceKind.RECORD,
        fetched_at=NOW,
        topic=topic,
        observed_at=datetime(year, 6, 1, tzinfo=UTC),
    )


AGE_2025 = [
    record("age-0", "domain_registered", 2025),
    record("age-1", "first_certificate", 2025),
    record("age-2", "first_capture", 2025),
]


def test_claim_far_before_all_age_evidence_is_unconfirmed_with_a_negative_fact() -> None:
    checks, facts = check_claims([claim(2015)], AGE_2025, "acme.example", {"acme"}, NOW)
    assert checks[0].status == "unconfirmed"  # domain records never contradict a founding year
    assert len(facts) == 1
    f = facts[0]
    assert f.polarity == Polarity.NEGATIVE and f.weight == 0.5 and not f.is_hard_flag
    assert f.topic == "founding_year_gap"
    assert f.statement.startswith("The site says the company was founded in 2015; ")
    assert "the domain was registered on 2025-06-01" in f.statement
    # the reason quotes the vendor page; the record it rests on goes along for verification
    assert f.source_url == ABOUT and f.quote == claim(2015).quote
    assert f.related_fact_ids == ["age-0"]


def test_gap_of_three_years_or_less_is_no_fact() -> None:
    # e.g. "founded 2024", domain registered 2025: a company may start before its website
    checks, facts = check_claims([claim(2022)], AGE_2025, "acme.example", set(), NOW)
    assert facts == []
    assert checks[0].status == "unconfirmed"
    assert "consistent" in checks[0].note


def test_needs_at_least_two_age_records() -> None:
    _, facts = check_claims([claim(2001)], AGE_2025[:1], "acme.example", set(), NOW)
    assert facts == []


def test_register_that_disagrees_contradicts_the_claim() -> None:
    register = record("legal_id-12", "register_founded", 2021, collector="legal_id")
    checks, facts = check_claims([claim(2009)], [*AGE_2025, register], "acme.example", set(), NOW)
    assert checks[0].status == "contradicted"
    assert facts[0].is_hard_flag and facts[0].topic == "contradicted_claim"
    assert "company register shows it was registered in 2021" in facts[0].statement
    assert facts[0].related_fact_ids == ["legal_id-12"]


def test_register_that_agrees_confirms_the_claim() -> None:
    register = record("legal_id-12", "register_founded", 2009, collector="legal_id")
    checks, facts = check_claims([claim(2009)], [*AGE_2025, register], "acme.example", set(), NOW)
    assert checks[0].status == "confirmed" and facts == []


def test_register_number_mismatch_is_contradicted() -> None:
    mismatch = record("legal_id-13", "contradicted_claim", 2020, collector="legal_id")
    c = claim(2020, ctype="register_number")
    checks, _ = check_claims([c], [mismatch], "acme.example", set(), NOW)
    assert checks[0].status == "contradicted"


def test_customer_confirmed_only_by_an_independent_mention() -> None:
    customer = Claim(
        id="c2",
        type="customer",
        text="Vendor says Globex is a customer",
        source_url=ABOUT,
        quote="Globex runs on Acme",
    )
    mention = record("footprint-0", "customer", 2024, collector="footprint").model_copy(
        update={
            "source_kind": SourceKind.INDEPENDENT,
            "statement": "A Globex case study describes using Acme",
        }
    )
    checks, _ = check_claims([customer], [mention], "acme.example", {"acme"}, NOW)
    assert checks[0].status == "confirmed"
    checks, _ = check_claims([customer], [], "acme.example", {"acme"}, NOW)
    assert checks[0].status == "unconfirmed"


# ---- confirmation needs the same specific fact ------------------------------------------------

PROJEKTRON_NAMES = {"Projektron", "Projektron BCS", "projektron"}


def independent(fid: str, statement: str, url: str, quote: str = "") -> Fact:
    return record(fid, "directory", 2024, collector="footprint").model_copy(
        update={
            "source_kind": SourceKind.INDEPENDENT,
            "statement": statement,
            "quote": quote,
            "source_url": url,
        }
    )


CUSTOMERS_850 = Claim(
    id="c7",
    type="customer",
    text="Vendor says over 850 customers use BCS.",
    source_url="https://www.projektron.de/",
    quote="Über 850 Kunden setzen BCS ein",
)


def test_a_general_product_description_does_not_confirm_a_customer_count() -> None:
    # The Projektron case: profiles describing BCS were taken as confirming "850 customers".
    general = [
        independent(
            "footprint-1",
            "A SIMIO directory listing describes Projektron BCS as project management software.",
            "https://navigator.simio-consulting.com/en/details/projektron-bcs",
        ),
        independent(
            "footprint-2",
            "Capterra lists Projektron BCS with 4.5 stars from 120 reviews.",
            "https://www.capterra.com/p/173655/Projektron-BCS",
        ),
    ]
    checks, _ = check_claims([CUSTOMERS_850], general, "projektron.de", PROJEKTRON_NAMES, NOW)
    assert checks[0].status == "unconfirmed" and checks[0].fact_ids == []

    stated = independent(
        "footprint-3",
        "An OMR review page says Projektron BCS is used by more than 850 customers.",
        "https://omr.com/en/reviews/product/projektron-bcs",
    )
    checks, _ = check_claims(
        [CUSTOMERS_850], [*general, stated], "projektron.de", PROJEKTRON_NAMES, NOW
    )
    assert checks[0].status == "confirmed" and checks[0].fact_ids == ["footprint-3"]


def test_a_different_number_or_a_search_tool_page_does_not_confirm() -> None:
    other_number = independent(
        "footprint-4",
        "A review says Projektron BCS has 8500 users.",
        "https://omr.com/en/reviews/product/projektron-bcs",
    )
    tool_page = independent(
        "footprint-5",
        "Tavily's company profile says Projektron BCS has 850 customers.",
        "https://tavily.com/knowledge/company/d4L3DmNnFzVY",
    )
    checks, _ = check_claims(
        [CUSTOMERS_850], [other_number, tool_page], "projektron.de", PROJEKTRON_NAMES, NOW
    )
    assert checks[0].status == "unconfirmed"


def test_register_record_must_state_the_claimed_number() -> None:
    c = Claim(
        id="c3",
        type="register_number",
        text="Vendor is registered at Amtsgericht Charlottenburg under HRB 79669.",
        source_url=ABOUT,
        quote="Amtsgericht Charlottenburg HRB 79669",
    )

    def register(statement: str) -> Fact:
        return record("legal_id-1", "register_record", 2024, collector="legal_id").model_copy(
            update={"statement": statement}
        )

    same = register(
        "North Data has a register page for Acme GmbH (Amtsgericht Charlottenburg HRB 79669)"
    )
    other = register(
        "North Data has a register page for Acme GmbH (Amtsgericht Charlottenburg HRB 12345)"
    )
    assert check_claims([c], [same], "acme.example", set(), NOW)[0][0].status == "confirmed"
    assert check_claims([c], [other], "acme.example", set(), NOW)[0][0].status == "unconfirmed"


def test_register_year_must_match_exactly_to_confirm() -> None:
    register = record("legal_id-12", "register_founded", 2010, collector="legal_id")
    checks, facts = check_claims([claim(2009)], [*AGE_2025, register], "acme.example", set(), NOW)
    assert checks[0].status == "unconfirmed" and facts == []  # near miss: neither way
