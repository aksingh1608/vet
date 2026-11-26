import json

import httpx

from tests.conftest import FakeLLM, fixture_text
from vet.collectors import age
from vet.collectors.base import Context
from vet.schemas import Polarity, SourceKind


def routes(**overrides: object) -> list[tuple[str, object]]:
    base: dict[str, object] = {
        "rdap.org/domain/": fixture_text("api/rdap.json"),
        "crt.sh/": fixture_text("api/crtsh.json"),
        "collapse=timestamp": fixture_text("api/cdx_years.json"),
        "cdx/search/cdx": fixture_text("api/cdx_first.json"),
    }
    base.update(overrides)
    return list(base.items())


def ctx_for(fetcher) -> Context:
    return Context(
        url="https://ledgerly.example/",
        domain="ledgerly.example",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=FakeLLM(),
        tavily=None,
    )


async def test_age_facts_from_all_three_sources(router_fetcher) -> None:
    fetcher, rec = router_fetcher(routes())
    async with fetcher:
        facts = await age.collect(ctx_for(fetcher))
    by_topic = {f.topic: f for f in facts}

    rdap = by_topic["domain_registered"]
    assert "registered on 2008-05-14" in rdap.statement
    assert rdap.quote == "2008-05-14T10:00:00Z"  # verifiable in the cached RDAP JSON
    assert rdap.polarity == Polarity.POSITIVE
    # human link to the ICANN lookup; verified against the RDAP JSON
    assert rdap.source_url == "https://lookup.icann.org/en/lookup?name=ledgerly.example"
    assert rdap.check_url == "https://rdap.org/domain/ledgerly.example"
    assert rdap.observed_at is not None and rdap.observed_at.year == 2008

    crt = by_topic["first_certificate"]
    assert "2012-01-20" in crt.statement  # the earliest of the two certificates
    assert crt.source_url == "https://crt.sh/?q=ledgerly.example"  # no output=json
    assert "output=json" in crt.check_url

    first, years = by_topic["first_capture"], by_topic["capture_history"]
    assert "earliest Wayback Machine capture of ledgerly.example is from 2009-02-14" in (
        first.statement
    )
    assert first.source_url == "https://web.archive.org/web/20090214101500/ledgerly.example/"
    assert first.quote == ""  # computed fact: verified by resolving the snapshot
    assert "18 of the 18 years from 2009 to 2026" in years.statement
    assert years.polarity == Polarity.POSITIVE
    assert years.source_url == "https://web.archive.org/web/*/ledgerly.example"
    assert "collapse=timestamp" in years.check_url
    assert all(f.source_kind == SourceKind.RECORD for f in facts)
    assert not any("northdata" in str(r.url) for r in rec.requests)


async def test_crtsh_failure_is_optional(router_fetcher) -> None:
    calls = {"crt": 0}

    def crt_down(request: httpx.Request) -> httpx.Response:
        calls["crt"] += 1
        return httpx.Response(503)

    fetcher, _ = router_fetcher(routes(**{"crt.sh/": crt_down}))
    async with fetcher:
        facts = await age.collect(ctx_for(fetcher))
    assert calls["crt"] == 1  # one attempt only
    assert not any(f.topic == "first_certificate" for f in facts)
    assert any(f.topic == "domain_registered" for f in facts)
    assert any("web.archive.org" in f.source_url for f in facts)


async def test_de_rdap_without_creation_date_gives_no_rdap_fact(router_fetcher) -> None:
    fetcher, _ = router_fetcher(routes(**{"rdap.org/domain/": fixture_text("api/rdap_de.json")}))
    async with fetcher:
        facts = await age.collect(ctx_for(fetcher))
    assert not any(f.topic == "domain_registered" for f in facts)
    assert any("web.archive.org" in f.source_url for f in facts)


async def test_no_wayback_captures_is_a_negative_record_fact(router_fetcher) -> None:
    empty = fixture_text("api/cdx_empty.json")
    fetcher, _ = router_fetcher(routes(**{"collapse=timestamp": empty, "cdx/search/cdx": empty}))
    async with fetcher:
        facts = await age.collect(ctx_for(fetcher))
    none = [f for f in facts if "no captures" in f.statement]
    assert len(none) == 1
    assert none[0].polarity == Polarity.NEGATIVE
    assert "cdx/search/cdx" in none[0].check_url  # the checkable CDX query
    assert none[0].source_url == "https://web.archive.org/web/*/ledgerly.example"


async def test_young_domain_is_negative(router_fetcher) -> None:
    young = json.dumps(
        {"events": [{"eventAction": "registration", "eventDate": "2026-10-02T00:00:00Z"}]}
    )
    fetcher, _ = router_fetcher(routes(**{"rdap.org/domain/": young}))
    async with fetcher:
        facts = await age.collect(ctx_for(fetcher))
    rdap = next(f for f in facts if f.topic == "domain_registered")
    assert rdap.polarity == Polarity.NEGATIVE
