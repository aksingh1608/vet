"""Batch overview page: offline, built from an invented 3-row stackgini CSV."""

import json
import re
import shutil
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import FIXTURES
from vet import cli
from vet.config import Settings
from vet.overview import first_flag, render_overview
from vet.schemas import Axis, Finding, Polarity, RealAlive, Reason, Trust, Verdict

RUN_ID = "20261127-143000-abc123"
runner = CliRunner()


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.srcs: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        a = dict(attrs)
        if a.get("href"):
            self.hrefs.append(a["href"] or "")
        if a.get("src"):
            self.srcs.append(a["src"] or "")


def parsed(html: str) -> _Collect:
    p = _Collect()
    p.feed(html)
    return p


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(cache_path=tmp_path / "cache.sqlite", contact="test")


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / RUN_ID
    run.mkdir()
    shutil.copy(FIXTURES / "overview" / "stackgini.csv", run / "stackgini.csv")
    report = run / "report"
    report.mkdir()
    (report / "P1--larkspur-notes.example.html").write_text("<html></html>", encoding="utf-8")
    return run


def test_overview_counts_rows_and_links(run_dir: Path, settings: Settings) -> None:
    path = render_overview(run_dir, settings)
    assert path == run_dir / "report" / "overview.html"
    html = path.read_text(encoding="utf-8")
    page = parsed(html)

    # Top: counts as an inline SVG bar chart, total and run date.
    assert "svg" in page.tags and "rect" in page.tags
    assert "Green: 1, Yellow: 1, Red: 1" in html
    assert re.search(r'class="total">3<', html)
    assert "2026-11-27 14:30 UTC" in html

    # Rows: badge, URL, first flag of the row's colour, report link when it exists.
    assert "Domain registered 2009-03-14" in html
    assert "Listed on Companies House" not in html  # only the first green flag
    assert "Claims 50000 users on one page" in html
    assert "Have I Been Pwned lists a breach dated 2025-02-01" in html
    assert "P1--larkspur-notes.example.html" in page.hrefs
    assert sum(1 for h in page.hrefs if h.endswith(".html")) == 1  # P2, P3 have no page
    assert "https://larkspur-notes.example/" in page.hrefs
    assert "https://lookup.icann.org/en/lookup?name=larkspur-notes.example" in page.hrefs


def test_overview_escapes_text_and_renders_only_http_links(
    run_dir: Path, settings: Settings
) -> None:
    html = render_overview(run_dir, settings).read_text(encoding="utf-8")
    page = parsed(html)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "script" not in page.tags
    assert all(h.startswith(("https://", "http://")) or h.endswith(".html") for h in page.hrefs)
    assert not any("javascript" in h.lower() for h in page.hrefs)
    assert "javascript:alert(2)" in html  # shown as text, never as a link


def test_overview_is_self_contained(run_dir: Path, settings: Settings) -> None:
    html = render_overview(run_dir, settings).read_text(encoding="utf-8")
    page = parsed(html)
    assert page.srcs == []
    assert not {"script", "link", "iframe", "img"} & set(page.tags)
    assert "@import" not in html and "url(" not in html


def test_falls_back_to_verdicts_json(tmp_path: Path, settings: Settings) -> None:
    run = tmp_path / "manual-run"
    run.mkdir()
    reason = Reason(
        text="No Wayback captures before 2026",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.NEGATIVE,
        source_url="https://web.archive.org/web/*/thornwick.example",
        quote="",
        fact_id="age-2",
    )
    verdict = Verdict(
        product_url="https://thornwick.example/",
        real_alive=RealAlive.NO,
        trust=Trust.DO_NOT_TRUST,
        confidence=0.8,
        summary="s",
        reasons=[reason],
        findings=[
            Finding(
                text="Founding year contradicted by the register",
                source_url="https://thornwick.example/about",
                fact_id="cc-1",
            )
        ],
        run_id="manual-run",
        git_sha="x",
        model="fake",
        created_at=datetime(2026, 11, 27, tzinfo=UTC),
    )
    data = verdict.model_dump(mode="json")
    data["real_alive"] = settings.real_alive_labels[verdict.real_alive]
    data["trust"] = settings.trust_labels[verdict.trust]
    (run / "verdicts.json").write_text(json.dumps([data]), encoding="utf-8")

    html = render_overview(run, settings).read_text(encoding="utf-8")
    assert "Green: 0, Yellow: 0, Red: 1" in html
    assert "Founding year contradicted by the register" in html
    assert "from verdicts.json" in html


def test_no_verdict_file_is_a_clear_error(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(FileNotFoundError):
        render_overview(tmp_path, settings)


def test_first_flag_parsing() -> None:
    assert first_flag("None identified.") == ("", "")
    assert first_flag("A. (record: https://a.example/x) | B. (press: https://b.example)") == (
        "A.",
        "https://a.example/x",
    )
    assert first_flag("No link here") == ("No link here", "")


def test_cli_overview(run_dir: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: cls(contact="test")))
    result = runner.invoke(cli.app, ["overview", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert (run_dir / "report" / "overview.html").exists()
    missing = runner.invoke(cli.app, ["overview", str(run_dir / "nope")])
    assert missing.exit_code == 1
