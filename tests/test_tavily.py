import json

from tests.conftest import FAKE_TAVILY_TOKEN, fixture_text
from vet import trace
from vet.tavily import Tavily


async def test_credits_counted_on_miss_and_zero_on_rerun(router_fetcher) -> None:
    fetcher, rec = router_fetcher([("api.tavily.com", fixture_text("api/tavily_footprint.json"))])
    t = trace.Trace(product_url="https://ledgerly.example/")
    trace.set_current(t)
    try:
        async with fetcher:
            tavily = Tavily(fetcher.settings, fetcher)
            await tavily.search("ledgerly reviews", include_domains=["g2.com"])
            await tavily.search("ledgerly reviews", include_domains=["g2.com"])  # cached
            await tavily.search("ledgerly news")
    finally:
        trace.set_current(None)
    assert len(rec.requests) == 2
    assert t.usage.tavily_searches == 3
    assert t.usage.tavily_cache_hits == 1
    assert t.usage.tavily_credits == 2  # basic = 1 credit per uncached search
    events = [e for e in t.events if e["event"] == "tavily"]
    assert [e["credits"] for e in events] == [1, 0, 1]


async def test_api_key_in_header_not_in_body_or_cache(router_fetcher, keyed_settings) -> None:
    fetcher, rec = router_fetcher([("api.tavily.com", fixture_text("api/tavily_footprint.json"))])
    async with fetcher:
        await Tavily(fetcher.settings, fetcher).search("ledgerly")
    request = rec.requests[0]
    assert request.headers["Authorization"] == f"Bearer {FAKE_TAVILY_TOKEN}"
    assert FAKE_TAVILY_TOKEN not in request.content.decode()
    assert FAKE_TAVILY_TOKEN.encode() not in keyed_settings.cache_path.read_bytes()


async def test_advanced_depth_only_behind_flag(router_fetcher, keyed_settings) -> None:
    fetcher, rec = router_fetcher([("api.tavily.com", fixture_text("api/tavily_footprint.json"))])
    advanced = keyed_settings.model_copy(update={"tavily_advanced": True})
    t = trace.Trace(product_url="x")
    trace.set_current(t)
    try:
        async with fetcher:
            await Tavily(advanced, fetcher).search("ledgerly")
    finally:
        trace.set_current(None)
    assert json.loads(rec.requests[0].content)["search_depth"] == "advanced"
    assert t.usage.tavily_credits == 2


async def test_snippets_kept_for_verifier(router_fetcher) -> None:
    fetcher, _ = router_fetcher([("api.tavily.com", fixture_text("api/tavily_northdata.json"))])
    async with fetcher:
        tavily = Tavily(fetcher.settings, fetcher)
        results = await tavily.search("x")
    assert tavily.snippets[results[0].url].startswith("Ledgerly Software GmbH")


async def test_search_tool_pages_are_dropped_from_results(router_fetcher) -> None:
    body = {
        "results": [
            {"url": "https://tavily.com/knowledge/company/abc123", "content": "Acme is a tool"},
            {"url": "https://www.g2.com/products/acme/reviews", "content": "Acme reviews"},
        ]
    }
    fetcher, _ = router_fetcher([("api.tavily.com", json.dumps(body))])
    async with fetcher:
        tavily = Tavily(fetcher.settings, fetcher)
        results = await tavily.search("acme")
    assert [r.url for r in results] == ["https://www.g2.com/products/acme/reviews"]
    assert "https://tavily.com/knowledge/company/abc123" not in tavily.snippets
