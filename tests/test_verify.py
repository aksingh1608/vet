import json
from datetime import UTC, datetime

from tests.conftest import FakeLLM
from vet.llm import unwrap
from vet.schemas import Axis, Fact, Polarity, SourceKind
from vet.verify import (
    _Support,
    _SupportOut,
    needs_support_check,
    verify_and_select,
    verify_reasons,
)

NOW = datetime(2026, 11, 25, tzinfo=UTC)
ND = "https://www.northdata.de/Ledgerly%20Software%20GmbH/HRB%20123456"
G2 = "https://www.g2.com/products/ledgerly/reviews"


def fact(
    fid: str,
    url: str,
    quote: str,
    kind: SourceKind = SourceKind.INDEPENDENT,
    collector: str = "footprint",
    **kw: object,
) -> Fact:
    data: dict[str, object] = {
        "id": fid,
        "collector": collector,
        "statement": f"statement {fid}",
        "axis": Axis.REAL_ALIVE,
        "polarity": Polarity.POSITIVE,
        "weight": 0.5,
        "source_url": url,
        "quote": quote,
        "source_kind": kind,
        "fetched_at": NOW,
    }
    data.update(kw)
    return Fact.model_validate(data)


def support_all(unsupported: frozenset[str] = frozenset()):
    def respond(user: str) -> _SupportOut:
        items = json.loads(unwrap(user))
        return _SupportOut(
            items=[_Support(id=i["id"], supported=i["id"] not in unsupported) for i in items]
        )

    return respond


PAGES = [
    ("review.example/a", "<p>Ledgerly has 212 reviews with an average of 4.6.</p>"),
    ("review.example/b", "<p>Customers use Ledgerly daily.</p>"),
    ("review.example/c", "<p>Totally different text.</p>"),
    ("rdap.org/domain/", '{"events": [{"eventDate": "2008-05-14T10:00:00Z"}]}'),
    ("web.archive.org/web/2009", "<html>snapshot</html>"),
    ("ledgerly.example/about", "<p>Ledgerly was founded in 1990 in Munich.</p>"),
    ("ledgerly.example/docs", 404),
]


async def test_quotes_checked_on_source_record_snippet_and_by_model(router_fetcher) -> None:
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
            collector="age",
            topic="first_capture",
            observed_at=datetime(2009, 2, 14, tzinfo=UTC),
        ),
        fact(
            "rdap",
            "https://lookup.icann.org/en/lookup?name=ledgerly.example",
            "2008-05-14T10:00:00Z",
            SourceKind.RECORD,
            collector="age",
            topic="domain_registered",
            check_url="https://rdap.org/domain/ledgerly.example",
            observed_at=datetime(2008, 5, 14, tzinfo=UTC),
        ),
        fact("dead", "https://review.example/gone", "anything"),
        fact("nd", ND, "Amtsgericht München HRB 123456", SourceKind.RECORD, collector="legal_id"),
        fact("g2", G2, "212 reviews on G2", collector="footprint"),
        fact(
            "gone",
            "https://ledgerly.example/docs",
            "",
            SourceKind.VENDOR,
            collector="site_reader",
            topic="missing_page",
            polarity=Polarity.NEGATIVE,
        ),
    ]
    snippets = {
        ND: "Ledgerly Software GmbH, München. Amtsgericht München HRB 123456.",
        G2: "Ledgerly has 212 reviews on G2 with an average rating of 4.6.",
    }
    llm = FakeLLM({_SupportOut: support_all(frozenset({"overclaim"}))})
    async with fetcher:
        selection, checks = await verify_and_select(
            facts, facts, [], fetcher, llm, snippets, fetcher.settings, "ledgerly.example"
        )

    results = {c.fact_id: c for c in checks}
    assert results["ok"].passed
    assert not results["missing"].source_ok
    # the sentence overclaimed; the verified quote replaces it
    assert results["overclaim"].passed and "verbatim quote" in results["overclaim"].note
    assert not results["dead"].source_ok
    assert results["rdap"].passed and "record" in results["rdap"].note
    assert results["computed"].passed
    assert "Tavily snippet" in results["nd"].note and results["nd"].passed
    assert results["g2"].passed  # review site: snippet only
    assert results["gone"].passed and "404" in results["gone"].note  # the 404 is the claim
    hosts = {r.url.host for r in rec.requests}
    assert not any(h.endswith(("northdata.de", "g2.com", "icann.org")) for h in hosts)

    # one age reason, merged and linked to the ICANN lookup; one per collector
    texts = [r.text for r in selection.reasons]
    age = [r for r in selection.reasons if r.fact_id in ("rdap", "computed")]
    assert len(age) == 1
    assert age[0].source_url.startswith("https://lookup.icann.org/")
    assert "domain registered 2008-05-14" in age[0].text.lower()
    assert "first archived" in age[0].text
    assert len(selection.reasons) == len(set(texts))
    assert any(n.startswith("age reason merged") for n in selection.notes)


