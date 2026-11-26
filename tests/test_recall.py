"""Footprint recall: targeted searches for strong evidence (in parallel), and the GitHub
adoption record."""

import json
from datetime import UTC, datetime

import httpx

from tests.conftest import FakeLLM
from vet.collectors import alive, footprint
from vet.collectors.base import Context, SiteIdentity, SitePage
from vet.collectors.footprint import _Classified, _Item
from vet.llm import unwrap
from vet.schemas import Axis, Polarity
from vet.sources import evidence_class
from vet.tavily import TavilyResult

PRESS = "https://www.heise.de/news/ledgerly-series-a"
CUSTOMER = "https://acme-logistics.example/blog/why"
REVIEWS = "https://www.g2.com/products/ledgerly/reviews"
PH = "https://www.producthunt.com/products/ledgerly"
CLASSES = {  # url -> (source_type, topic, quote)
    PRESS: ("press", "news", "Ledgerly raises Series A"),
    CUSTOMER: ("blog", "customer", "we use Ledgerly daily"),
    REVIEWS: ("review", "reviews", "Ledgerly 212 reviews"),
    PH: ("other", "news", "Ledgerly on PH"),
}
TARGETED = ("funding OR raises", 'case study OR "we use', '"Ledgerly" reviews')


def result(url: str) -> TavilyResult:
    return TavilyResult(url=url, content=f"... {CLASSES[url][2]} ...")


