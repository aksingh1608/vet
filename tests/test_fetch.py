import asyncio

import httpx
import pytest

from tests.conftest import FakeClock, Recorder
from vet.config import Settings
from vet.fetch import FetchError, FetchStatusError, RateLimiter, RobotsDisallowed

ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"


def site(pages: dict[str, tuple[int, str]], robots: str | None = ROBOTS_ALLOW_ALL):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if robots is None:
                return httpx.Response(404)
            return httpx.Response(200, text=robots)
        status, body = pages.get(request.url.path, (404, "not found"))
        return httpx.Response(status, text=body)

    return handler


# ---- rate limiting ------------------------------------------------------------------


async def test_rate_limiter_spaces_same_host(clock: FakeClock) -> None:
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    starts = []
    for _ in range(3):
        await limiter.wait("a.example")
        starts.append(clock.now)
    assert [b - a for a, b in zip(starts, starts[1:], strict=False)] == [1.0, 1.0]


async def test_rate_limiter_does_not_delay_other_hosts(clock: FakeClock) -> None:
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    await limiter.wait("a.example")
    await limiter.wait("b.example")
    assert clock.sleeps == []


async def test_rate_limiter_per_host_interval(clock: FakeClock) -> None:
    limiter = RateLimiter(1.0, {"fast.example": 0.25}, clock=clock, sleep=clock.sleep)
    await limiter.wait("fast.example")
    await limiter.wait("fast.example")
    assert clock.sleeps == [0.25]


async def test_rate_limiter_concurrent_tasks_are_spaced(clock: FakeClock) -> None:
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    starts: list[float] = []

    async def go() -> None:
        await limiter.wait("a.example")
        starts.append(clock.now)

    await asyncio.gather(*(go() for _ in range(4)))
    starts.sort()
    assert all(b - a >= 1.0 for a, b in zip(starts, starts[1:], strict=False))


async def test_fetcher_spaces_requests_to_same_host(make_fetcher) -> None:
    rec = Recorder(site({"/a": (200, "A"), "/b": (200, "B"), "/c": (200, "C")}))
    async with make_fetcher(rec) as f:
        for path in ("/a", "/b", "/c"):
            await f.get_text(f"https://vendor.example{path}")
    # robots.txt, /a, /b, /c: four requests, each at least 1 s after the previous one
    assert rec.paths() == ["/robots.txt", "/a", "/b", "/c"]
    gaps = [b - a for a, b in zip(rec.times, rec.times[1:], strict=False)]
    assert all(g >= 1.0 for g in gaps), gaps


def test_default_source_policies() -> None:
    s = Settings()
    assert 60 / s.host_policies["web.archive.org"].min_interval_s <= 20
    crt = s.host_policies["crt.sh"]
    assert 60 / crt.min_interval_s <= 4
    assert crt.max_retries == 0
    assert crt.timeout_s == 20.0
    assert 60 / s.host_policies["api.company-information.service.gov.uk"].min_interval_s <= 100
    assert s.host_policies["haveibeenpwned.com"].min_interval_s >= 2.0
    assert s.default_host_interval_s >= 1.0


# ---- cache --------------------------------------------------------------------------


async def test_cache_hit_avoids_second_request(make_fetcher) -> None:
    rec = Recorder(site({"/about": (200, "<p>About us</p>")}))
    async with make_fetcher(rec) as f:
        first = await f.get_text("https://vendor.example/about")
        second = await f.get_text("https://vendor.example/about")
    assert rec.paths() == ["/robots.txt", "/about"]
    assert not first.from_cache
    assert second.from_cache
    assert second.text == first.text


async def test_cache_persists_across_fetchers(make_fetcher) -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"ok": True}))
    async with make_fetcher(rec) as f:
        await f.get_json("https://api.example/x")
    async with make_fetcher(rec) as f:
        assert await f.get_json("https://api.example/x") == {"ok": True}
    assert len(rec.requests) == 1


async def test_refresh_bypasses_cache(make_fetcher) -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={}))
    async with make_fetcher(rec) as f:
        await f.get_json("https://api.example/x")
        await f.get_json("https://api.example/x", refresh=True)
    assert len(rec.requests) == 2


async def test_post_cache_key_includes_body_but_not_headers(make_fetcher, settings) -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"q": r.content.decode()}))
    async with make_fetcher(rec) as f:
        await f.post_json("https://api.example/search", {"query": "a"}, headers={"X-K": "1"})
        await f.post_json("https://api.example/search", {"query": "a"}, headers={"X-K": "2"})
        await f.post_json("https://api.example/search", {"query": "b"})
    assert len(rec.requests) == 2
    stored = settings.cache_path.read_bytes()
    assert b"X-K" not in stored


async def test_server_errors_are_not_cached(make_fetcher) -> None:
    rec = Recorder(lambda r: httpx.Response(503))
    async with make_fetcher(rec) as f:
        with pytest.raises(FetchStatusError):
            await f.get_json("https://api.example/x")
        with pytest.raises(FetchStatusError):
            await f.get_json("https://api.example/x")
    assert len(rec.requests) == 6  # 3 attempts each, nothing cached


