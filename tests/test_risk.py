import json
from datetime import UTC, datetime

import pytest

from tests.conftest import FakeLLM, fixture_text
from vet.collectors import risk
from vet.collectors.base import Context, SiteIdentity
from vet.collectors.risk import _Classified, _Item, news_facts_from
from vet.llm import unwrap
from vet.schemas import Axis, Polarity, SourceKind
from vet.tavily import Tavily, TavilyResult

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def make_ctx(fetcher, llm, tavily: bool = True, country: str = "DE") -> Context:
    return Context(
        url="https://ledgerly.example/",
        domain="ledgerly.example",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=llm,
        tavily=Tavily(fetcher.settings, fetcher) if tavily else None,
        identity=SiteIdentity(vendor_name="Ledgerly", country=country),
    )


async def test_hibp_breaches_become_record_facts(router_fetcher) -> None:
    fetcher, rec = router_fetcher(
        [("haveibeenpwned.com/api/v3/breaches", fixture_text("api/hibp_breaches.json"))]
    )
    async with fetcher:
        facts = await risk.collect(make_ctx(fetcher, FakeLLM(), tavily=False))

    big, small = facts
    assert big.topic == "breach" and big.source_kind == SourceKind.RECORD
    assert big.is_hard_flag and big.weight == 0.8  # passwords, 2.4M accounts, within 5 years
    assert "2,400,000 accounts" in big.statement and "2023-03-14" in big.statement
    assert big.source_url == "https://haveibeenpwned.com/PwnedWebsites#Ledgerly"
    assert big.check_url.endswith("/api/v3/breaches?domain=ledgerly.example")
    assert big.quote == "2023-03-14"
    assert not small.is_hard_flag and small.weight == 0.4
    # only the one allowed HIBP endpoint, no key header
    hibp = [r for r in rec.requests if r.url.host == "haveibeenpwned.com"]
    assert [r.url.path for r in hibp] == ["/api/v3/breaches"]
    assert "hibp-api-key" not in {k.lower() for k in hibp[0].headers}


def item(url: str, event: str, quote: str, **kw: object) -> _Item:
    base: dict[str, object] = {
        "url": url,
        "about_this_vendor": True,
        "independent": True,
        "event": event,
        "date": "2024",
        "source_type": "other",
        "vendor_disclosed": False,
        "severity": 0.9,
        "statement": f"{event} reported",
        "quote": quote,
    }
    base.update(kw)
    return _Item.model_validate(base)


def results(*pairs: tuple[str, str]) -> dict[str, TavilyResult]:
    return {u: TavilyResult(url=u, content=c) for u, c in pairs}


async def test_single_source_news_is_never_a_hard_flag(router_fetcher) -> None:
    fetcher, _ = router_fetcher([])
    async with fetcher:
        ctx = make_ctx(fetcher, FakeLLM(), tavily=False)
    seen = results(
        ("https://heise.de/a", "Bei Ledgerly sind Kundendaten abgeflossen, berichtet heise."),
        ("https://blog.example/rumour", "Ledgerly is shutting down next month, I heard."),
    )
    items = [
        item(
            "https://heise.de/a",
            "breach",
            "Bei Ledgerly sind Kundendaten abgeflossen",
            source_type="forum",
        ),
        item(
            "https://blog.example/rumour",
            "discontinued",
            "Ledgerly is shutting down",
            source_type="blog",
        ),
    ]
    facts = news_facts_from(items, seen, ctx, NOW, 10)
    assert not any(f.is_hard_flag for f in facts)
    assert all(f.weight < 0.5 for f in facts)  # halved: one article is a lead, not a finding
    assert {f.topic for f in facts} == {"unconfirmed_breach", "unconfirmed_discontinued"}


async def test_corroborated_or_vendor_announced_events_can_be_hard_flags(router_fetcher) -> None:
    fetcher, _ = router_fetcher([])
    async with fetcher:
        ctx = make_ctx(fetcher, FakeLLM(), tavily=False)
    seen = results(
        ("https://heise.de/a", "Bei Ledgerly sind Kundendaten abgeflossen."),
        ("https://golem.de/b", "Ledgerly bestätigt Datenleck bei Kundendaten."),
        ("https://ledgerly.example/blog/goodbye", "Ledgerly will shut down on 31 March 2027."),
        ("https://other.example/x", "Ledger Farms, a dairy, was acquired."),
    )
    items = [
        item("https://heise.de/a", "breach", "Bei Ledgerly sind Kundendaten abgeflossen"),
        item("https://golem.de/b", "breach", "Ledgerly bestätigt Datenleck"),
        item(
            "https://ledgerly.example/blog/goodbye",
            "discontinued",
            "Ledgerly will shut down on 31 March 2027",
            independent=False,
        ),
        item("https://other.example/x", "acquisition", "was acquired", about_this_vendor=False),
        item("https://heise.de/a", "breach", "not in the snippet at all"),
    ]
    facts = news_facts_from(items, seen, ctx, NOW, 10)
    by_url = {f.source_url: f for f in facts}
    assert set(by_url) == {
        "https://heise.de/a",
        "https://golem.de/b",
        "https://ledgerly.example/blog/goodbye",
    }
    assert by_url["https://heise.de/a"].is_hard_flag
    shutdown = by_url["https://ledgerly.example/blog/goodbye"]
    assert shutdown.is_hard_flag and shutdown.source_kind == SourceKind.VENDOR
    assert shutdown.axis == Axis.REAL_ALIVE and shutdown.polarity == Polarity.NEGATIVE


