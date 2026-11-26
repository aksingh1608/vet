"""One product, end to end, inside a time budget.

age starts at once (domain only) in parallel with the site reader; legal_id and footprint
start when the claims are in. Collectors that are still running when the collection deadline
passes are cancelled and recorded as timed out; the judge and verifier always run.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from vet import trace
from vet.collectors import age, footprint, legal_id
from vet.collectors.base import CollectorResult, Context, run_collector
from vet.config import Settings
from vet.fetch import Fetcher
from vet.judge import judge
from vet.llm import LLM, StructuredLLM
from vet.output import ProductRecord
from vet.schemas import Verdict
from vet.site_reader import SiteReport, read_site
from vet.tavily import Tavily
from vet.text import registered_domain
from vet.verify import verify_reasons

logger = logging.getLogger(__name__)

# Seconds kept back from the product budget for judge + verification.
JUDGE_AND_VERIFY_RESERVE_S = 60.0
MIN_COLLECT_WINDOW_S = 20.0


@dataclass
class Deps:
    settings: Settings
    fetcher: Fetcher
    llm: StructuredLLM
    run_id: str
    git_sha: str
    model: str


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


async def _site(ctx: Context, timeout_s: float) -> tuple[SiteReport | None, CollectorResult]:
    started = time.monotonic()
    trace.emit({"event": "collector_start", "collector": "site_reader"})
    error = ""
    report: SiteReport | None = None
    try:
        async with asyncio.timeout(timeout_s):
            report = await read_site(ctx)
    except TimeoutError:
        error = f"timed out after {timeout_s:.0f}s"
    except Exception as exc:  # noqa: BLE001 - graceful failure is a hard rule
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("site reader failed for %s", ctx.url, exc_info=True)
    seconds = round(time.monotonic() - started, 2)
    facts = report.facts if report else []
    trace.emit(
        {
            "event": "collector_end",
            "collector": "site_reader",
            "facts": len(facts),
            "seconds": seconds,
            "error": error,
        }
    )
    return report, CollectorResult(name="site_reader", facts=facts, seconds=seconds, error=error)


async def run_product(url: str, deps: Deps) -> tuple[ProductRecord, trace.Trace]:
    url = normalize_url(url)
    t = trace.Trace(product_url=url)
    trace.set_current(t)
    started = time.monotonic()
    settings = deps.settings
    deadline = started + settings.product_budget_s - JUDGE_AND_VERIFY_RESERVE_S
    tavily = Tavily(settings, deps.fetcher) if settings.tavily_api_key else None
    ctx = Context(
        url=url,
        domain=registered_domain(url),
        settings=settings,
        fetcher=deps.fetcher,
        llm=deps.llm,
        tavily=tavily,
    )
    timeout = settings.collector_timeout_s

    age_task = asyncio.create_task(run_collector("age", age.collect, ctx, timeout))
    report, site_result = await _site(ctx, timeout)
    if report is not None:
        ctx.claims, ctx.identity = report.claims, report.identity
    tasks = [age_task]
    if report is not None:
        tasks += [
            asyncio.create_task(run_collector("legal_id", legal_id.collect, ctx, timeout)),
            asyncio.create_task(run_collector("footprint", footprint.collect, ctx, timeout)),
        ]
    window = max(deadline - time.monotonic(), MIN_COLLECT_WINDOW_S)
    done, pending = await asyncio.wait(tasks, timeout=window)
    for task in pending:
        task.cancel()
    results = [site_result] + [task.result() for task in tasks if task in done]
    names = ["age", "legal_id", "footprint"]
    for task, name in zip(tasks, names, strict=False):
        if task in pending:
            results.append(
                CollectorResult(
                    name=name,
                    facts=[],
                    seconds=round(window, 2),
                    error="cancelled at product deadline",
                )
            )

    facts = [f for r in results for f in r.facts]
    judgement = await judge(deps.llm, ctx.claims, facts)
    by_id = {f.id: f for f in facts}
    ranked = [by_id[i] for i in judgement.ranked_fact_ids if i in by_id]
    snippets = tavily.snippets if tavily else {}
    reasons, checks = await verify_reasons(ranked, deps.fetcher, deps.llm, snippets, settings)

    verdict = Verdict(
        product_url=url,
        real_alive=judgement.real_alive,
        trust=judgement.trust,
        confidence=judgement.confidence,
        summary=judgement.summary,
        reasons=reasons,
        findings=judgement.findings,
        run_id=deps.run_id,
        git_sha=deps.git_sha,
        model=deps.model,
        created_at=datetime.now(UTC),
    )
    seconds = round(time.monotonic() - started, 1)
    t.emit(
        {
            "event": "product_done",
            "seconds": seconds,
            "reasons": len(reasons),
            "usage": t.usage.model_dump(),
        }
    )
    errors = [f"{r.name}: {r.error}" for r in results if r.error]
    if report is not None:
        errors += report.errors
    record = ProductRecord(
        verdict=verdict,
        identity=ctx.identity,
        claims=ctx.claims,
        facts=facts,
        collectors=results,
        verification=checks,
        judge_notes=judgement.notes,
        used_llm_judge=judgement.used_llm,
        usage=t.usage,
        seconds=seconds,
        errors=errors,
    )
    trace.set_current(None)
    return record, t


async def run_many(
    urls: list[str], settings: Settings, git_sha: str, run_id: str | None = None
) -> tuple[str, list[ProductRecord], list[trace.Trace]]:
    run_id = run_id or new_run_id()
    llm = LLM(settings)
    sem = asyncio.Semaphore(settings.max_parallel_products)
    async with Fetcher(settings) as fetcher:
        deps = Deps(
            settings=settings,
            fetcher=fetcher,
            llm=llm,
            run_id=run_id,
            git_sha=git_sha,
            model=f"{settings.judge_model} + {settings.fast_model}",
        )

        async def one(u: str) -> tuple[ProductRecord, trace.Trace]:
            async with sem:
                return await run_product(u, deps)

        pairs = await asyncio.gather(*(one(u) for u in urls))
    return run_id, [p[0] for p in pairs], [p[1] for p in pairs]
