"""Buyer view: batch survival, the stackgini layout, labels and eval for ``overall``.

All companies are invented.
"""

import asyncio
from pathlib import Path

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from tests.conftest import FakeLLM
from tests.test_schemas import make_verdict
from vet import orchestrator, trace
from vet.buyer_view import NO_FLAGS, baseline_overall, baseline_view
from vet.collectors.base import SiteIdentity
from vet.config import Settings
from vet.evaluate import overall_table, score
from vet.labels import load_labels
from vet.llm import LLM, LLMError
from vet.output import (
    STACKGINI_CSV,
    InputRow,
    ProductRecord,
    load_records,
    read_inputs,
    validate_stackgini_file,
    write_run,
)
from vet.schemas import Overall, RealAlive, Trust


def ok_record(row: InputRow, verdict_kw: dict | None = None) -> ProductRecord:
    verdict = make_verdict().model_copy(
        update={"product_url": orchestrator.normalize_url(row.url), **(verdict_kw or {})}
    )
    return ProductRecord(
        verdict=verdict,
        identity=SiteIdentity(),
        claims=[],
        facts=[],
        collectors=[],
        verification=[],
        judge_notes=[],
        used_llm_judge=True,
        usage=trace.Usage(),
        seconds=1.0,
        errors=[],
        buyer=baseline_view(verdict, [], [], input_id=row.id, url=row.url),
        input=row,
    )


@pytest.fixture
def stackgini(settings: Settings) -> Settings:
    return settings.model_copy(update={"output_format": "stackgini"})


async def test_one_failing_product_never_ends_the_batch(tmp_path, stackgini, monkeypatch):
    rows = [
        InputRow(url="https://alpha-ledger.example/", id="a1"),
        InputRow(url="https://broken-crm.example/", id="b2"),
        InputRow(url="https://gamma-hr.example/", id="c3"),
    ]

    async def fake_run_product(url, deps, row=None):  # noqa: ANN001
        if "broken" in url:
            # The judge raises something that is not an LLMError (e.g. an SDK error).
            raise KeyError("judge exploded")
        return ok_record(row), trace.Trace(product_url=url)

    monkeypatch.setattr(orchestrator, "run_product", fake_run_product)
    monkeypatch.setattr(orchestrator, "LLM", lambda settings, cache=None: FakeLLM())
    run_dir = tmp_path / "run"
    _, records, traces = await orchestrator.run_many(
        rows, stackgini, "sha", run_id="r1", run_dir=run_dir
    )

    assert [r.input.id for r in records] == ["a1", "b2", "c3"]
    failed = records[1]
    assert failed.buyer.overall == Overall.YELLOW
    assert failed.buyer.method == "failed"
    assert failed.buyer.categories == ["not_enough_evidence"]
    assert "Not checked" in failed.buyer.reasoning
    assert failed.verdict.real_alive == RealAlive.UNCLEAR
    assert failed.verdict.trust == Trust.CAUTION
    # Each product was saved the moment it finished, before the batch returned.
    assert len(load_records(run_dir)) == 3

    path = write_run(run_dir, records, traces, stackgini)
    assert path.name == STACKGINI_CSV
    assert validate_stackgini_file(path, stackgini, rows) == []


