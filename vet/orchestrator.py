"""One product, end to end, inside a time budget.

age starts at once (domain only) in parallel with the site reader; legal_id, footprint, risk,
alive, looks, identity and archive start when the site has been read. Collectors that are
still running when the collection deadline passes are cancelled and recorded as timed out.
Then claims_check, the judge, verification and reason selection always run.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vet import trace
from vet.buyer_view import baseline_view, failed_view
from vet.categorise import categorised_view
from vet.claims_check import check_claims
from vet.collectors import (
    age,
    alive,
    archive,
    corporate,
    footprint,
    identity,
    legal_id,
    looks,
    risk,
)
from vet.collectors.base import (
    Collector,
    CollectorResult,
    Context,
    SiteIdentity,
    run_collector,
)
from vet.config import Settings
from vet.fetch import Fetcher
from vet.judge import judge, relabel_after_verification
from vet.llm import LLM, LLMCache, StructuredLLM
from vet.output import InputRow, ProductRecord, write_record
from vet.schemas import RealAlive, Trust, Verdict
from vet.site_reader import SiteReport, read_site
from vet.tavily import Tavily
from vet.text import normalize_url, registered_domain
from vet.verify import verify_and_select

logger = logging.getLogger(__name__)

# Seconds kept back from the product budget for judge + verification.
JUDGE_AND_VERIFY_RESERVE_S = 60.0
MIN_COLLECT_WINDOW_S = 20.0
# Collectors that need the site reader's output (claims, identity, pages).
AFTER_SITE: dict[str, Collector] = {
    "legal_id": legal_id.collect,
    "footprint": footprint.collect,
    "risk": risk.collect,
    "alive": alive.collect,
    "looks": looks.collect,
    "identity": identity.collect,
    "archive": archive.collect,
    "corporate": corporate.collect,
}
# Collectors whose facts feed the buyer view only, never the two-axis judge.
BUYER_ONLY_COLLECTORS = frozenset({"corporate"})


def collectors_after_site(settings: Settings) -> dict[str, Collector]:
    """Buyer-only collectors run only when the categorised buyer view is on (credits, time)."""
    if settings.buyer_categorise:
        return dict(AFTER_SITE)
    return {k: v for k, v in AFTER_SITE.items() if k not in BUYER_ONLY_COLLECTORS}


@dataclass
class Deps:
    settings: Settings
    fetcher: Fetcher
    llm: StructuredLLM
    run_id: str
    git_sha: str
    model: str


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


def not_checked(results: list[CollectorResult], report: SiteReport | None) -> list[str]:
    """What we could not read, and why, in words a buyer understands (our problem, not the
    vendor's)."""
    out = [f"{r.name} ({r.error})" for r in results if r.error]
    if report is not None:
        out += [e for e in report.errors if any(k in e for k in ("403", "robots", "timed out"))]
    return out[:5]


async def run_product(
    url: str, deps: Deps, row: InputRow | None = None
) -> tuple[ProductRecord, trace.Trace]:
    row = row or InputRow(url=url)
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

    tasks: dict[str, asyncio.Task[CollectorResult]] = {
        "age": asyncio.create_task(run_collector("age", age.collect, ctx, timeout))
    }
    report, site_result = await _site(ctx, settings.site_reader_timeout_s)
    if report is not None:
        ctx.claims, ctx.identity, ctx.pages = report.claims, report.identity, report.pages
    # Run even when the homepage is unreadable (e.g. a WAF answers 403): search and breach
    # records need only the domain, and the domain name stands in for the vendor name.
    for name, collect in collectors_after_site(settings).items():
        tasks[name] = asyncio.create_task(run_collector(name, collect, ctx, timeout))
    window = max(deadline - time.monotonic(), MIN_COLLECT_WINDOW_S)
    done, pending = await asyncio.wait(tasks.values(), timeout=window)
    for task in pending:
        task.cancel()
    results = [site_result]
    for name, task in tasks.items():
        if task in done:
            results.append(task.result())
        else:
            results.append(
                CollectorResult(
                    name=name,
                    facts=[],
                    seconds=round(window, 2),
                    error="cancelled at product deadline",
                )
            )

    facts = [f for r in results for f in r.facts]
    names = {ctx.identity.vendor_name, ctx.identity.product_name, ctx.domain.split(".")[0]}
    claim_checks, check_facts = check_claims(
        ctx.claims, facts, ctx.domain, names, datetime.now(UTC)
    )
    facts += check_facts
    trace.emit(
        {
            "event": "claims_check",
            "checks": [c.model_dump() for c in claim_checks],
            "facts": [f.id for f in check_facts],
        }
    )
    judge_facts = [f for f in facts if f.collector not in BUYER_ONLY_COLLECTORS]
    judgement = await judge(
        deps.llm,
        ctx.claims,
        judge_facts,
        claim_checks,
        settings.breach_recent_years,
        settings.label_thresholds,
    )
    by_id = {f.id: f for f in facts}
    ranked = [by_id[i] for i in judgement.ranked_fact_ids if i in by_id]
    snippets = tavily.snippets if tavily else {}
    selection, checks = await verify_and_select(
        ranked,
        judge_facts,
        judgement.finding_fact_ids,
        deps.fetcher,
        deps.llm,
        snippets,
        settings,
        ctx.domain,
        frozenset(judgement.finding_only_ids),
    )
    reasons = selection.reasons
    failed = {c.fact_id for c in checks if not c.passed}
    judgement = relabel_after_verification(
        judgement, judge_facts, failed, settings.breach_recent_years, settings.label_thresholds
    )

    verdict = Verdict(
        product_url=url,
        real_alive=judgement.real_alive,
        trust=judgement.trust,
        confidence=judgement.confidence,
        summary=judgement.summary,
        reasons=reasons,
        findings=selection.findings,
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
    buyer = baseline_view(
        verdict,
        facts,
        not_checked(results, report),
        input_id=row.id,
        product=row.product or ctx.identity.product_name,
        vendor=row.vendor or ctx.identity.vendor_name,
        url=row.url,
    )
    buyer_notes: list[str] = []
    if settings.buyer_categorise:
        try:
            buyer, buyer_notes, extra = await categorised_view(
                verdict=verdict,
                facts=facts,
                verification=checks,
                identity=ctx.identity,
                not_checked=buyer.not_checked,
                llm=deps.llm,
                fetcher=deps.fetcher,
                snippets=snippets,
                settings=settings,
                base=buyer,
                today=datetime.now(UTC),
            )
            checks = [*checks, *extra]
        except Exception as exc:  # noqa: BLE001 - the baseline view is the fallback
            logger.warning("categoriser failed for %s; baseline view kept: %s", url, exc)
            buyer_notes = [f"categoriser failed ({type(exc).__name__}); baseline mapping used"]
        t.emit({"event": "buyer_view", "overall": buyer.overall.value, "notes": buyer_notes})
    seconds = round(time.monotonic() - started, 1)  # including the buyer view
    record = ProductRecord(
        verdict=verdict,
        identity=ctx.identity,
        claims=ctx.claims,
        claim_checks=claim_checks,
        facts=facts,
        collectors=results,
        verification=checks,
        judge_notes=judgement.notes,
        selection_notes=selection.notes + buyer_notes,
        used_llm_judge=judgement.used_llm,
        usage=t.usage,
        seconds=seconds,
        errors=errors,
        buyer=buyer,
        input=row,
    )
    trace.set_current(None)
    return record, t


def failed_record(row: InputRow, deps: Deps, error: str, seconds: float) -> ProductRecord:
    """A product whose run raised: unclear / caution, buyer view Yellow, says what failed."""
    url = normalize_url(row.url)
    verdict = Verdict(
        product_url=url,
        real_alive=RealAlive.UNCLEAR,
        trust=Trust.CAUTION,
        confidence=0.0,
        summary=f"The run failed before a verdict could be reached ({error[:160]}).",
        reasons=[],
        findings=[],
        run_id=deps.run_id,
        git_sha=deps.git_sha,
        model=deps.model,
        created_at=datetime.now(UTC),
    )
    return ProductRecord(
        verdict=verdict,
        identity=SiteIdentity(),
        claims=[],
        facts=[],
        collectors=[],
        verification=[],
        judge_notes=[f"run failed: {error}"],
        used_llm_judge=False,
        usage=trace.Usage(),
        seconds=seconds,
        errors=[f"run: {error}"],
        buyer=failed_view(row.url, error, input_id=row.id, product=row.product, vendor=row.vendor),
        input=row,
    )


async def run_product_safe(
    row: InputRow, deps: Deps, run_dir: Path | None = None
) -> tuple[ProductRecord, trace.Trace]:
    """run_product that never raises (except cancellation). The record is written to
    ``run_dir/products/`` as soon as the product is done, so a crash later loses nothing."""
    started = time.monotonic()
    try:
        record, t = await run_product(row.url, deps, row)
    except Exception as exc:  # noqa: BLE001 - one product must never end the batch
        logger.exception(
            "product %s failed (%s); writing a fallback record", row.url, type(exc).__name__
        )
        trace.set_current(None)
        error = f"{type(exc).__name__}: {exc}"
        record = failed_record(row, deps, error, round(time.monotonic() - started, 1))
        t = trace.Trace(product_url=normalize_url(row.url))
        t.emit({"event": "product_failed", "error": error})
    if run_dir is not None:
        try:
            await asyncio.to_thread(write_record, run_dir, record)
        except OSError:
            logger.exception("could not save the record for %s", row.url)
    return record, t


async def run_many(
    urls: Sequence[str | InputRow],
    settings: Settings,
    git_sha: str,
    run_id: str | None = None,
    run_dir: Path | None = None,
) -> tuple[str, list[ProductRecord], list[trace.Trace]]:
    """Run every input, at most ``max_parallel_products`` at once. One product failing never
    ends the batch. With ``run_dir``, each record is saved the moment it is done."""
    run_id = run_id or new_run_id()
    rows = [u if isinstance(u, InputRow) else InputRow(url=u) for u in urls]
    llm = LLM(settings, cache=LLMCache(settings.cache_path) if settings.llm_cache else None)
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

        async def one(row: InputRow) -> tuple[ProductRecord, trace.Trace]:
            async with sem:
                return await run_product_safe(row, deps, run_dir)

        pairs = await asyncio.gather(*(one(r) for r in rows))
    return run_id, [p[0] for p in pairs], [p[1] for p in pairs]
