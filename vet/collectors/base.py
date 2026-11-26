"""Collector plumbing: the context every collector receives and the wrapper that guarantees a
failing or slow collector returns no facts plus a logged error instead of crashing the run."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from vet import trace
from vet.config import Settings
from vet.fetch import Fetcher
from vet.llm import StructuredLLM
from vet.schemas import Claim, Fact
from vet.tavily import Tavily

logger = logging.getLogger(__name__)


class SiteIdentity(BaseModel):
    vendor_name: str = ""
    product_name: str = ""
    legal_entity: str = ""
    register_court: str = ""
    register_number: str = ""
    country: str = ""


@dataclass
class Context:
    url: str
    domain: str
    settings: Settings
    fetcher: Fetcher
    llm: StructuredLLM
    tavily: Tavily | None
    claims: list[Claim] = field(default_factory=list)
    identity: SiteIdentity = field(default_factory=SiteIdentity)
    homepage_text: str = ""

    def now(self) -> datetime:
        return datetime.now(UTC)


class CollectorResult(BaseModel):
    name: str
    facts: list[Fact]
    seconds: float
    error: str = ""


Collector = Callable[[Context], Awaitable[list[Fact]]]


async def run_collector(
    name: str, collect: Collector, ctx: Context, timeout_s: float
) -> CollectorResult:
    """Run one collector under a timeout. Never raises (except cancellation)."""
    started = time.monotonic()
    trace.emit({"event": "collector_start", "collector": name})
    error = ""
    facts: list[Fact] = []
    try:
        async with asyncio.timeout(timeout_s):
            facts = await collect(ctx)
    except TimeoutError:
        error = f"timed out after {timeout_s:.0f}s"
    except Exception as exc:  # noqa: BLE001 - graceful failure is a hard rule of the spec
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("collector %s failed for %s", name, ctx.url, exc_info=True)
    seconds = round(time.monotonic() - started, 2)
    if error:
        logger.warning("collector %s: %s", name, error)
    trace.emit(
        {
            "event": "collector_end",
            "collector": name,
            "facts": len(facts),
            "seconds": seconds,
            "error": error,
        }
    )
    return CollectorResult(name=name, facts=facts, seconds=seconds, error=error)


def fact_id(collector: str, n: int) -> str:
    return f"{collector}-{n}"
