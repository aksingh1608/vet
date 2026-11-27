"""Tavily search through the polite fetcher.

Every response is cached in the fetch cache, keyed by the request body (query + parameters;
the API key travels in a header and is never part of the key), so reruns cost zero credits.
Credits are counted per product in the current trace.
"""

import json
import logging
from typing import Any

from pydantic import BaseModel

from vet import trace
from vet.config import Settings
from vet.fetch import Fetcher, FetchStatusError
from vet.sources import is_search_tool_url

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"
DEFAULT_MAX_RESULTS = 5


class TavilyResult(BaseModel):
    url: str
    title: str = ""
    content: str = ""
    score: float = 0.0


class Tavily:
    def __init__(self, settings: Settings, fetcher: Fetcher) -> None:
        if settings.tavily_api_key is None:
            raise ValueError("TAVILY_API_KEY is not set")
        self._settings = settings
        self._fetcher = fetcher
        self._headers = {"Authorization": f"Bearer {settings.tavily_api_key.get_secret_value()}"}
        # url -> snippet content; the verifier may use these for snippet-only domains.
        self.snippets: dict[str, str] = {}

    async def usage(self) -> tuple[int, int | None]:
        """(credits used, credit limit). The key's own limit when it has one, else the account
        plan's. Never cached: credits change."""
        data = await self._fetcher.get_json(TAVILY_USAGE_URL, headers=self._headers, refresh=True)
        return parse_usage(data)

    async def search(
        self,
        query: str,
        *,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        topic: str = "general",
    ) -> list[TavilyResult]:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": self._settings.tavily_search_depth,
            "max_results": max_results,
            "topic": topic,
        }
        if include_domains:
            payload["include_domains"] = sorted(include_domains)
        if exclude_domains:
            payload["exclude_domains"] = sorted(exclude_domains)

        result = await self._fetcher.post_api(TAVILY_SEARCH_URL, payload, headers=self._headers)
        credits = 0 if result.from_cache else self._settings.tavily_credits_per_search
        current = trace.current()
        if current is not None:
            current.usage.tavily_searches += 1
            current.usage.tavily_credits += credits
            if result.from_cache:
                current.usage.tavily_cache_hits += 1
        trace.emit(
            {
                "event": "tavily",
                "query": query,
                "include_domains": include_domains,
                "cache": "hit" if result.from_cache else "miss",
                "credits": credits,
                "status": result.status,
            }
        )
        if not result.ok:
            raise FetchStatusError(TAVILY_SEARCH_URL, result.status)

        data = json.loads(result.text)
        results = [TavilyResult.model_validate(r) for r in data.get("results", [])]
        # Tavily's own pages (generated company profiles) are not sources and have no page
        # behind them to link to: drop them before any collector sees them.
        dropped = [r.url for r in results if is_search_tool_url(r.url)]
        if dropped:
            logger.debug("dropped %d search-tool result(s): %s", len(dropped), dropped)
            results = [r for r in results if not is_search_tool_url(r.url)]
        for r in results:  # every returned URL, so computed facts can show Tavily found it
            self.snippets.setdefault(r.url, "")
            if r.content:
                self.snippets[r.url] = r.content
        return results


def parse_usage(data: Any) -> tuple[int, int | None]:
    key = data.get("key") if isinstance(data, dict) else None
    account = data.get("account") if isinstance(data, dict) else None
    if isinstance(key, dict) and key.get("limit") is not None:
        return int(key.get("usage") or 0), int(key["limit"])
    if isinstance(account, dict) and account.get("plan_limit") is not None:
        return int(account.get("plan_usage") or 0), int(account["plan_limit"])
    if isinstance(key, dict) and "usage" in key:
        return int(key["usage"] or 0), None
    raise ValueError("unexpected /usage answer from Tavily")
