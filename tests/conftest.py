from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from vet.config import Settings
from vet.fetch import FetchCache, Fetcher


class FakeClock:
    """Monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Recorder:
    """httpx MockTransport handler that records every request it receives."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []
        self.clock: FakeClock | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.clock is not None:
            self.times.append(self.clock.now)
        return self.handler(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(cache_path=tmp_path / "cache.sqlite", contact="test")


@pytest.fixture
def make_fetcher(settings: Settings, clock: FakeClock):
    clients: list[httpx.AsyncClient] = []

    def _make(recorder: Recorder, cache_path: Path | None = None) -> Fetcher:
        recorder.clock = clock
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        clients.append(client)
        cache = FetchCache(cache_path or settings.cache_path)
        return Fetcher(settings, client=client, cache=cache, clock=clock, sleep=clock.sleep)

    return _make


FIXTURES = Path(__file__).parent / "fixtures"
FAKE_TAVILY_TOKEN = "tvly-test-not-real"  # noqa: S105 - placeholder, never sent anywhere
FAKE_CH_TOKEN = "ch-test-not-real"  # noqa: S105 - placeholder, never sent anywhere


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Router:
    """Mock transport: the first route whose substring occurs in the URL answers.

    robots.txt allows everything unless a route says otherwise; anything else is a 404.
    """

    def __init__(self, routes: list[tuple[str, object]]) -> None:
        self.routes = routes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, answer in self.routes:
            if needle in url:
                if callable(answer):
                    return answer(request)
                if isinstance(answer, httpx.Response):
                    return answer
                if isinstance(answer, int):
                    return httpx.Response(answer)
                if isinstance(answer, str):
                    return httpx.Response(200, text=answer)
                return httpx.Response(200, json=answer)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(404, text="not found")


class FakeLLM:
    """Stands in for vet.llm.LLM. Responders map an output type to a function of the prompt."""

    def __init__(self, responders: dict[type, Callable[[str], object]] | None = None) -> None:
        self.responders = dict(responders or {})
        self.calls: list[tuple[str, str]] = []

    async def structured(self, *, kind, system, user, output, step, max_tokens=8000):  # noqa: ANN001
        self.calls.append((step, user))
        if output not in self.responders:
            from vet.llm import LLMError

            raise LLMError(f"no fake responder for {output.__name__}")
        return self.responders[output](user)


@pytest.fixture
def keyed_settings(tmp_path: Path) -> Settings:
    from pydantic import SecretStr

    return Settings(
        cache_path=tmp_path / "cache.sqlite",
        contact="test",
        tavily_api_key=SecretStr(FAKE_TAVILY_TOKEN),
        companies_house_api_key=SecretStr(FAKE_CH_TOKEN),
    )


@pytest.fixture
def router_fetcher(keyed_settings: Settings, clock: FakeClock):
    """Build a Fetcher over a Router; returns (fetcher, recorder)."""

    def _make(routes: list[tuple[str, object]]) -> tuple[Fetcher, Recorder]:
        rec = Recorder(Router(routes))
        rec.clock = clock
        client = httpx.AsyncClient(transport=httpx.MockTransport(rec), follow_redirects=True)
        fetcher = Fetcher(
            keyed_settings,
            client=client,
            cache=FetchCache(keyed_settings.cache_path),
            clock=clock,
            sleep=clock.sleep,
        )
        return fetcher, rec

    return _make
