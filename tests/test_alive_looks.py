from datetime import UTC, datetime

from tests.conftest import FakeLLM, fixture_text
from vet.collectors import alive, looks
from vet.collectors.base import Context, SiteIdentity, SitePage
from vet.schemas import Polarity, SourceKind

NOW = datetime(2026, 11, 25, tzinfo=UTC)
HOME = "https://ledgerly.example/"


def page(kind: str, raw: str = "", text: str = "", **kw: object) -> SitePage:
    return SitePage(kind=kind, url=f"{HOME}{kind}", raw=raw, text=text or raw, **kw)


def ctx_for(fetcher, pages: list[SitePage]) -> Context:
    return Context(
        url=HOME,
        domain="ledgerly.example",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=FakeLLM(),
        tavily=None,
        identity=SiteIdentity(vendor_name="Ledgerly"),
        pages=pages,
    )


def test_find_dates_in_english_and_german() -> None:
    text = "v4.2 - 2026-10-03. Release 12. März 2026. March 3, 2025. 01.02.2024. 2031-01-01"
    found = {d.date().isoformat() for d, _ in alive.find_dates(text, NOW)}
    assert found == {"2026-10-03", "2026-03-12", "2025-03-03", "2024-02-01"}  # no future date


def test_stale_changelog_is_a_frozen_signal_recent_one_cannot_help() -> None:
    stale = page("changelog", text="Version 3.1 released 2023-04-02. Version 3.0 2022-11-01.")
    fresh = page("blog", text="Posted November 12, 2026")
    facts = alive.page_date_facts([stale, fresh], NOW)
    frozen = next(f for f in facts if f.topic == "frozen")
    assert frozen.polarity == Polarity.NEGATIVE and frozen.quote == "2023-04-02"
    recent = next(f for f in facts if f.topic == "release_activity")
    # vendor-sourced: the judge's rules never count it in the vendor's favour
    assert recent.source_kind == SourceKind.VENDOR


async def test_github_activity_from_a_matching_account(router_fetcher) -> None:
    fetcher, rec = router_fetcher(
        [("api.github.com/users/ledgerly-hq/repos", fixture_text("api/github_repos.json"))]
    )
    home = page(
        "home",
        raw='<a href="https://github.com/ledgerly-hq">GitHub</a>'
        '<a href="https://github.com/torvalds">famous</a>'
        '<a href="https://github.com/features">features</a>',
    )
    async with fetcher:
        facts = await alive.collect(ctx_for(fetcher, [home]))
    gh = next(f for f in facts if "github" in f.source_url)
    assert gh.source_url == "https://github.com/ledgerly-hq/ledgerly-sdk"  # not a fork/archive
    assert gh.quote == "2026-11-02T12:30:00Z" and gh.source_kind == SourceKind.RECORD
    assert gh.check_url.startswith("https://api.github.com/users/ledgerly-hq/repos")
    assert not any("torvalds" in str(r.url) for r in rec.requests)  # not this vendor's account


def test_linked_accounts_need_a_matching_name(router_fetcher) -> None:
    raw = (
        '<a href="https://pypi.org/project/ledgerly-client/">pypi</a>'
        '<a href="https://www.npmjs.com/package/@ledgerly/sdk">npm</a>'
        '<a href="https://pypi.org/project/requests/">borrowed</a>'
        '<a href="https://apps.apple.com/de/app/ledgerly/id1234567890">app</a>'
    )
    fetcher, _ = router_fetcher([])
    found = alive.linked_accounts([page("home", raw=raw)], ctx_for(fetcher, []))
    assert found["pypi"] == "ledgerly-client"
    assert found["npm"] == "@ledgerly/sdk"
    assert found["appstore"] == "1234567890"


def test_looks_facts_are_low_weight_filler() -> None:
    home = page(
        "home",
        raw='<meta name="generator" content="Framer 2f1a"><p>Lorem ipsum dolor sit amet</p>',
        text="Lorem ipsum dolor sit amet",
    )
    builder = looks.builder_fact(home, NOW)
    placeholder = looks.placeholder_facts([home], NOW)[0]
    assert builder is not None and "Framer" in builder.statement
    for f in (builder, placeholder):
        assert f.is_filler and f.weight <= 0.1 and f.collector == "looks"
    docs = [page("docs", status=404), page("docs", status=200)]
    dead = looks.dead_help_fact(docs, NOW)
    assert dead is not None and "1 of 2" in dead.statement
