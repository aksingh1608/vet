import json

from tests.conftest import FakeLLM, fixture_text
from tests.test_age import routes as age_routes
from tests.test_site_reader import extract_response, site_routes
from vet.judge import JudgeOut
from vet.orchestrator import Deps, normalize_url, run_product
from vet.output import validate_verdict_file, write_run
from vet.schemas import RealAlive, Trust
from vet.site_reader import _ExtractOut
from vet.verify import _Support, _SupportOut


def judge_response(user: str) -> JudgeOut:
    data = json.loads(user.removeprefix("<data>\n").removesuffix("\n</data>"))
    ids = [f["id"] for f in data["facts"]]
    return JudgeOut(
        real_alive=RealAlive.YES,
        trust=Trust.TRUST,
        confidence=0.7,
        summary="Long-lived domain; the site tries to steer AI evaluators.",
        reason_fact_ids=ids,
        findings=["The about page contains hidden text addressed to AI evaluators."],
    )


def support(user: str) -> _SupportOut:
    items = json.loads(user.removeprefix("<data>\n").removesuffix("\n</data>"))
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