async def test_cancellation_still_propagates(tmp_path, stackgini, monkeypatch):
    async def slow(url, deps, row=None):  # noqa: ANN001
        await asyncio.sleep(10)

    monkeypatch.setattr(orchestrator, "run_product", slow)
    monkeypatch.setattr(orchestrator, "LLM", lambda settings, cache=None: FakeLLM())
    task = asyncio.create_task(
        orchestrator.run_many([InputRow(url="https://slow.example/")], stackgini, "sha")
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class _Out(BaseModel):
    ok: bool


class _FailingMessages:
    async def parse(self, **kwargs):  # noqa: ANN003
        request = httpx.Request("POST", "https://api.anthropic.example/v1/messages")
        raise anthropic.APIConnectionError(request=request)


class _FailingClient:
    messages = _FailingMessages()


async def test_sdk_errors_become_llm_errors(settings: Settings) -> None:
    llm = LLM(settings, client=_FailingClient())  # type: ignore[arg-type]
    with pytest.raises(LLMError):
        await llm.structured(kind="fast", system="s", user="u", output=_Out, step="t")


def test_baseline_mapping() -> None:
    v = make_verdict()
    assert baseline_overall(v.model_copy(update={"real_alive": RealAlive.NO})) == Overall.RED
    assert baseline_overall(v.model_copy(update={"trust": Trust.DO_NOT_TRUST})) == Overall.RED
    good = v.model_copy(update={"real_alive": RealAlive.YES, "trust": Trust.TRUST})
    assert baseline_overall(good) == Overall.GREEN
    assert baseline_overall(v) == Overall.YELLOW  # yes + caution


def test_stackgini_rows_keep_input_order_and_validate(tmp_path, stackgini) -> None:
    rows = [InputRow(url="https://zeta.example/", id="9"), InputRow(url="https://eta.example/")]
    records = [ok_record(r) for r in rows]
    path = write_run(tmp_path, records, [], stackgini)
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(stackgini.stackgini_columns)
    assert validate_stackgini_file(path, stackgini, rows) == []
    assert (tmp_path / "stackgini.json").exists()
    # Wrong order and a missing row are both reported.
    assert validate_stackgini_file(path, stackgini, list(reversed(rows)))
    assert validate_stackgini_file(path, stackgini, rows[:1])


def test_validator_rejects_bad_label_search_links_and_empty_reasoning(tmp_path, stackgini):
    path = tmp_path / STACKGINI_CSV
    header = ",".join(stackgini.stackgini_columns)
    path.write_text(
        f"{header}\n"
        '1,https://a.example/,,,Amber,"Fine. (press: https://tavily.com/x)",'
        f"{NO_FLAGS},{NO_FLAGS},\n",
        encoding="utf-8",
    )
    problems = " ".join(validate_stackgini_file(path, stackgini))
    assert "overall 'Amber'" in problems
    assert "search tool" in problems
    assert "no reasoning" in problems


def test_read_inputs_txt_and_csv(tmp_path: Path) -> None:
    txt = tmp_path / "in.txt"
    txt.write_text("# blind\nhttps://one.example/\n\nhttps://two.example/\n", encoding="utf-8")
    assert [r.url for r in read_inputs(txt)] == ["https://one.example/", "https://two.example/"]
    csv_in = tmp_path / "in.csv"
    csv_in.write_text(
        'id,url,product,vendor\n7,https://three.example/,"Three, Pro",Three Ltd\n',
        encoding="utf-8",
    )
    (row,) = read_inputs(csv_in)
    assert (row.id, row.product, row.vendor) == ("7", "Three, Pro", "Three Ltd")


def test_overall_labels_and_score(tmp_path: Path, settings: Settings) -> None:
    labels_csv = tmp_path / "labels.csv"
    labels_csv.write_text(
        "id,url,product,vendor,overall,green_flags,yellow_flags,red_flags,reasoning\n"
        '1,https://red-one.example/,R,R Co,Red,,,"Regulator order.\nSecond line",Bad.\n'
        "2,https://amber-two.example/,A,A Co,amber,,Renamed twice,,Hold.\n"
        "3,https://green-three.example/,G,G Co,green,Wide adoption,,,Fine.\n",
        encoding="utf-8",
    )
    labels = load_labels(labels_csv, settings)
    assert [lb.overall for lb in labels] == [Overall.RED, Overall.YELLOW, Overall.GREEN]
    assert labels[0].flags["red"].startswith("Regulator order.")

    def rec(label, ra, tr):  # noqa: ANN001
        row = InputRow(url=label.url, id=label.id)
        return ok_record(row, {"real_alive": ra, "trust": tr})

    records = [
        rec(labels[0], RealAlive.YES, Trust.TRUST),  # Red called Green
        rec(labels[1], RealAlive.YES, Trust.CAUTION),  # Yellow, correct
        rec(labels[2], RealAlive.NO, Trust.TRUST),  # Green called Red
    ]
    result = score(records, labels)
    assert (result.overall.correct, result.overall.total) == (1, 3)
    assert result.red_recall == "0/1"
    assert result.false_reds == 1
    assert result.overall_stat("red", "green") == 1
    assert len(result.overall_misses) == 2
    assert overall_table(result)[0] == "overall: 1/3 (33%)"


def test_tavily_usage_prefers_key_limit_then_plan_limit() -> None:
    from vet.tavily import parse_usage

    plan = {"plan_usage": 770, "plan_limit": 1000}
    assert parse_usage({"key": {"usage": 5, "limit": 50}, "account": plan}) == (5, 50)
    assert parse_usage({"key": {"usage": 770, "limit": None}, "account": plan}) == (770, 1000)
    assert parse_usage({"key": {"usage": 3}}) == (3, None)


def test_old_record_without_buyer_view_gets_the_baseline_row(tmp_path, stackgini) -> None:
    record = ok_record(InputRow(url="https://older.example/")).model_copy(
        update={"buyer": None, "input": None}
    )
    path = write_run(tmp_path, [record], [], stackgini)
    assert validate_stackgini_file(path, stackgini) == []


def test_report_shows_the_traffic_light_first(tmp_path, stackgini) -> None:
    from vet.report import render

    record = ok_record(InputRow(url="https://shown.example/", id="5"))
    write_run(tmp_path / "run", [record], [], stackgini)
    pages = render(tmp_path / "run", tmp_path / "out")
    html = pages[0].read_text(encoding="utf-8")
    assert html.index("Buyer action") < html.index("Real &amp; alive")


def test_a_failed_product_is_visible_everywhere(tmp_path, stackgini, monkeypatch, caplog):
    """The per-product catch stays (batch survival), but the failure is never silent: ERROR log
    with the exception type, a trace event, the record's errors, the buyer reasoning and the
    CLI summary all show it."""
    import logging

    from rich.console import Console
    from typer.testing import CliRunner

    from vet import cli

    async def fake_run_product(url, deps, row=None):  # noqa: ANN001
        if "broken" in url:
            raise KeyError("judge exploded")
        return ok_record(row), trace.Trace(product_url=url)

    monkeypatch.setattr(orchestrator, "run_product", fake_run_product)
    monkeypatch.setattr(orchestrator, "LLM", lambda settings, cache=None: FakeLLM())
    local = stackgini.model_copy(update={"runs_dir": tmp_path / "runs"})
    monkeypatch.setattr(cli, "_settings", lambda advanced, fmt: local)
    console = Console(record=True, width=200, color_system=None)
    monkeypatch.setattr(cli, "console", console)
    inputs = tmp_path / "in.csv"
    inputs.write_text(
        "id,url\n1,https://fine-books.example/\n2,https://broken-crm.example/\n", encoding="utf-8"
    )

    with caplog.at_level(logging.ERROR, logger="vet.orchestrator"):
        result = CliRunner().invoke(cli.app, ["run", "--file", str(inputs)])
    assert result.exit_code == 0, result.output

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("broken-crm" in r.getMessage() and "KeyError" in r.getMessage() for r in errors)
    shown = console.export_text()
    assert "1 of 2 products failed" in shown and "broken-crm.example" in shown

    (run_dir,) = (tmp_path / "runs").iterdir()
    failed = next(r for r in load_records(run_dir) if r.input.id == "2")
    assert failed.errors and failed.errors[0].startswith("run: KeyError")
    assert "run failed: KeyError" in failed.buyer.reasoning
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"event": "product_failed"' in trace_text and "KeyError" in trace_text


def test_buyer_only_collectors_run_only_with_the_categoriser(settings) -> None:
    assert not settings.buyer_categorise  # the baseline mapping ships by default
    assert "corporate" not in orchestrator.collectors_after_site(settings)
    on = settings.model_copy(update={"buyer_categorise": True})
    assert "corporate" in orchestrator.collectors_after_site(on)
    assert "identity" in orchestrator.collectors_after_site(settings)


async def test_vet_credits_never_reads_usage_from_the_cache(router_fetcher) -> None:
    from vet.tavily import Tavily

    usage = '{"key": {"usage": 5, "limit": null}, "account": {"plan_usage": 5, "plan_limit": 9}}'
    fetcher, rec = router_fetcher([("api.tavily.com/usage", usage)])
    async with fetcher:
        tavily = Tavily(fetcher.settings, fetcher)
        assert await tavily.usage() == (5, 9)
        assert await tavily.usage() == (5, 9)
    assert len([r for r in rec.requests if r.url.path == "/usage"]) == 2  # both went out
