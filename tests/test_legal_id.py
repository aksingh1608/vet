import base64
import json

import httpx

from tests.conftest import FAKE_CH_TOKEN, FakeLLM, fixture_text
from vet.collectors import legal_id
from vet.collectors.base import Context, SiteIdentity
from vet.collectors.legal_id import _Record, _Records
from vet.schemas import Axis, Polarity, SourceKind
from vet.tavily import Tavily

ND_URL = "https://www.northdata.de/Ledgerly%20Software%20GmbH,%20M%C3%BCnchen/HRB%20123456"
ND_OTHER = "https://www.northdata.de/Ledgerly%20Consulting%20UG,%20Berlin/HRB%20999"


def nd_records(**override: object) -> _Records:
    good = {
        "url": ND_URL,
        "same_entity": True,
        "register_number": "HRB 123456",
        "register_court": "Amtsgericht München",
        "founded": "Gegründet 2009",
        "status": "active",
        "quote": "Ledgerly Software GmbH, München. Amtsgericht München HRB 123456.",
    }
    good.update(override)
    namesake = {
        "url": ND_OTHER,
        "same_entity": False,
        "register_number": "HRB 999",
        "register_court": "Amtsgericht Charlottenburg",
        "founded": "",
        "status": "liquidation",
        "quote": "In Liquidation.",
    }
    return _Records(records=[_Record(**good), _Record(**namesake)])


def make_ctx(fetcher, llm, identity: SiteIdentity, domain: str = "ledgerly.example") -> Context:
    return Context(
        url=f"https://{domain}/",
        domain=domain,
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=llm,
        tavily=Tavily(fetcher.settings, fetcher),
        identity=identity,
    )


DE = SiteIdentity(
    vendor_name="Ledgerly",
    legal_entity="Ledgerly Software GmbH",
    register_court="Amtsgericht München",
    register_number="HRB 123456",
    country="DE",
)


async def test_northdata_found_via_tavily_and_never_fetched(router_fetcher) -> None:
    fetcher, rec = router_fetcher([("api.tavily.com", fixture_text("api/tavily_northdata.json"))])
    llm = FakeLLM({_Records: lambda user: nd_records()})
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, llm, DE))

    assert not any("northdata" in r.url.host for r in rec.requests)
    body = json.loads(rec.requests[0].content)
    assert body["include_domains"] == ["northdata.com", "northdata.de"]
    assert body["search_depth"] == "basic"

    record = facts[0]
    assert record.source_url == ND_URL
    assert record.source_kind == SourceKind.RECORD
    assert record.polarity == Polarity.POSITIVE and record.axis == Axis.TRUST
    assert "HRB 123456" in record.statement
    assert any("registered in 2009" in f.statement for f in facts)
    # the namesake in liquidation is ignored
    assert not any(f.is_hard_flag for f in facts)


async def test_northdata_bad_status_and_number_mismatch(router_fetcher) -> None:
    fetcher, _ = router_fetcher([("api.tavily.com", fixture_text("api/tavily_northdata.json"))])
    identity = DE.model_copy(update={"register_number": "HRB 654321"})
    llm = FakeLLM({_Records: lambda user: nd_records(status="insolvency", quote="Status: aktiv.")})
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, llm, identity))
    assert any(f.is_hard_flag and "insolvency" in f.statement for f in facts)
    assert any("differs from the one on the site (HRB 654321)" in f.statement for f in facts)


async def test_northdata_quote_not_in_snippet_is_dropped(router_fetcher) -> None:
    fetcher, _ = router_fetcher([("api.tavily.com", fixture_text("api/tavily_northdata.json"))])
    llm = FakeLLM({_Records: lambda user: nd_records(quote="Founded 1887 by royal decree")})
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, llm, DE))
    # the invented quote yields nothing; only the code-matched register URL remains
    assert [f.topic for f in facts] == ["register_record"]
    assert facts[0].quote == "" and "1887" not in facts[0].statement


UK = SiteIdentity(
    vendor_name="Ledgerly",
    legal_entity="Ledgerly Limited",
    register_court="Companies House",
    register_number="Company No. 01234567",
    country="GB",
)


async def test_companies_house_active(router_fetcher) -> None:
    fetcher, rec = router_fetcher([("/company/01234567", fixture_text("api/ch_profile.json"))])
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, FakeLLM(), UK, "ledgerly.co.uk"))
    status = next(f for f in facts if "status 'active'" in f.statement)
    assert status.polarity == Polarity.POSITIVE
    assert status.source_url.startswith("https://find-and-update.company-information")
    assert any("incorporated on 2009-02-01" in f.statement for f in facts)
    # the key goes in the Basic auth header, never in the URL
    request = rec.requests[0]
    expected = base64.b64encode(f"{FAKE_CH_TOKEN}:".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert FAKE_CH_TOKEN not in str(request.url)


async def test_companies_house_dissolved_is_hard_flag(router_fetcher) -> None:
    fetcher, _ = router_fetcher(
        [("/company/01234567", fixture_text("api/ch_profile_dissolved.json"))]
    )
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, FakeLLM(), UK, "ledgerly.co.uk"))
    assert any(f.is_hard_flag and f.polarity == Polarity.NEGATIVE for f in facts)


async def test_companies_house_search_by_name(router_fetcher) -> None:
    search = {"items": [{"title": "LEDGERLY LIMITED", "company_number": "01234567"}]}
    fetcher, rec = router_fetcher(
        [
            ("/search/companies", httpx.Response(200, json=search)),
            ("/company/01234567", fixture_text("api/ch_profile.json")),
        ]
    )
    identity = UK.model_copy(update={"register_number": ""})
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, FakeLLM(), identity, "ledgerly.co.uk"))
    assert "q=Ledgerly+Limited" in str(rec.requests[0].url)
    assert facts


async def test_no_legal_entity_means_no_lookups(router_fetcher) -> None:
    fetcher, rec = router_fetcher([])
    async with fetcher:
        facts = await legal_id.collect(make_ctx(fetcher, FakeLLM(), SiteIdentity()))
    assert facts == [] and rec.requests == []


def test_north_data_url_naming_entity_and_number_is_a_record(router_fetcher) -> None:
    from datetime import UTC, datetime

    from vet.collectors.base import Context, SiteIdentity
    from vet.collectors.legal_id import url_record_fact

    fetcher, _ = router_fetcher([])
    ctx = Context(
        url="https://www.projektron.de/",
        domain="projektron.de",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=None,  # type: ignore[arg-type]
        tavily=None,
        identity=SiteIdentity(
            legal_entity="Projektron GmbH",
            register_court="Amtsgericht Charlottenburg",
            register_number="HRB 79669",
        ),
    )
    now = datetime(2026, 11, 25, tzinfo=UTC)
    url = (
        "https://www.northdata.de/PROJEKTRON%20GmbH,%20Berlin/"
        "Amtsgericht%20Charlottenburg%20(Berlin)%20HRB%2079669%20B"
    )
    fact = url_record_fact(ctx, url, now)
    assert fact is not None and fact.topic == "register_record" and fact.quote == ""
    assert fact.source_url == url
    # another entity, or the right name with another number: no record
    assert (
        url_record_fact(ctx, "https://www.northdata.de/HRB%20GmbH,%20Hannover/HRB%20101453", now)
        is None
    )
    assert url_record_fact(ctx, url.replace("79669", "796690"), now) is None