# ---- robots.txt ---------------------------------------------------------------------


async def test_robots_disallow_blocks_vendor_page(make_fetcher) -> None:
    rec = Recorder(site({"/private": (200, "secret")}, robots="User-agent: *\nDisallow: /private"))
    async with make_fetcher(rec) as f:
        with pytest.raises(RobotsDisallowed):
            await f.get_text("https://vendor.example/private")
    assert rec.paths() == ["/robots.txt"]


async def test_robots_missing_means_allowed(make_fetcher) -> None:
    rec = Recorder(site({"/": (200, "home")}, robots=None))
    async with make_fetcher(rec) as f:
        result = await f.get_text("https://vendor.example/")
    assert result.text == "home"


async def test_robots_server_error_means_disallowed(make_fetcher) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return httpx.Response(200, text="home")

    rec = Recorder(handler)
    async with make_fetcher(rec) as f:
        with pytest.raises(RobotsDisallowed):
            await f.get_text("https://vendor.example/")


async def test_robots_fetched_once_per_origin(make_fetcher) -> None:
    rec = Recorder(site({"/a": (200, "A"), "/b": (200, "B")}))
    async with make_fetcher(rec) as f:
        await asyncio.gather(
            f.get_text("https://vendor.example/a"), f.get_text("https://vendor.example/b")
        )
    assert rec.paths().count("/robots.txt") == 1


async def test_api_calls_skip_robots(make_fetcher) -> None:
    rec = Recorder(site({"/v1": (200, "{}")}, robots="User-agent: *\nDisallow: /"))
    async with make_fetcher(rec) as f:
        assert await f.get_json("https://api.example/v1") == {}
    assert rec.paths() == ["/v1"]


# ---- retries, errors, identity ------------------------------------------------------


async def test_retries_then_succeeds(make_fetcher, clock: FakeClock) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, json={"ok": 1})

    rec = Recorder(handler)
    async with make_fetcher(rec) as f:
        assert await f.get_json("https://api.example/x") == {"ok": 1}
    assert len(rec.requests) == 2
    assert 1.0 in clock.sleeps  # first backoff step


async def test_transport_error_raises_fetch_error_after_retries(make_fetcher) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    rec = Recorder(handler)
    async with make_fetcher(rec) as f:
        with pytest.raises(FetchError):
            await f.get_json("https://api.example/x")
    assert len(rec.requests) == 3  # 1 try + 2 retries


async def test_not_found_is_not_retried(make_fetcher) -> None:
    rec = Recorder(lambda r: httpx.Response(404))
    async with make_fetcher(rec) as f:
        result = await f.fetch_api("https://api.example/missing")
    assert result.status == 404
    assert len(rec.requests) == 1


async def test_default_client_identity_and_timeout(settings: Settings) -> None:
    from vet.fetch import Fetcher

    async with Fetcher(settings) as f:
        client = f._client
        assert client.headers["User-Agent"].startswith("vet/")
        assert client.timeout.read == 15.0
        assert client.timeout.connect == settings.connect_timeout_s


# ---- per-host policy and blocked hosts ----------------------------------------------


async def test_crtsh_gets_one_attempt_and_its_own_timeout(make_fetcher) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    async with make_fetcher(Recorder(handler)) as f:
        with pytest.raises(FetchError):
            await f.get_json("https://crt.sh/?q=example.com&output=json")
    assert len(seen) == 1
    assert seen[0].extensions["timeout"]["read"] == 20.0


@pytest.mark.parametrize(
    "url",
    ["https://www.northdata.de/Foo%20GmbH", "https://northdata.com/x", "https://a.northdata.de/"],
)
async def test_northdata_is_never_fetched(make_fetcher, url: str) -> None:
    from vet.fetch import BlockedHost

    rec = Recorder(lambda r: httpx.Response(200, text="should not be reached"))
    async with make_fetcher(rec) as f:
        with pytest.raises(BlockedHost):
            await f.get_text(url)
        with pytest.raises(BlockedHost):
            await f.get_json(url)
    assert rec.requests == []


async def test_cached_lookup_never_touches_network(make_fetcher) -> None:
    rec = Recorder(site({"/p": (200, "hello")}))
    async with make_fetcher(rec) as f:
        assert await f.cached("https://vendor.example/p") is None
        await f.get_text("https://vendor.example/p")
        n = len(rec.requests)
        hit = await f.cached("https://vendor.example/p")
    assert hit is not None and hit.text == "hello"
    assert len(rec.requests) == n


async def test_post_api_reports_cache_hits(make_fetcher) -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"results": []}))
    async with make_fetcher(rec) as f:
        first = await f.post_api("https://api.example/search", {"query": "x"})
        second = await f.post_api("https://api.example/search", {"query": "x"})
    assert not first.from_cache
    assert second.from_cache
