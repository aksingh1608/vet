"""`vet demo`: live progress from trace events, the verdict panel, and replay. No network."""

import re

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from tests.conftest import FakeLLM
from tests.test_age import routes as age_routes
from tests.test_orchestrator import judge_response, support
from tests.test_report import record
from tests.test_site_reader import extract_response, site_routes
from vet import cli, demo, trace
from vet.judge import JudgeOut
from vet.orchestrator import Deps, run_product
from vet.output import write_run
from vet.schemas import Axis, Polarity, Reason
from vet.site_reader import _ExtractOut
from vet.trace import Trace
from vet.verify import _SupportOut

runner = CliRunner()


def text_of(renderable) -> str:  # noqa: ANN001
    console = Console(record=True, width=200, color_system=None)
    console.print(renderable)
    return console.export_text()


@pytest.fixture
def no_network(monkeypatch):
    async def refuse(self, request, **kwargs):  # noqa: ANN001
        raise AssertionError(f"network call in a test: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


@pytest.fixture
def out(monkeypatch) -> Console:
    console = Console(record=True, width=200, color_system=None)
    monkeypatch.setattr(cli, "console", console)
    return console


def test_trace_listener_sees_events_and_cannot_break_a_run() -> None:
    seen: list[dict] = []
    token = trace.set_listener(seen.append)
    try:
        t = Trace(product_url="https://acme.example/")
        t.emit({"event": "collector_start", "collector": "age"})
    finally:
        trace.reset_listener(token)
    assert seen[0]["product"] == "https://acme.example/"
    assert seen[0]["collector"] == "age"

    def boom(event: dict) -> None:
        raise RuntimeError("display broke")

    token = trace.set_listener(boom)
    try:
        t.emit({"event": "collector_end", "collector": "age", "seconds": 1.0, "error": ""})
    finally:
        trace.reset_listener(token)
    assert len(t.events) == 2  # the event was still recorded


async def test_progress_follows_the_offline_pipeline(router_fetcher) -> None:
    # Tavily is down, so footprint and legal_id fail; the rest succeeds from fixtures.
    fetcher, _ = router_fetcher([("api.tavily.com", 500), *age_routes(), *site_routes()])
    llm = FakeLLM({_ExtractOut: extract_response, JudgeOut: judge_response, _SupportOut: support})
    progress = demo.Progress()
    token = trace.set_listener(progress.handle)
    try:
        async with fetcher:
            deps = Deps(
                settings=fetcher.settings,
                fetcher=fetcher,
                llm=llm,
                run_id="demo-test",
                git_sha="abc",
                model="fake",
            )
            rec, _ = await run_product("ledgerly.example", deps)
    finally:
        trace.reset_listener(token)

    steps = progress.steps
    assert steps["site_reader"].state == demo.OK
    assert steps["age"].state == demo.OK
    assert steps["footprint"].state == demo.FAILED and steps["footprint"].detail
    assert steps[demo.JUDGE_STEP].state == demo.OK
    assert list(steps)[-1] == demo.JUDGE_STEP

    progress.finish(rec)
    shown = text_of(progress)
    assert "✓" in shown and "✗" in shown and "footprint" in shown
    assert all(re.search(r"\d+\.\ds", line) for line in shown.splitlines() if line.strip())


def test_finish_marks_collectors_cancelled_at_the_deadline() -> None:
    progress = demo.Progress()
    progress.handle({"event": "collector_start", "collector": "alive"})
    rec = record()
    rec.collectors.append(
        rec.collectors[0].model_copy(
            update={"name": "alive", "seconds": 90.0, "error": "cancelled at product deadline"}
        )
    )
    progress.finish(rec)
    assert progress.steps["alive"].state == demo.FAILED
    assert progress.steps["age"].state == demo.OK


def test_panel_shows_calls_reasons_links_and_never_obeys_markup() -> None:
    rec = record()
    sneaky = Reason(
        text="[green]trust[/green] Reviews on an independent site",
        axis=Axis.TRUST,
        polarity=Polarity.POSITIVE,
        source_url="https://reviews.example/acme",
        quote="x",
        fact_id="footprint-1",
    )
    rec.verdict.reasons = [sneaky, *rec.verdict.reasons[:3]]
    shown = text_of(demo.verdict_panel(rec))
    assert "Acme" in shown
    assert "real_alive  yes" in shown and "trust  caution" in shown
    assert "[green]trust[/green] Reviews" in shown  # printed literally, not styled
    assert "https://reviews.example/acme" in shown
    assert "https://lookup.icann.org/en/lookup?name=acme.example" in shown
    assert "Breach in 2023" in shown


def test_demo_from_run_replays_without_network(tmp_path, settings, no_network, out) -> None:
    run_dir = tmp_path / "run"
    write_run(run_dir, [record()], [], settings)
    result = runner.invoke(cli.app, ["demo", "--from-run", str(run_dir)])
    assert result.exit_code == 0, result.output
    shown = out.export_text()
    assert "✓" in shown and "age" in shown and "1.2s" in shown
    assert "real_alive  yes" in shown and "trust  caution" in shown


def test_demo_from_run_with_several_products_needs_the_url(tmp_path, settings, out) -> None:
    run_dir = tmp_path / "run"
    write_run(run_dir, [record(), record("https://other.example/")], [], settings)
    result = runner.invoke(cli.app, ["demo", "--from-run", str(run_dir)])
    assert result.exit_code == 1
    assert "2 products" in out.export_text()

    result = runner.invoke(cli.app, ["demo", "other.example", "--from-run", str(run_dir)])
    assert result.exit_code == 0
    assert "https://other.example/" in out.export_text()

    result = runner.invoke(cli.app, ["demo", "missing.example", "--from-run", str(run_dir)])
    assert result.exit_code == 1


def test_demo_needs_a_url_or_a_run(out) -> None:
    assert runner.invoke(cli.app, ["demo"]).exit_code == 2
    assert runner.invoke(cli.app, ["demo", "--from-run", "/no/such/dir"]).exit_code == 2


def test_demo_live_runs_the_pipeline_and_writes_the_run(
    tmp_path, settings, monkeypatch, no_network, out
) -> None:
    calls: list[list[str]] = []

    async def fake_run_many(urls, s, git_sha, run_id=None):  # noqa: ANN001
        calls.append(urls)
        t = Trace(product_url="https://acme.example/")
        trace.set_current(t)
        trace.emit({"event": "collector_start", "collector": "age"})
        trace.emit({"event": "collector_end", "collector": "age", "seconds": 1.2, "error": ""})
        trace.emit({"event": "claims_check"})
        t.emit({"event": "product_done"})
        trace.set_current(None)
        return "demo-run", [record()], [t]

    monkeypatch.setattr(demo, "run_many", fake_run_many)
    local = settings.model_copy(update={"runs_dir": tmp_path / "runs"})
    monkeypatch.setattr(cli, "_settings", lambda advanced, fmt: local)

    result = runner.invoke(cli.app, ["demo", "acme.example"])
    assert result.exit_code == 0, result.output
    assert calls == [["acme.example"]]
    run_dir = tmp_path / "runs" / "demo-run"
    assert (run_dir / "verdicts.json").exists()
    assert (run_dir / "products" / "acme.example.json").exists()
    assert (run_dir / cli.REPORT_DIR).is_dir()
    shown = out.export_text()
    assert "trust  caution" in shown and "age" in shown


def test_replay_times_judge_from_the_saved_trace(tmp_path, settings, no_network) -> None:
    rec = record()
    t = Trace(product_url=rec.verdict.product_url)
    t.events = [
        {"t": 0.1, "event": "collector_start", "collector": "age"},
        {"t": 1.3, "event": "collector_end", "collector": "age", "seconds": 1.2, "error": ""},
        {"t": 2.0, "event": "claims_check"},
        {"t": 9.5, "event": "product_done"},
    ]
    run_dir = tmp_path / "run"
    write_run(run_dir, [rec], [t], settings)
    console = Console(record=True, width=200, color_system=None)
    demo.replay(rec, console, run_dir)
    judge_line = next(ln for ln in console.export_text().splitlines() if demo.JUDGE_STEP in ln)
    assert "7.5s" in judge_line

    console = Console(record=True, width=200, color_system=None)
    demo.replay(rec, console, tmp_path / "no-trace")
    judge_line = next(ln for ln in console.export_text().splitlines() if demo.JUDGE_STEP in ln)
    assert "✓" in judge_line and "0.0s" not in judge_line


def test_demo_validates_the_verdict_file_like_vet_run(
    tmp_path, settings, monkeypatch, no_network, out
) -> None:
    # record()'s finding links to javascript:, which the verdict-file check rejects.
    async def fake_run_many(urls, s, git_sha, run_id=None, run_dir=None):  # noqa: ANN001
        return run_id or "same-run", [record()], [Trace(product_url="https://acme.example/")]

    monkeypatch.setattr(demo, "run_many", fake_run_many)
    monkeypatch.setattr("vet.orchestrator.run_many", fake_run_many)
    local = settings.model_copy(update={"runs_dir": tmp_path / "runs"})
    monkeypatch.setattr(cli, "_settings", lambda advanced, fmt: local)

    def problems(command: list[str]) -> list[str]:
        start = len(out.export_text(clear=False))
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 0, result.output
        lines = out.export_text(clear=False)[start:].splitlines()
        return [ln.strip() for ln in lines if ln.strip().startswith(("✗", "✓ verdict file"))]

    from_demo = problems(["demo", "acme.example"])
    from_run = problems(["run", "acme.example"])
    assert from_demo == from_run
    assert from_demo == ["✗ verdict 0 (https://acme.example/): finding 0 has no openable link"]
