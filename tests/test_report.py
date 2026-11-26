from datetime import UTC, datetime

import pytest

from vet.collectors.base import CollectorResult, SiteIdentity
from vet.output import ProductRecord, write_run
from vet.report import PublicExportRefused, render, safe_url
from vet.schemas import Axis, Finding, Polarity, RealAlive, Reason, Trust, Verdict
from vet.trace import Trace, Usage

NOW = datetime(2026, 11, 25, tzinfo=UTC)


def record(url: str = "https://acme.example/") -> ProductRecord:
    reason = Reason(
        text="<script>alert(1)</script> Domain registered 1994-07-30",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.POSITIVE,
        source_url="https://lookup.icann.org/en/lookup?name=acme.example",
        quote="1994-07-30",
        fact_id="age-0",
    )
    verdict = Verdict(
        product_url=url,
        real_alive=RealAlive.YES,
        trust=Trust.CAUTION,
        confidence=0.7,
        summary="Long history; one breach.",
        reasons=[reason] * 4,
        findings=[
            Finding(text="Breach in 2023", source_url="javascript:alert(1)", fact_id="risk-0")
        ],
        run_id="run-1",
        git_sha="abc",
        model="fake",
        created_at=NOW,
    )
    return ProductRecord(
        verdict=verdict,
        identity=SiteIdentity(vendor_name="Acme"),
        claims=[],
        facts=[],
        collectors=[CollectorResult(name="age", facts=[], seconds=1.2)],
        verification=[],
        judge_notes=[],
        used_llm_judge=True,
        usage=Usage(tavily_credits=7),
        seconds=42.0,
        errors=[],
    )


def test_safe_url() -> None:
    assert safe_url("https://x.example/a") == "https://x.example/a"
    assert safe_url("javascript:alert(1)") == "#"
    assert safe_url("data:text/html,hi") == "#"


def test_report_escapes_content_blocks_script_links_and_credits_hibp(tmp_path, settings) -> None:
    run = tmp_path / "run"
    write_run(run, [record()], [Trace(product_url="https://acme.example/")], settings)
    written = render(run, tmp_path / "site", labelled_dir=tmp_path / "none")
    page = (tmp_path / "site" / "acme.example.html").read_text("utf-8")
    index = (tmp_path / "site" / "index.html").read_text("utf-8")
    assert len(written) == 2
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert 'href="javascript:' not in page and 'href="#"' in page
    assert "https://lookup.icann.org/en/lookup?name=acme.example" in page
    for html in (page, index):
        assert 'Breach data: <a href="https://haveibeenpwned.com"' in html
        assert "Have I Been Pwned</a>" in html
    assert "acme.example.html" in index


def test_public_export_refuses_labelled_products(tmp_path, settings) -> None:
    labelled = tmp_path / "labelled"
    labelled.mkdir()
    (labelled / "examples.csv").write_text("url,label\nhttps://www.acme.example,trust\n")
    run = tmp_path / "run"
    write_run(run, [record()], [Trace(product_url="https://acme.example/")], settings)
    with pytest.raises(PublicExportRefused):
        render(run, tmp_path / "site", public=True, labelled_dir=labelled)
    # not public: allowed (local report)
    render(run, tmp_path / "site", public=False, labelled_dir=labelled)
    # public, product not labelled: allowed
    other = tmp_path / "run2"
    write_run(other, [record("https://other.example/")], [Trace(product_url="x")], settings)
    render(other, tmp_path / "site2", public=True, labelled_dir=labelled)
