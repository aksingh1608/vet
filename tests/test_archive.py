"""Archive track record: offline, Wayback answers mocked, invented companies."""

import httpx

from tests.conftest import FakeLLM, fixture_text
from vet.collectors import archive
from vet.collectors.base import Context, SiteIdentity
from vet.schemas import Polarity
from vet.text import contains_quote

DOMAIN = "tallowmere.example"
TALLOWMERE = SiteIdentity(
    vendor_name="Tallowmere",
    product_name="Tallowmere Ledger",
    legal_entity="Tallowmere Software Ltd",
)
CDX_HEADER = ["timestamp", "length"]


def snap(ts: str) -> str:
    return f"https://web.archive.org/web/{ts}/https://{DOMAIN}/"


def wayback(rows: list[list[str]], pages: dict[str, object]):  # noqa: ANN201
    """CDX answers ``rows``; a snapshot answers its fixture, or the status code given."""

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/cdx/search/cdx" in url:
            return httpx.Response(200, json=[CDX_HEADER, *rows])
        ts = url.split("/web/", 1)[1].split("/", 1)[0]
        page = pages.get(ts, 404)
        if isinstance(page, int):
            return httpx.Response(page)
        return httpx.Response(200, text=fixture_text(f"archive/{page}"))

    return answer


def make_ctx(fetcher, identity: SiteIdentity = TALLOWMERE) -> Context:  # noqa: ANN001
    return Context(
        url=f"https://{DOMAIN}/",
        domain=DOMAIN,
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=FakeLLM(),
        tavily=None,
        identity=identity,
    )


async def run(router_fetcher, rows, pages, identity: SiteIdentity = TALLOWMERE):  # noqa: ANN001
    fetcher, rec = router_fetcher([("web.archive.org", wayback(rows, pages))])
    async with fetcher:
        facts = await archive.collect(make_ctx(fetcher, identity))
    snapshots = [str(r.url) for r in rec.requests if "/web/" in str(r.url)]
    return facts, snapshots, rec


def assert_weightless(fact) -> None:  # noqa: ANN001
    assert fact.weight == 0.0
    assert fact.polarity == Polarity.NEUTRAL
    assert not fact.is_hard_flag


async def test_same_vendor_on_the_earliest_copy(router_fetcher) -> None:
    rows = [["20120304101500", "5400"], ["20130101000000", "6100"]]
    facts, snapshots, _ = await run(
        router_fetcher, rows, {"20120304101500": "same_vendor_2012.html"}
    )
    assert len(facts) == 1 and len(snapshots) == 1
    fact = facts[0]
    assert fact.topic == "same_vendor_since"
    assert "2012" in fact.statement and "Tallowmere Ledger" in fact.statement
    assert fact.source_url == snap("20120304101500")
    assert contains_quote(fixture_text("archive/same_vendor_2012.html"), fact.quote)
    assert fact.observed_at is not None and fact.observed_at.year == 2012
    assert_weightless(fact)


async def test_different_site_is_neutral_never_negative(router_fetcher) -> None:
    rows = [["20090611080000", "4800"]]
    facts, _, _ = await run(router_fetcher, rows, {"20090611080000": "other_site_2009.html"})
    assert len(facts) == 1
    fact = facts[0]
    assert fact.topic == "domain_older_than_vendor"
    assert "2009-06-11" in fact.statement
    # The page prints the domain itself; that is not the vendor's name.
    assert fact.quote == "Bramblecote Kennels - dog boarding in the Cotswolds"
    assert fact.source_url == snap("20090611080000")
    assert_weightless(fact)


async def test_parked_copy_is_skipped_for_the_next(router_fetcher) -> None:
    rows = [["20100101000000", "2200"], ["20120304101500", "5400"]]
    pages = {"20100101000000": "parked.html", "20120304101500": "same_vendor_2012.html"}
    facts, snapshots, _ = await run(router_fetcher, rows, pages)
    assert [f.topic for f in facts] == ["same_vendor_since"]
    assert len(snapshots) == 2


async def test_thin_or_parked_copies_give_no_fact_and_at_most_two_fetches(router_fetcher) -> None:
    rows = [
        ["20100101000000", "2200"],
        ["20100201000000", "2300"],
        ["20120304101500", "5400"],
    ]
    pages = {
        "20100101000000": "parked.html",
        "20100201000000": "parked.html",
        "20120304101500": "same_vendor_2012.html",
    }
    facts, snapshots, _ = await run(router_fetcher, rows, pages)
    assert facts == []
    assert len(snapshots) == archive.MAX_SNAPSHOTS == 2


async def test_tiny_captures_are_not_fetched(router_fetcher) -> None:
    rows = [["20080101000000", "310"], ["20120304101500", "5400"]]
    facts, snapshots, _ = await run(
        router_fetcher, rows, {"20120304101500": "same_vendor_2012.html"}
    )
    assert [f.topic for f in facts] == ["same_vendor_since"]
    assert snapshots == [snap("20120304101500")]


async def test_fetch_problem_gives_no_fact(router_fetcher) -> None:
    rows = [["20120304101500", "5400"], ["20130101000000", "6100"]]
    pages = {"20120304101500": 503, "20130101000000": "same_vendor_2012.html"}
    facts, snapshots, _ = await run(router_fetcher, rows, pages)
    assert facts == []
    assert "20130101000000" not in " ".join(snapshots)  # a failure stops; it is not evidence


async def test_cdx_failure_gives_no_fact(router_fetcher) -> None:
    fetcher, _ = router_fetcher([("web.archive.org", 500)])
    async with fetcher:
        assert await archive.collect(make_ctx(fetcher)) == []


async def test_no_names_no_requests(router_fetcher) -> None:
    facts, _, rec = await run(router_fetcher, [], {}, identity=SiteIdentity())
    assert facts == [] and rec.requests == []


def test_names_include_the_entity_without_its_legal_form() -> None:
    ctx_names = archive.names(
        Context(
            url="",
            domain=DOMAIN,
            settings=None,  # type: ignore[arg-type]
            fetcher=None,  # type: ignore[arg-type]
            llm=FakeLLM(),
            tavily=None,
            identity=TALLOWMERE,
        )
    )
    assert "Tallowmere Software" in ctx_names and "Tallowmere Software Ltd" in ctx_names


def test_name_matching_ignores_host_names() -> None:
    assert archive.name_on_page("Visit tallowmere.example today", ["Tallowmere"]) == ""
    assert archive.name_on_page("Welcome to Tallow Mere Ledger", ["Tallowmere Ledger"]) == (
        "Tallowmere Ledger"
    )
