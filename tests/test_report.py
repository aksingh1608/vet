import re
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest

from vet.collectors.base import CollectorResult, SiteIdentity
from vet.output import ProductRecord, write_run
from vet.report import PublicExportRefused, render, safe_url
from vet.schemas import (
    Axis,
    Claim,
    ClaimCheck,
    Fact,
    Finding,
    Polarity,
    RealAlive,
    Reason,
    SourceKind,
    Trust,
    Verdict,
)
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


# ---- presentation: self-contained page, evidence links only ----------------------------------


class _Links(HTMLParser):
    """Every URL the browser would load or follow: href and src attributes, unescaped."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.urls |= {v for k, v in attrs if k in ("href", "src") and v}


def external_urls(html: str) -> set[str]:
    parser = _Links()
    parser.feed(html)
    return {u for u in parser.urls if re.match(r"(?i)^[a-z][a-z0-9+.-]*:|^//", u)}


def evidence_record() -> ProductRecord:
    base = record()
    registered = Fact(
        id="age-0", collector="age", statement="acme.example was registered on 1994-07-30",
        axis=Axis.REAL_ALIVE, polarity=Polarity.POSITIVE, weight=0.5,
        source_url="https://lookup.icann.org/en/lookup?name=acme.example", quote="1994-07-30",
        source_kind=SourceKind.RECORD, fetched_at=NOW, topic="domain_registered",
        observed_at=datetime(1994, 7, 30, tzinfo=UTC),
    )  # fmt: skip
    breach = Fact(
        id="risk-0", collector="risk", statement="Have I Been Pwned lists a breach of Acme",
        axis=Axis.TRUST, polarity=Polarity.NEGATIVE, weight=0.6, is_hard_flag=True,
        source_url="https://haveibeenpwned.com/PwnedWebsites#Acme", quote="Acme",
        source_kind=SourceKind.RECORD, fetched_at=NOW, topic="breach",
    )  # fmt: skip
    claim = Claim(id="c1", type="founded_year", text="Vendor says it was founded in 1990",
                  source_url="https://acme.example/about", quote="since 1990")  # fmt: skip
    check = ClaimCheck(claim_id="c1", status="contradicted", fact_ids=["age-0"], note="n")
    return base.model_copy(
        update={"facts": [registered, breach], "claims": [claim], "claim_checks": [check]}
    )


def test_report_is_self_contained_and_links_only_to_evidence(tmp_path, settings) -> None:
    rec = evidence_record()
    run = tmp_path / "run"
    write_run(run, [rec], [Trace(product_url="https://acme.example/")], settings)
    render(run, tmp_path / "site", labelled_dir=tmp_path / "none")
    v = rec.verdict
    evidence = {r.source_url for r in v.reasons} | {f.source_url for f in v.findings}
    evidence |= {f.source_url for f in rec.facts} | {c.source_url for c in rec.claims}
    allowed = evidence | {"https://haveibeenpwned.com", v.product_url}
    for name in ("acme.example.html", "index.html"):
        html = (tmp_path / "site" / name).read_text("utf-8")
        assert external_urls(html) <= allowed, name
        for loader in ("<script", "<link", "<img", "<iframe", "@import", "url(", "src="):
            assert loader not in html, (name, loader)
    page = (tmp_path / "site" / "acme.example.html").read_text("utf-8")
    assert "Red flags" in page and "Claims vs records" in page
    assert 'class="contradicted"' in page and "✗" in page
    assert "1994-07-30" in page
    assert "Not on record: first certificate, first archive capture" in page


def test_overview_links_the_name_to_the_detail_page(tmp_path, settings) -> None:
    run = tmp_path / "run"
    write_run(run, [record()], [Trace(product_url="https://acme.example/")], settings)
    render(run, tmp_path / "site", labelled_dir=tmp_path / "none")
    index = (tmp_path / "site" / "index.html").read_text("utf-8")
    assert '<a class="name" href="acme.example.html">acme.example</a>' in index
    assert 'href="https://acme.example/"' in index and "visit site" in index


def test_no_red_flags_section_without_red_flags(tmp_path, settings) -> None:
    run = tmp_path / "run"
    write_run(run, [record()], [Trace(product_url="https://acme.example/")], settings)
    render(run, tmp_path / "site", labelled_dir=tmp_path / "none")
    assert "Red flags" not in (tmp_path / "site" / "acme.example.html").read_text("utf-8")
