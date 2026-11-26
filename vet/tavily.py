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

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
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
        for r in results:
            if r.content:
                self.snippets[r.url] = r.content
        return results
