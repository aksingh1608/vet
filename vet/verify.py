"""Verify every reason before it ships.

For each candidate fact, in the judge's order:
1. Source check. Quoted facts: the quote must appear on the source page (from cache, else
   fetched politely). Snippet-only domains (North Data): the quote must appear in the cached
   Tavily snippet for that URL. Computed facts (no quote): the source URL must resolve.
2. Support check (fast model, one batch): does the quote support the sentence without
   overclaiming? Computed facts skip this; their sentence is generated from the record.
Failing reasons are dropped; the next-ranked facts fill in until there are MIN_REASONS.
"""

import asyncio
import json
import logging

from pydantic import BaseModel

from vet.config import Settings
from vet.fetch import Fetcher, FetchError, FetchResult, host_matches, host_of
from vet.llm import LLMError, StructuredLLM, load_prompt
from vet.schemas import MAX_REASONS, MIN_REASONS, Fact, Reason, SourceKind
from vet.text import contains_quote

logger = logging.getLogger(__name__)

# Record APIs and archives: verified without robots.txt (they are APIs, not vendor sites).
API_HOSTS = ("rdap.org", "crt.sh", "web.archive.org", "api.company-information.service.gov.uk")
FIRST_BATCH = 10


class VerifyResult(BaseModel):
    fact_id: str
    source_ok: bool
    supported: bool | None = None
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.source_ok and self.supported is not False


class _Support(BaseModel):
    id: str
    supported: bool


class _SupportOut(BaseModel):
    items: list[_Support]


def reason_from(fact: Fact) -> Reason:
    return Reason(
        text=fact.statement,
        axis=fact.axis,
        polarity=fact.polarity,
        source_url=fact.source_url,
        quote=fact.quote,
        fact_id=fact.id,
    )


async def _load(fetcher: Fetcher, fact: Fact) -> FetchResult | None:
    cached = await fetcher.cached(fact.source_url)
    if cached is not None:
        return cached
    host = host_of(fact.source_url)
    if fact.source_kind == SourceKind.RECORD and host_matches(host, API_HOSTS):
        return await fetcher.fetch_api(fact.source_url)
    return await fetcher.get_text(fact.source_url)


async def check_source(
    fact: Fact, fetcher: Fetcher, snippets: dict[str, str], settings: Settings
) -> VerifyResult:
    host = host_of(fact.source_url)
    if host_matches(host, settings.blocked_domains):
        snippet = snippets.get(fact.source_url, "")
        ok = bool(fact.quote) and contains_quote(snippet, fact.quote)
        return VerifyResult(
            fact_id=fact.id,
            source_ok=ok,
            note="quote found in cached Tavily snippet" if ok else "quote not in Tavily snippet",
        )
    try:
        result = await _load(fetcher, fact)
    except FetchError as exc:
        return VerifyResult(fact_id=fact.id, source_ok=False, note=f"source unreachable: {exc}")
    if result is None or not result.ok:
        status = result.status if result else "none"
        return VerifyResult(fact_id=fact.id, source_ok=False, note=f"source status {status}")
    if not fact.quote:
        return VerifyResult(
            fact_id=fact.id, source_ok=True, supported=None, note="computed fact; source resolves"
        )
    if contains_quote(result.text, fact.quote):
        return VerifyResult(fact_id=fact.id, source_ok=True, note="quote found on source page")
    snippet = snippets.get(fact.source_url, "")
    if snippet and contains_quote(snippet, fact.quote):
        # The page changed or renders client-side; the quote is only in the search snippet.
        return VerifyResult(
            fact_id=fact.id, source_ok=False, note="quote only in search snippet, not on the page"
        )
    return VerifyResult(fact_id=fact.id, source_ok=False, note="quote not found on source page")


def needs_support_check(fact: Fact) -> bool:
    """Human-written quotes get the model check. Record facts from an API (RDAP date, crt.sh
    timestamp) carry a machine value as quote; their sentence is generated from it in code."""
    if not fact.quote:
        return False
    is_api_record = fact.source_kind == SourceKind.RECORD and host_matches(
        host_of(fact.source_url), API_HOSTS
    )
    return not is_api_record


async def check_support(llm: StructuredLLM, facts: list[Fact]) -> dict[str, bool]:
    items = [{"id": f.id, "sentence": f.statement, "quote": f.quote} for f in facts if f.quote]
    if not items:
        return {}
    out = await llm.structured(
        kind="fast",
        system=load_prompt("verify_reason.md"),
        user=f"<data>\n{json.dumps(items, ensure_ascii=False)}\n</data>",
        output=_SupportOut,
        step="verify_support",
    )
    return {i.id: i.supported for i in out.items}


async def _verify_batch(
    facts: list[Fact],
    fetcher: Fetcher,
    llm: StructuredLLM,
    snippets: dict[str, str],
    settings: Settings,
) -> dict[str, VerifyResult]:
    checks = await asyncio.gather(*(check_source(f, fetcher, snippets, settings) for f in facts))
    results = {r.fact_id: r for r in checks}
    to_judge = [f for f in facts if results[f.id].source_ok and needs_support_check(f)]
    try:
        support = await check_support(llm, to_judge)
    except LLMError as exc:
        logger.warning("support check failed (%s); keeping source-verified reasons", exc)
        support = {}
    for f in to_judge:
        verdict = support.get(f.id)
        results[f.id].supported = verdict
        if verdict is False:
            results[f.id].note += "; quote does not support the sentence"
    return results


async def verify_reasons(
    ranked: list[Fact],
    fetcher: Fetcher,
    llm: StructuredLLM,
    snippets: dict[str, str],
    settings: Settings,
) -> tuple[list[Reason], list[VerifyResult]]:
    """Return up to MAX_REASONS verified reasons in rank order, plus every check made."""
    checked: dict[str, VerifyResult] = {}
    kept: list[Fact] = []
    remaining = list(ranked)
    batch_size = FIRST_BATCH
    first = True
    while remaining and (first or len(kept) < MIN_REASONS):
        first = False
        batch, remaining = remaining[:batch_size], remaining[batch_size:]
        checked |= await _verify_batch(batch, fetcher, llm, snippets, settings)
        kept += [f for f in batch if checked[f.id].passed]
        batch_size = max(MIN_REASONS - len(kept), 1) * 2
    kept = kept[:MAX_REASONS]
    return [reason_from(f) for f in kept], list(checked.values())