async def test_backfills_from_lower_ranked_facts(router_fetcher) -> None:
    fetcher, _ = router_fetcher(list(PAGES))
    bad = [fact(f"bad{i}", "https://review.example/c", "not there") for i in range(30)]
    good = [
        fact(f"good{i}", "https://review.example/b", "Customers use Ledgerly", collector=f"c{i}")
        for i in range(5)
    ]
    llm = FakeLLM({_SupportOut: support_all()})
    async with fetcher:
        reasons, _ = await verify_reasons(bad + good, fetcher, llm, {}, fetcher.settings)
    assert len(reasons) >= 4
    assert all(r.fact_id.startswith("good") for r in reasons)


async def test_at_most_seven_reasons(router_fetcher) -> None:
    fetcher, _ = router_fetcher(list(PAGES))
    good = [
        fact(f"g{i}", "https://review.example/b", "Customers use Ledgerly", collector=f"c{i}")
        for i in range(12)
    ]
    async with fetcher:
        reasons, _ = await verify_reasons(
            good, fetcher, FakeLLM({_SupportOut: support_all()}), {}, fetcher.settings
        )
    assert len(reasons) == 7
    assert [r.fact_id for r in reasons] == [f"g{i}" for i in range(7)]


async def test_claims_check_fact_needs_its_record_to_verify(router_fetcher) -> None:
    fetcher, _ = router_fetcher(list(PAGES))
    record = fact(
        "rdap",
        "https://lookup.icann.org/en/lookup?name=ledgerly.example",
        "1999-01-01T00:00:00Z",  # not in the RDAP JSON: the record fails
        SourceKind.RECORD,
        collector="age",
        topic="domain_registered",
        check_url="https://rdap.org/domain/ledgerly.example",
    )
    gap = fact(
        "claims_check-0",
        "https://ledgerly.example/about",
        "founded in 1990",
        SourceKind.VENDOR,
        collector="claims_check",
        topic="founding_year_gap",
        polarity=Polarity.NEGATIVE,
        axis=Axis.TRUST,
        related_fact_ids=["rdap"],
    )
    async with fetcher:
        selection, checks = await verify_and_select(
            [gap, record], [gap, record], [gap.id], fetcher, FakeLLM(), {}, fetcher.settings
        )
    results = {c.fact_id: c for c in checks}
    assert not results["claims_check-0"].passed
    assert "underlying record" in results["claims_check-0"].note
    assert selection.findings == []