class FakeTavily:
    """Answers each query from a table (first key contained in the query); records queries."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def search(self, query: str, max_results: int = 5, **params: object):  # noqa: ANN201
        self.asked.append(query)
        urls = next((u for key, u in self.answers.items() if key in query), [])
        return [result(u) for u in urls]


def classify(user: str) -> _Classified:
    sent = json.loads(unwrap(user))
    items = []
    for r in sent["results"]:
        stype, topic, quote = CLASSES[r["url"]]
        items.append(
            _Item(url=r["url"], relevant=True, independent=True, topic=topic, source_type=stype,
                  quote=quote, statement="s", axis=Axis.TRUST, polarity=Polarity.POSITIVE,
                  weight=0.5, is_hard_flag=False)
        )  # fmt: skip
    return _Classified(items=items)


def ctx_with(tavily: FakeTavily) -> Context:
    return Context(
        url="https://ledgerly.example/",
        domain="ledgerly.example",
        settings=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        llm=FakeLLM({_Classified: classify}),
        tavily=tavily,  # type: ignore[arg-type]
        identity=SiteIdentity(vendor_name="Ledgerly", country="US"),
    )


def targeted(asked: list[str]) -> list[str]:
    return [key for q in asked for key in TARGETED if key in q]


async def test_targeted_searches_run_when_fewer_than_three_strong_sites() -> None:
    tavily = FakeTavily(
        {
            '"Ledgerly" ledgerly.example': [PH],
            TARGETED[0]: [PRESS],
            TARGETED[1]: [CUSTOMER],
            TARGETED[2]: [REVIEWS],
        }
    )
    facts = await footprint.collect(ctx_with(tavily))
    assert sorted(targeted(tavily.asked)) == sorted(TARGETED)  # all three, in one parallel round
    assert footprint.strong_sites(facts) == {"heise.de", "acme-logistics.example", "g2.com"}


async def test_no_targeted_searches_when_three_strong_sites_are_already_found() -> None:
    tavily = FakeTavily({'"Ledgerly" ledgerly.example': [PRESS, CUSTOMER, REVIEWS]})
    await footprint.collect(ctx_with(tavily))
    assert targeted(tavily.asked) == []


async def test_launch_sites_stay_weak() -> None:
    tavily = FakeTavily({'"Ledgerly" ledgerly.example': [PH]})
    facts = await footprint.collect(ctx_with(tavily))
    assert [evidence_class(f) for f in facts] == ["weak"]
    assert footprint.strong_sites(facts) == set()


# ---- GitHub adoption record ---------------------------------------------------------------

NOW = datetime(2026, 11, 26, tzinfo=UTC)


def test_contributor_count_reads_the_last_page() -> None:
    base = "https://api.github.com/repositories/1/contributors?per_page=1&anon=1"
    link = f'<{base}&page=2>; rel="next", <{base}&page=148>; rel="last"'
    assert alive.contributor_count(link, "[{}]") == 148
    assert alive.contributor_count("", "[{}]") == 1
    assert alive.contributor_count("", "[]") == 0


def repos(stars: int) -> str:
    return json.dumps(
        [
            {"name": "analytics", "fork": False, "archived": False, "stargazers_count": stars,
             "pushed_at": "2026-11-20T10:00:00Z"},
            {"name": "docs", "fork": False, "archived": False, "stargazers_count": 3,
             "pushed_at": "2026-11-25T10:00:00Z"},
        ]
    )  # fmt: skip


def contributors(n: int):  # noqa: ANN201
    def handler(request: httpx.Request) -> httpx.Response:
        last = f'<https://api.github.com/x?per_page=1&anon=1&page={n}>; rel="last"'
        return httpx.Response(200, text="[{}]", headers={"Link": last})

    return handler


def gh_ctx(fetcher) -> Context:  # noqa: ANN001
    home = SitePage(kind="home", url="https://ledgerly.example/", text="x",
                    final_url="https://ledgerly.example/",
                    raw='<a href="https://github.com/ledgerly">GitHub</a>')  # fmt: skip
    return Context(
        url="https://ledgerly.example/",
        domain="ledgerly.example",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=FakeLLM(),
        tavily=None,
        identity=SiteIdentity(vendor_name="Ledgerly"),
        pages=[home],
    )


async def test_github_adoption_is_a_strong_record(router_fetcher) -> None:
    fetcher, _ = router_fetcher(
        [
            ("api.github.com/users/ledgerly/repos", repos(4200)),
            ("api.github.com/repos/ledgerly/analytics/contributors", contributors(148)),
        ]
    )
    async with fetcher:
        fact = await alive.github_adoption_fact(gh_ctx(fetcher), "ledgerly", 14, NOW)
    assert fact is not None
    assert fact.statement == (
        "GitHub: github.com/ledgerly/analytics has 4,200 stars and 148 contributors; "
        "last pushed 2026-11-20"
    )
    assert fact.axis == Axis.TRUST and evidence_class(fact) == "strong"
    assert fact.check_url.startswith("https://api.github.com/users/ledgerly/repos")
    assert fact.quote == "2026-11-20T10:00:00Z"


async def adoption(router_fetcher, stars: int, people: int):  # noqa: ANN001, ANN201
    fetcher, _ = router_fetcher(
        [
            ("api.github.com/users/ledgerly/repos", repos(stars)),
            ("api.github.com/repos/ledgerly/analytics/contributors", contributors(people)),
        ]
    )
    async with fetcher:
        return await alive.github_adoption_fact(gh_ctx(fetcher), "ledgerly", 14, NOW)


async def test_too_few_stars_give_no_adoption_fact(router_fetcher) -> None:
    assert await adoption(router_fetcher, stars=40, people=148) is None


async def test_too_few_contributors_give_no_adoption_fact(router_fetcher) -> None:
    assert await adoption(router_fetcher, stars=4200, people=2) is None


async def test_a_namesakes_good_news_is_not_evidence() -> None:
    """The smollaunch case: "Smol has teamed up with Pulpex" is about Smol, a detergent maker,
    not about Smol Launch."""
    url = "https://www.packagingeurope.com/smol-and-pulpex"
    CLASSES[url] = ("press", "news", "Smol has teamed up with Pulpex to launch a bottle")
    tavily = FakeTavily({'"Smol Launch" smollaunch.com': [url]})
    ctx = ctx_with(tavily)
    ctx.domain, ctx.url = "smollaunch.com", "https://smollaunch.com/"
    ctx.identity = SiteIdentity(vendor_name="Smol Launch")
    facts = await footprint.collect(ctx)
    assert facts == []
