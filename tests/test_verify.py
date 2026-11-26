from datetime import UTC, datetime

from tests.conftest import FakeLLM
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.verify import _Support, _SupportOut, verify_reasons

NOW = datetime(2026, 11, 25, tzinfo=UTC)
ND = "https://www.northdata.de/Ledgerly%20Software%20GmbH/HRB%20123456"


def fact(fid: str, url: str, quote: str, kind: SourceKind = SourceKind.INDEPENDENT) -> Fact:
    return Fact(
        id=fid,
        collector="footprint",
        statement=f"statement {fid}",
        axis=Axis.REAL_ALIVE,
        polarity=Polarity.POSITIVE,
        weight=0.5,
        source_url=url,
        quote=quote,
        source_kind=kind,
        fetched_at=NOW,
    )


def support_all(unsupported: set[str] = frozenset()):  # type: ignore[assignment]
    def respond(user: str) -> _SupportOut:
        import json

        items = json.loads(user.removeprefix("<data>\n").removesuffix("\n</data>"))
        return _SupportOut(
            items=[_Support(id=i["id"], supported=i["id"] not in unsupported) for i in items]
        )

    return respond


PAGES = [
    ("review.example/a", "<p>Ledgerly has 212 reviews with an average of 4.6.</p>"),
    ("review.example/b", "<p>Customers use Ledgerly daily.</p>"),
    ("review.example/c", "<p>Totally different text.</p>"),
    ("web.archive.org/web/2009", "<html>snapshot</html>"),
]


async def test_quotes_checked_on_source_and_by_model(router_fetcher) -> None:
    fetcher, rec = router_fetcher(list(PAGES))
    facts = [
        fact("ok", "https://review.example/a", "212 reviews with an average of 4.6"),
        fact("missing", "https://review.example/c", "Ledgerly is great"),
        fact("overclaim", "https://review.example/b", "Customers use Ledgerly daily"),
        fact(
            "computed",
            "https://web.archive.org/web/2009/ledgerly.example/",
            "",
            SourceKind.RECORD,
        ),
        fact("dead", "https://review.example/gone", "anything"),
        fact("nd", ND, "Amtsgericht München HRB 123456", SourceKind.RECORD),
    ]
    snippets = {ND: "Ledgerly Software GmbH, München. Amtsgericht München HRB 123456."}
    llm = FakeLLM({_SupportOut: support_all({"overclaim"})})
    async with fetcher:
        reasons, checks = await verify_reasons(facts, fetcher, llm, snippets, fetcher.settings)

    assert [r.fact_id for r in reasons] == ["ok", "computed", "nd"]
    results = {c.fact_id: c for c in checks}
    assert not results["missing"].source_ok
    assert results["overclaim"].supported is False
    assert not results["dead"].source_ok
    assert "Tavily snippet" in results["nd"].note
    assert not any("northdata" in r.url.host for r in rec.requests)  # never fetched


async def test_backfills_from_lower_ranked_facts(router_fetcher) -> None:
    fetcher, _ = router_fetcher(list(PAGES))
    bad = [fact(f"bad{i}", "https://review.example/c", "not there") for i in range(10)]
    good = [
        fact(f"good{i}", "https://review.example/b", "Customers use Ledgerly") for i in range(5)
    ]
    llm = FakeLLM({_SupportOut: support_all()})
    async with fetcher:
        reasons, _ = await verify_reasons(bad + good, fetcher, llm, {}, fetcher.settings)
    assert len(reasons) >= 4
    assert all(r.fact_id.startswith("good") for r in reasons)


async def test_at_most_seven_reasons(router_fetcher) -> None:
    fetcher, _ = router_fetcher(list(PAGES))
    good = [fact(f"g{i}", "https://review.example/b", "Customers use Ledgerly") for i in range(12)]
    async with fetcher:
        reasons, _ = await verify_reasons(
            good, fetcher, FakeLLM({_SupportOut: support_all()}), {}, fetcher.settings
        )
    assert len(reasons) == 7
    assert [r.fact_id for r in reasons] == [f"g{i}" for i in range(7)]


def test_api_record_facts_skip_the_model_check() -> None:
    from vet.verify import needs_support_check

    rdap = fact("r", "https://rdap.org/domain/x.example", "2008-05-14T10:00:00Z", SourceKind.RECORD)
    review = fact("i", "https://review.example/a", "212 reviews")
    nd = fact("n", ND, "Amtsgericht München HRB 123456", SourceKind.RECORD)
    assert not needs_support_check(rdap)
    assert needs_support_check(review)
    assert needs_support_check(nd)  # human-written register text still gets checked