async def test_news_queries_in_english_and_german(router_fetcher) -> None:
    tavily = json.dumps({"results": [{"url": "https://heise.de/a", "content": "nothing"}]})
    fetcher, rec = router_fetcher([("haveibeenpwned.com", "[]"), ("api.tavily.com", tavily)])
    sent: list[str] = []

    def classify(user: str) -> _Classified:
        sent.append(unwrap(user))
        return _Classified(items=[])

    async with fetcher:
        await risk.collect(make_ctx(fetcher, FakeLLM({_Classified: classify})))
    queries = [
        json.loads(r.content)["query"] for r in rec.requests if r.url.host == "api.tavily.com"
    ]
    assert any("data breach" in q for q in queries)
    assert any("Datenleck" in q and "Insolvenz" in q for q in queries)
    assert len(sent) == 1


async def test_one_reputable_press_report_is_enough_and_disclosure_is_tagged(
    router_fetcher,
) -> None:
    fetcher, _ = router_fetcher([])
    async with fetcher:
        ctx = make_ctx(fetcher, FakeLLM(), tavily=False)
    seen = results(
        ("https://mashable.com/a", "Mozilla is shutting down Ledgerly on July 8, 2025."),
        ("https://www.linkedin.com/posts/x", "Ledgerly confirmed a breach in 2023, they said."),
        ("https://techcrunch.com/b", "Ledgerly notified customers of a 2023 breach."),
    )
    items = [
        item(
            "https://mashable.com/a", "discontinued", "shutting down Ledgerly", source_type="press"
        ),
        item(  # a LinkedIn post is social, whatever the classifier says
            "https://www.linkedin.com/posts/x",
            "breach",
            "Ledgerly confirmed a breach in 2023",
            source_type="press",
        ),
        item(
            "https://techcrunch.com/b",
            "breach",
            "Ledgerly notified customers of a 2023 breach",
            source_type="press",
            vendor_disclosed=True,
            date="March 2023",
        ),
    ]
    facts = {f.source_url: f for f in news_facts_from(items, seen, ctx, NOW, 10)}
    shutdown = facts["https://mashable.com/a"]
    assert shutdown.is_hard_flag and shutdown.topic == "discontinued"
    assert facts["https://www.linkedin.com/posts/x"].source_type == "social"
    notice = facts["https://techcrunch.com/b"]
    assert notice.tags == ["disclosed"] and notice.observed_at.year == 2023


async def test_negatives_about_sister_brands_or_other_companies_are_ignored(router_fetcher) -> None:
    """The Cal.eu and Affinity cases: a sister service and a subsidiary shutting down are not
    the product being judged shutting down."""
    fetcher, _ = router_fetcher([])
    async with fetcher:
        cal = Context(
            url="https://cal.com/", domain="cal.com", settings=fetcher.settings,
            fetcher=fetcher, llm=FakeLLM(), tavily=None,
            identity=SiteIdentity(vendor_name="Cal.com"),
        )  # fmt: skip
        canva = Context(
            url="https://www.canva.com/", domain="canva.com", settings=fetcher.settings,
            fetcher=fetcher, llm=FakeLLM(), tavily=None,
            identity=SiteIdentity(vendor_name="Canva"),
        )  # fmt: skip
    cal_eu = results(("https://dev.to/x", "Heads up: Cal.eu closes on November 1, 2026."))
    facts = news_facts_from(
        [item("https://dev.to/x", "discontinued", "Cal.eu closes on November 1, 2026",
              source_type="press")],
        cal_eu, cal, NOW, 10,
    )  # fmt: skip
    assert facts == []
    affinity = results(
        ("https://dpreview.com/a", "Affinity's website no longer sells the desktop versions.")
    )
    facts = news_facts_from(
        [item("https://dpreview.com/a", "discontinued",
              "Affinity's website no longer sells the desktop versions", source_type="press")],
        affinity, canva, NOW, 10,
    )  # fmt: skip
    assert facts == []
    own = results(("https://cal.com/blog/x", "Cal.com will shut down on 1 March 2027."))
    facts = news_facts_from(
        [item("https://cal.com/blog/x", "discontinued", "Cal.com will shut down on 1 March 2027",
              independent=False)],
        own, cal, NOW, 10,
    )  # fmt: skip
    assert [f.topic for f in facts] == ["discontinued"]  # the product itself: kept


