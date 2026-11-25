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
