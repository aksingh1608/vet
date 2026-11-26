import json

from tests.conftest import FakeLLM, fixture_text
from vet.collectors import footprint
from vet.collectors.base import Context, SiteIdentity
from vet.collectors.footprint import _Classified, _Item
from vet.llm import unwrap
from vet.schemas import Axis, Polarity, SourceKind
from vet.tavily import Tavily

G2 = "https://www.g2.com/products/ledgerly/reviews"
HEISE = "https://www.heise.de/news/ledgerly-datenleck"


def item(url: str, **kw: object) -> _Item:
    base = {
        "url": url,
        "relevant": True,
        "independent": True,
        "axis": Axis.REAL_ALIVE,
        "polarity": Polarity.POSITIVE,
        "weight": 0.5,
        "statement": "s",
        "quote": "",
        "is_hard_flag": False,
        "topic": "reviews",
        "source_type": "review",
    }
    base.update(kw)
    return _Item(**base)


def classify(user: str) -> _Classified:
    sent = json.loads(unwrap(user))
    urls = {r["url"] for r in sent["results"]}
    assert "https://ledgerly.example/blog/launch" not in urls  # own domain never classified
    return _Classified(
        items=[
            item(
                G2,
                statement="G2 lists 212 reviews of Ledgerly averaging 4.6 out of 5",
                quote="Ledgerly has 212 reviews on G2 with an average rating of 4.6 out of 5",
                weight=1.7,  # the model's number is ignored
            ),
            item(
                "https://www.prnewswire.com/ledgerly-raises",
                independent=False,
                statement="Ledgerly raised a Series B",
                quote="Ledgerly today announced a Series B round",
            ),
            item("https://news.example.org/ledger-farms", relevant=False),
            item(
                HEISE,
                axis=Axis.TRUST,
                polarity=Polarity.NEGATIVE,
                weight=0.8,
                is_hard_flag=True,
                topic="news",
                statement="heise reports a 2024 data leak at Ledgerly",
                quote="Ledgerly sind 2024 Kundendaten von 12.000 Firmen abgeflossen",
            ),
            item(  # quote not in the result: dropped
                G2,
                statement="G2 calls Ledgerly the market leader",
                quote="Ledgerly is the undisputed market leader",
            ),
        ]
    )


def make_ctx(fetcher, llm, country: str = "DE") -> Context:
    return Context(
        url="https://ledgerly.example/",
        domain="ledgerly.example",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=llm,
        tavily=Tavily(fetcher.settings, fetcher),
        identity=SiteIdentity(vendor_name="Ledgerly", country=country),
    )


async def test_footprint_keeps_only_relevant_independent_quoted_results(router_fetcher) -> None:
    fetcher, rec = router_fetcher([("api.tavily.com", fixture_text("api/tavily_footprint.json"))])
    async with fetcher:
        facts = await footprint.collect(make_ctx(fetcher, FakeLLM({_Classified: classify})))

    assert [f.source_url for f in facts] == [G2, HEISE]
    # review counts and ratings become data, written by code; the quote stays as evidence
    assert facts[0].statement == "G2: 212 reviews, average rating 4.6/5"
    assert facts[0].code_worded and "212 reviews on G2" in facts[0].quote
    assert all(f.source_kind == SourceKind.INDEPENDENT for f in facts)
    assert facts[0].weight == 0.5  # strong evidence class: a review platform with 212 reviews
    assert facts[1].is_hard_flag and facts[1].axis == Axis.TRUST

    queries = [json.loads(r.content) for r in rec.requests]
    assert any(q.get("exclude_domains") == ["ledgerly.example"] for q in queries)
    assert any("g2.com" in q.get("include_domains", []) for q in queries)
    assert any("Erfahrungen" in q["query"] for q in queries)  # German-speaking vendor


async def test_no_german_query_for_non_german_vendor(router_fetcher) -> None:
    fetcher, rec = router_fetcher([("api.tavily.com", fixture_text("api/tavily_footprint.json"))])
    async with fetcher:
        await footprint.collect(make_ctx(fetcher, FakeLLM({_Classified: classify}), country="US"))
    assert not any("Erfahrungen" in json.loads(r.content)["query"] for r in rec.requests)
