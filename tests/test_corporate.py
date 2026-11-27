"""Corporate collector: offline, invented companies."""

from datetime import UTC, datetime

from vet.collectors import corporate
from vet.collectors.base import Context, SiteIdentity, SitePage
from vet.config import Settings
from vet.schemas import Polarity, SourceKind
from vet.tavily import TavilyResult

TODAY = datetime(2026, 11, 27, tzinfo=UTC)


def ctx(settings: Settings, domain: str = "acmeledger.example", pages=None) -> Context:  # noqa: ANN001
    return Context(
        url=f"https://{domain}/",
        domain=domain,
        settings=settings,
        fetcher=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        tavily=None,
        identity=SiteIdentity(vendor_name="AcmeLedger", product_name="AcmeLedger"),
        pages=pages or [],
    )


def item(**kw) -> corporate._Item:  # noqa: ANN003
    base = {
        "url": "https://news.example-press.com/acme",
        "about_this_vendor": True,
        "independent": True,
        "event": "regulator_finding",
        "concerns_product": True,
        "year": "2025",
        "counterparty": "",
        "source_type": "press",
        "quote": "The regulator fined AcmeLedger for deceptive claims",
        "statement": "A regulator fined AcmeLedger in 2025 for deceptive claims.",
    }
    return corporate._Item(**{**base, **kw})


def seen(content: str, url: str = "https://news.example-press.com/acme") -> dict:
    return {url: TavilyResult(url=url, content=content)}


def test_finding_kept_with_year_scope_and_zero_weight(settings) -> None:
    facts = corporate.facts_from(
        [item()],
        seen("In 2025 the regulator fined AcmeLedger for deceptive claims."),
        ctx(settings),
        TODAY,
    )
    (f,) = facts
    assert f.topic == "regulator_finding" and f.polarity == Polarity.NEGATIVE
    assert f.weight == 0.0 and "scope:product" in f.tags
    assert f.observed_at and f.observed_at.year == 2025


def test_quote_not_on_result_is_dropped(settings) -> None:
    assert corporate.facts_from([item()], seen("Nothing here."), ctx(settings), TODAY) == []


def test_namesake_negative_is_dropped(settings) -> None:
    other = item(quote="The regulator fined OtherCorp for deceptive claims")
    facts = corporate.facts_from(
        [other], seen("The regulator fined OtherCorp for deceptive claims."), ctx(settings), TODAY
    )
    assert facts == []


def test_ownership_change_keeps_counterparty_and_parent_scope(settings) -> None:
    owned = item(
        event="ownership_change",
        concerns_product=False,
        counterparty="Holdco Partners",
        quote="AcmeLedger was acquired by Holdco Partners in 2019",
    )
    (f,) = corporate.facts_from(
        [owned], seen("AcmeLedger was acquired by Holdco Partners in 2019."), ctx(settings), TODAY
    )
    assert "counterparty:Holdco Partners" in f.tags and "scope:parent" in f.tags


def test_lifecycle_statement_on_a_page_already_read(settings) -> None:
    page = SitePage(
        kind="product",
        url="https://acmeledger.example/classic",
        text="Welcome. AcmeLedger Classic has been replaced by AcmeLedger Cloud. Contact us.",
    )
    (f,) = corporate.lifecycle_facts(ctx(settings, pages=[page]), TODAY)
    assert f.source_kind == SourceKind.VENDOR and "statement_against_interest" in f.tags
    assert "replaced by AcmeLedger Cloud" in f.quote


def test_marketing_about_legacy_systems_is_not_a_lifecycle_statement(settings) -> None:
    page = SitePage(
        kind="home",
        url="https://acmeledger.example/",
        text="Replace your legacy systems with AcmeLedger.",
    )
    assert corporate.lifecycle_facts(ctx(settings, pages=[page]), TODAY) == []


def test_german_variants_only_for_german_speaking_sites(settings) -> None:
    assert len(corporate.queries(ctx(settings))) == 4
    assert len(corporate.queries(ctx(settings, domain="acmeledger.de"))) == 6


def test_end_of_support_for_older_versions_is_not_a_lifecycle_statement(settings) -> None:
    page = SitePage(
        kind="docs",
        url="https://acmeledger.example/docs",
        text="Archived notes cover older AcmeLedger release versions that are no longer supported.",
    )
    assert corporate.lifecycle_facts(ctx(settings, pages=[page]), TODAY) == []