def test_vendors_own_breach_notice_counts_without_naming_itself() -> None:
    from vet.sources import names_entity

    notice = "We detected a malicious attack on our systems on 24 May 2019."
    assert names_entity(notice, "canva.com", {"Canva"}, own_site=True)
    assert not names_entity(notice, "canva.com", {"Canva"})  # a third party must name it
    assert not names_entity("Cal.eu closes on 1 Nov", "cal.com", {"Cal.com"}, own_site=True)


# ---- breach decided in code -------------------------------------------------------------------

CANVA_2026 = (
    "Canva has disclosed that “some enterprise customer records” were recently exposed "
    "during a cyber security incident affecting one of its third-party data tools"
)
CANVA_LEAK_SITE = (
    "A new dedicated leak site by threat actors calling themselves “The Seven Deadly Sins” "
    "lists Canva Pty Ltd among the sites that haven’t paid them."
)
CANVA_2019 = (
    "The Canva data breach occurred on May 24, 2019, when hackers compromised the platform’s "
    "database, reportedly stealing information tied to approximately 139 million user accounts."
)
BASECAMP_STUFFING = (
    "Basecamp's team logged out and reset the passwords for all of the 124 accounts which were "
    "successfully breached"
)
BASECAMP_NOT_COMPROMISE = (
    "The January 29, 2019 event documented here was credential stuffing, not a confirmed "
    "compromise of Basecamp’s systems"
)


@pytest.mark.parametrize("llm_said", ["breach", "security_incident"])
@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (CANVA_2026, "breach"),  # records ... exposed
        (CANVA_2019, "breach"),  # "data breach"
        (CANVA_LEAK_SITE, "security_incident"),  # a listing on a leak site is not a breach
        (BASECAMP_STUFFING, "security_incident"),  # "breached" accounts: credential stuffing
        (BASECAMP_NOT_COMPROMISE, "security_incident"),
        ("Bei Ledgerly kam es zu einer Datenpanne", "breach"),
        ("Ledgerly bestätigt Datenleck", "breach"),
        ("Ledgerly says this was not a data breach", "security_incident"),
    ],
)
def test_breach_is_decided_in_code_whatever_the_model_said(
    llm_said: str, quote: str, expected: str
) -> None:
    assert risk.security_event(llm_said, quote, 2026, frozenset()) == expected


def test_hibp_record_in_the_same_year_makes_a_breach() -> None:
    quote = "Hackers compromised Canva's database in May 2019"
    assert risk.security_event("security_incident", quote, 2019, frozenset({2019})) == "breach"
    assert risk.security_event("security_incident", quote, 2026, frozenset({2019})) != "breach"
    assert risk.security_event("lawsuit", CANVA_2019, 2019, frozenset({2019})) == "lawsuit"


async def test_canva_2026_breach_is_stable_and_stated_as_its_quote(router_fetcher) -> None:
    fetcher, _ = router_fetcher([])
    async with fetcher:
        ctx = Context(
            url="https://www.canva.com/", domain="canva.com", settings=fetcher.settings,
            fetcher=fetcher, llm=FakeLLM(), tavily=None,
            identity=SiteIdentity(vendor_name="Canva"),
        )  # fmt: skip
    url = "https://www.cyberdaily.au/security/14317-canva"
    seen = results((url, f"... {CANVA_2026} ..."))
    for llm_said in ("breach", "security_incident"):
        facts = news_facts_from(
            [item(url, llm_said, CANVA_2026, date="2026", source_type="press")], seen, ctx, NOW, 10
        )
        (fact,) = facts
        assert fact.topic == "breach" and fact.code_worded
        assert fact.statement == f'cyberdaily.au: "{CANVA_2026}"'


async def test_basecamp_credential_stuffing_never_becomes_a_breach(router_fetcher) -> None:
    fetcher, _ = router_fetcher([])
    async with fetcher:
        ctx = Context(
            url="https://basecamp.com/", domain="basecamp.com", settings=fetcher.settings,
            fetcher=fetcher, llm=FakeLLM(), tavily=None,
            identity=SiteIdentity(vendor_name="Basecamp"),
        )  # fmt: skip
    url = "https://www.bleepingcomputer.com/news/security/basecamp-stuffing"
    seen = results((url, f"... {BASECAMP_STUFFING} ..."))
    facts = news_facts_from(
        [item(url, "breach", BASECAMP_STUFFING, date="2019", source_type="press")],
        seen, ctx, NOW, 10, hibp_years=frozenset(),
    )  # fmt: skip
    assert [f.topic for f in facts] == ["security_incident"]
