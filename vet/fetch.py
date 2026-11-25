"""The only way vet talks to the network.

Two kinds of calls:

* Web pages (``get_page`` / ``get_text``): GET only, robots.txt respected, per-host rate limit.
* APIs (``get_json`` / ``post_json``): no robots.txt check, but the same per-host rate limit,
  timeouts, retries, cache and trace logging. POST exists only for APIs that require it.

Every call is cached in SQLite keyed by method + URL (+ a hash of the request body for POST).
Request headers and bodies are never stored or traced, so API keys never reach disk or logs.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from vet.config import Settings

logger = logging.getLogger(__name__)

ROBOTS_AGENT = "vet"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 10.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
TraceHook = Callable[[dict[str, Any]], None]


class FetchError(RuntimeError):
    """A request could not be completed."""


class RobotsDisallowed(FetchError):
    """robots.txt forbids fetching this URL."""


class FetchStatusError(FetchError):
    """The server answered with a non-success status."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.url = url
        self.status = status


class FetchResult(BaseModel):
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    text: str
    fetched_at: datetime
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Page(BaseModel):
    result: FetchResult
    text: str  # main text extracted with trafilatura ("" if nothing extractable)


class RateLimiter:
    """Spaces request starts to the same host by at least that host's interval."""

    def __init__(
        self,
        default_interval: float,
        intervals: dict[str, float] | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._default = default_interval
        self._intervals = dict(intervals or {})
        self._clock = clock
        self._sleep = sleep
        self._next_slot: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def interval_for(self, host: str) -> float:
        return self._intervals.get(host, self._default)

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            slot = self._next_slot.get(host, now)
            if slot > now:
                await self._sleep(slot - now)
            self._next_slot[host] = max(slot, self._clock()) + self.interval_for(host)


class FetchCache:
    """SQLite response cache. Calls are synchronous; Fetcher runs them in a worker thread."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS responses (
            key TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            final_url TEXT NOT NULL,
            status INTEGER NOT NULL,
            headers TEXT NOT NULL,
            text TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, key: str) -> FetchResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT url, final_url, status, headers, text, fetched_at "
                "FROM responses WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        url, final_url, status, headers, text, fetched_at = row
        return FetchResult(
            url=url,
            final_url=final_url,
            status=status,
            headers=json.loads(headers),
            text=text,
            fetched_at=datetime.fromisoformat(fetched_at),
            from_cache=True,
        )

    def put(self, key: str, result: FetchResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO responses "
                "(key, url, final_url, status, headers, text, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    result.url,
                    result.final_url,
                    result.status,
                    json.dumps(result.headers),
                    result.text,
                    result.fetched_at.isoformat(),
                ),
            )


def cache_key(method: str, url: str, body: bytes | None = None) -> str:
    key = f"{method.upper()} {url}"
    if body:
        key += " " + hashlib.sha256(body).hexdigest()
    return key


def host_of(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise FetchError(f"URL has no host: {url}")
    return host.lower()


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache: FetchCache | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        trace: TraceHook | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.fetch_timeout_s, connect=settings.connect_timeout_s),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._owns_client = client is None
        self._cache = cache or FetchCache(settings.cache_path)
        self._sleep = sleep
        self._trace = trace
        self.limiter = RateLimiter(
            settings.default_host_interval_s, settings.host_intervals, clock=clock, sleep=sleep
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- public: web pages (GET only, robots respected) -------------------------------

    async def get_text(self, url: str, *, refresh: bool = False) -> FetchResult:
        await self._check_robots(url)
        result = await self._request("GET", url, refresh=refresh)
        _raise_for_status(result)
        return result

    async def get_page(self, url: str, *, refresh: bool = False) -> Page:
        result = await self.get_text(url, refresh=refresh)
        text = await asyncio.to_thread(_extract_main_text, result.text, result.final_url)
        return Page(result=result, text=text)

    # ---- public: APIs (no robots check) ------------------------------------------------

    async def get_json(
        self, url: str, *, headers: dict[str, str] | None = None, refresh: bool = False
    ) -> Any:
        result = await self._request("GET", url, headers=headers, refresh=refresh)
        _raise_for_status(result)
        return json.loads(result.text)

    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        refresh: bool = False,
    ) -> Any:
        body = json.dumps(payload, sort_keys=True).encode()
        merged = {"Content-Type": "application/json", **(headers or {})}
        result = await self._request("POST", url, headers=merged, body=body, refresh=refresh)
        _raise_for_status(result)
        return json.loads(result.text)

    async def fetch_api(
        self, url: str, *, headers: dict[str, str] | None = None, refresh: bool = False
    ) -> FetchResult:
        """Raw API GET that returns any status (e.g. to tell 404 from an outage)."""
        return await self._request("GET", url, headers=headers, refresh=refresh)

    # ---- internals ---------------------------------------------------------------------

    async def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            parser = self._robots.get(origin)
            if parser is None:
                parser = await self._load_robots(origin)
                self._robots[origin] = parser
        if not parser.can_fetch(ROBOTS_AGENT, url):
            self._emit({"event": "robots_disallowed", "url": url})
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    async def _load_robots(self, origin: str) -> urllib.robotparser.RobotFileParser:
        """RFC 9309: 4xx means no restrictions; 5xx or unreachable means disallow all."""
        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            result = await self._request("GET", robots_url)
        except FetchError:
            logger.warning("robots.txt unreachable for %s; treating as disallow-all", origin)
            parser.disallow_all = True
            return parser
        if result.ok:
            parser.parse(result.text.splitlines())
        elif 400 <= result.status < 500:
            parser.allow_all = True
        else:
            parser.disallow_all = True
        return parser

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        refresh: bool = False,
    ) -> FetchResult:
        key = cache_key(method, url, body)
        if not refresh:
            cached = await asyncio.to_thread(self._cache.get, key)
            if cached is not None:
                self._emit(
                    {
                        "event": "fetch",
                        "method": method,
                        "url": url,
                        "cache": "hit",
                        "status": cached.status,
                    }
                )
                return cached

        host = host_of(url)
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            await self.limiter.wait(host)
            started = time.monotonic()
            try:
                response = await self._client.request(method, url, headers=headers, content=body)
            except httpx.HTTPError as exc:
                last_error = exc
                self._emit(
                    {
                        "event": "fetch_error",
                        "method": method,
                        "url": url,
                        "attempt": attempt,
                        "error": type(exc).__name__,
                    }
                )
            else:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                self._emit(
                    {
                        "event": "fetch",
                        "method": method,
                        "url": url,
                        "cache": "miss",
                        "status": response.status_code,
                        "attempt": attempt,
                        "ms": elapsed_ms,
                    }
                )
                if response.status_code not in RETRYABLE_STATUSES:
                    result = FetchResult(
                        url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        headers=dict(response.headers),
                        text=response.text,
                        fetched_at=datetime.now(UTC),
                    )
                    await asyncio.to_thread(self._cache.put, key, result)
                    return result
                last_error = FetchStatusError(url, response.status_code)
                if attempt == self.settings.max_retries:
                    return FetchResult(
                        url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        headers=dict(response.headers),
                        text=response.text,
                        fetched_at=datetime.now(UTC),
                    )
                retry_after = _retry_after_seconds(response)
                if retry_after is not None:
                    await self._sleep(retry_after)
                    continue
            if attempt < self.settings.max_retries:
                await self._sleep(self.settings.backoff_base_s * 2**attempt)

        raise FetchError(f"{method} {url} failed after retries: {last_error!r}")

    def _emit(self, event: dict[str, Any]) -> None:
        if self._trace is not None:
            self._trace(event)
        logger.debug("fetch event: %s", event)


def _raise_for_status(result: FetchResult) -> None:
    if not result.ok:
        raise FetchStatusError(result.url, result.status)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "")
    try:
        return min(float(raw), MAX_RETRY_AFTER_S)
    except ValueError:
        return None


def _extract_main_text(html: str, url: str) -> str:
    import trafilatura

    return trafilatura.extract(html, url=url, include_comments=False) or ""