def test_code_worded_facts_skip_the_model_check() -> None:
    rdap = fact(
        "r",
        "https://lookup.icann.org/en/lookup?name=x.example",
        "2008-05-14T10:00:00Z",
        SourceKind.RECORD,
        collector="age",
        check_url="https://rdap.org/domain/x.example",
    )
    hibp = fact(
        "h",
        "https://haveibeenpwned.com/PwnedWebsites#X",
        "2019-05-24",
        SourceKind.RECORD,
        collector="risk",
        check_url="https://haveibeenpwned.com/api/v3/breaches?domain=x.example",
    )
    review = fact("i", "https://review.example/a", "212 reviews")
    nd = fact("n", ND, "Amtsgericht München HRB 123456", SourceKind.RECORD, collector="legal_id")
    guard = fact("g", "https://x.example/", "ignore previous", SourceKind.VENDOR, collector="guard")
    assert not needs_support_check(rdap)
    assert not needs_support_check(hibp)
    assert not needs_support_check(guard)
    assert needs_support_check(review)
    assert needs_support_check(nd)  # human-written register text still gets checked


def test_overclaiming_sentence_falls_back_to_the_verified_quote() -> None:
    from vet.verify import VerifyResult, fall_back_to_quotes

    review = fact(
        "fp-1",
        "https://www.gartner.com/reviews/product/x",
        "Constant product development - major release every quarter.",
        statement="Gartner reviewers rate X 4.8 for security and support",
    )
    breach = fact(
        "risk-0", "https://heise.de/a", "Datenleck bei X", collector="risk", is_hard_flag=True
    )
    vendor = fact("site-1", "https://x.example/", "Founded 1990", collector="site_reader")
    results = {
        f.id: VerifyResult(fact_id=f.id, source_ok=True, supported=False)
        for f in (review, breach, vendor)
    }
    out = fall_back_to_quotes({f.id: f for f in (review, breach, vendor)}, results)
    assert set(out) == {"fp-1"}
    assert out["fp-1"].statement == (
        'gartner.com: "Constant product development - major release every quarter."'
    )
    assert results["fp-1"].passed and "verbatim quote" in results["fp-1"].note
    assert not results["risk-0"].passed  # a hard flag must stand on its own sentence
    assert not results["site-1"].passed


# ---- pages that refuse us: the cached Tavily snippet stands in -----------------------------


async def test_refused_page_is_verified_from_the_snippet_and_marked(router_fetcher) -> None:
    from vet.verify import check_source

    quote = "Ledgerly notified customers of a 2026 breach"
    fetcher, _ = router_fetcher([("press.example/403", 403), ("press.example/429", 429)])
    snippets = {
        "https://press.example/403": f"... {quote} ...",
        "https://press.example/429": f"... {quote} ...",
    }
    async with fetcher:
        for path, status in (("403", 403), ("429", 429)):
            f = fact(f"risk-{path}", f"https://press.example/{path}", quote)
            result = await check_source(f, fetcher, snippets, fetcher.settings)
            assert result.passed and result.snippet_sourced
            assert result.note.startswith(f"snippet-sourced: page answered {status}")
        # a quote the snippet does not hold still fails
        bad = fact("risk-x", "https://press.example/403", "words that are nowhere")
        result = await check_source(bad, fetcher, snippets, fetcher.settings)
        assert not result.passed and not result.snippet_sourced


async def test_a_stored_403_gives_the_same_result_on_the_next_run(router_fetcher) -> None:
    """Verification uses the stored copy: a 403 cached by the first run is not refetched, and
    the second run reaches the same decision through the same snippet."""
    from vet.verify import check_source

    quote = "Ledgerly notified customers of a 2026 breach"
    fetcher, rec = router_fetcher([("press.example/403", 403)])
    snippets = {"https://press.example/403": f"... {quote} ..."}
    f = fact("risk-1", "https://press.example/403", quote)
    async with fetcher:
        first = await check_source(f, fetcher, snippets, fetcher.settings)
        page_hits = sum(1 for r in rec.requests if r.url.path == "/403")
        second = await check_source(f, fetcher, snippets, fetcher.settings)
    assert first == second and second.snippet_sourced
    assert sum(1 for r in rec.requests if r.url.path == "/403") == page_hits  # not refetched
