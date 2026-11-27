import json

from tests.conftest import FakeLLM, fixture_text
from tests.test_age import routes as age_routes
from tests.test_site_reader import extract_response, site_routes
from vet.judge import JudgeOut
from vet.llm import unwrap
from vet.orchestrator import Deps, normalize_url, run_product
from vet.output import validate_verdict_file, write_run
from vet.schemas import RealAlive, Trust
from vet.site_reader import _ExtractOut
from vet.verify import _Support, _SupportOut


def judge_response(user: str) -> JudgeOut:
    data = json.loads(unwrap(user))
    ids = [f["id"] for f in data["facts"]]
    return JudgeOut(
        real_alive=RealAlive.YES,
        trust=Trust.TRUST,
        confidence=0.7,
        summary="Long-lived domain; the site tries to steer AI evaluators.",
        reason_fact_ids=ids,
        finding_fact_ids=[i for i in ids if i.startswith("guard")],
    )


def support(user: str) -> _SupportOut:
    items = json.loads(unwrap(user))
    return _SupportOut(items=[_Support(id=i["id"], supported=True) for i in items])


async def test_product_run_survives_failing_collectors(router_fetcher, tmp_path) -> None:
    # Tavily is down: footprint and legal_id both fail; the run must still finish.
    routes = [("api.tavily.com", 500), *age_routes(), *site_routes()]
    fetcher, _ = router_fetcher(routes)
    llm = FakeLLM({_ExtractOut: extract_response, JudgeOut: judge_response, _SupportOut: support})
    async with fetcher:
        deps = Deps(
            settings=fetcher.settings,
            fetcher=fetcher,
            llm=llm,
            run_id="test-run",
            git_sha="abc123",
            model="fake",
        )
        record, t = await run_product("ledgerly.example", deps)

    errors = {c.name: c.error for c in record.collectors}
    assert errors["site_reader"] == ""
    assert errors["age"] == ""
    assert errors["footprint"] and errors["legal_id"]  # failed, logged, not raised

    v = record.verdict
    assert v.product_url == "https://ledgerly.example/"
    assert v.trust == Trust.DO_NOT_TRUST or v.trust == Trust.CAUTION  # injection hard flag caps it
    assert 4 <= len(v.reasons) <= 7
    assert any("AI systems" in r.text for r in v.reasons)
    assert any(e["event"] == "product_done" for e in t.events)

    path = write_run(tmp_path / "run", [record], [t], fetcher.settings)
    assert validate_verdict_file(path, fetcher.settings) == []
    assert (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8").count("\n") > 5
    assert (tmp_path / "run" / "products" / "ledgerly.example.json").exists()


def test_normalize_url() -> None:
    assert normalize_url(" example.com ") == "https://example.com/"
    assert normalize_url("http://X.example/a#top") == "http://x.example/a"


def test_fixture_sanity() -> None:
    assert "HRB 123456" in fixture_text("site/impressum.html")


async def test_a_collector_that_times_out_does_not_break_the_run(
    router_fetcher, monkeypatch
) -> None:
    import asyncio

    from vet import orchestrator

    async def stuck(ctx):  # noqa: ANN001
        await asyncio.sleep(30)
        return []

    monkeypatch.setitem(orchestrator.AFTER_SITE, "alive", stuck)
    routes = [("api.tavily.com", 500), *age_routes(), *site_routes()]
    fetcher, _ = router_fetcher(routes)
    settings = fetcher.settings.model_copy(update={"collector_timeout_s": 0.2})
    llm = FakeLLM({_ExtractOut: extract_response, JudgeOut: judge_response, _SupportOut: support})
    async with fetcher:
        deps = Deps(
            settings=settings, fetcher=fetcher, llm=llm, run_id="t", git_sha="x", model="fake"
        )
        record, _ = await run_product("ledgerly.example", deps)
    errors = {c.name: c.error for c in record.collectors}
    assert errors["alive"].startswith("timed out")
    assert errors["age"] == "" and errors["looks"] == ""
    assert {c.name for c in record.collectors} >= {"site_reader", "age", "risk", "alive", "looks"}
    assert len(record.verdict.reasons) >= 4


async def test_blind_round_input_columns_reach_the_output_row(router_fetcher, tmp_path) -> None:
    """The organiser's CSV has id,url,product,vendor: all four reach the verdict row."""
    from vet.output import InputRow, buyer_to_row

    routes = [("api.tavily.com", 500), *age_routes(), *site_routes()]
    fetcher, _ = router_fetcher(routes)
    llm = FakeLLM({_ExtractOut: extract_response, JudgeOut: judge_response, _SupportOut: support})
    row = InputRow(url="ledgerly.example", id="B7", product="Ledgerly Books", vendor="Ledgerly Ltd")
    async with fetcher:
        deps = Deps(
            settings=fetcher.settings,
            fetcher=fetcher,
            llm=llm,
            run_id="test-run",
            git_sha="abc123",
            model="fake",
        )
        record, _ = await run_product(row.url, deps, row)
    out = buyer_to_row(record, fetcher.settings)
    assert (out["id"], out["url"], out["product"], out["vendor"]) == (
        "B7",
        "ledgerly.example",
        "Ledgerly Books",
        "Ledgerly Ltd",
    )
