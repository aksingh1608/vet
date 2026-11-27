"""Identity and social-proof audit: offline, synthetic sites with invented companies."""

import json

from tests.conftest import FakeLLM, fixture_text
from vet.collectors import identity
from vet.collectors.base import Context, SiteIdentity, SitePage
from vet.collectors.identity import _Logos
from vet.llm import unwrap
from vet.schemas import Axis, Polarity, SourceKind
from vet.tavily import Tavily
from vet.text import contains_quote

HOLLOW = "quillnest.example"
SOLID = "brindlewood.example"
NOT_CUSTOMER_WORDS = ("partner", "startups", "iso", "ventures")


def page(kind: str, domain: str, path: str, fixture: str = "", **kw) -> SitePage:  # noqa: ANN003
    raw = fixture_text(f"identity/{fixture}") if fixture else ""
    return SitePage(kind=kind, url=f"https://{domain}{path}", status=200 if raw else 404,
                    raw=raw, **kw)  # fmt: skip


def logo_llm() -> FakeLLM:
    """Classifies from the alt text the collector sends, like the fast model would."""

    def respond(user: str) -> _Logos:
        logos = json.loads(unwrap(user))
        out = []
        for logo in logos:
            alt = logo["alt"].lower()
            if not alt:
                kind = "unknown"
            elif any(w in alt for w in NOT_CUSTOMER_WORDS):
                kind = "investor" if "ventures" in alt else "partner"
            elif "soc 2" in alt:
                kind = "certification"
            else:
                kind = "customer"
            out.append({"index": logo["index"], "kind": kind})
        return _Logos.model_validate({"logos": out})

    return FakeLLM({_Logos: respond})


def make_ctx(fetcher, llm, domain: str, pages: list[SitePage], ident: SiteIdentity) -> Context:  # noqa: ANN001
    return Context(
        url=f"https://{domain}/",
        domain=domain,
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=llm,
        tavily=Tavily(fetcher.settings, fetcher),
        identity=ident,
        pages=pages,
    )


def hollow_pages(about_error: str = "") -> list[SitePage]:
    about = page("about", HOLLOW, "/about", "" if about_error else "hollow_about.html")
    about.error = about_error
    return [
        page("home", HOLLOW, "/", "hollow_home.html"),
        about,
        page("legal", HOLLOW, "/impressum", guessed=True, error="HTTP 404"),
    ]


HOLLOW_ID = SiteIdentity(vendor_name="Quillnest", product_name="Quillnest")
SOLID_ID = SiteIdentity(
    vendor_name="Brindlewood Analytics",
    product_name="Brindlewood",
    legal_entity="Brindlewood Analytics GmbH",
    country="DE",
)


async def run(router_fetcher, domain, pages, ident, tavily_answer: object = 500, llm=None):  # noqa: ANN001
    fetcher, rec = router_fetcher([("api.tavily.com", tavily_answer)])
    async with fetcher:
        facts = await identity.collect(make_ctx(fetcher, llm or logo_llm(), domain, pages, ident))
    return {f.topic: f for f in facts}, facts, rec, pages


def assert_quotes_on_pages(facts, pages) -> None:  # noqa: ANN001
    raw = {p.final_url or p.url: p.raw for p in pages}
    for f in facts:
        if f.topic == "company_size":
            continue
        assert f.quote, f.topic
        assert contains_quote(raw[f.source_url], f.quote), (f.topic, f.quote)


async def test_hollow_site_reports_every_topic(router_fetcher) -> None:
    by_topic, facts, rec, pages = await run(
        router_fetcher,
        HOLLOW,
        hollow_pages(),
        HOLLOW_ID,
        fixture_text("identity/linkedin_tavily.json"),
    )
    assert set(by_topic) == {
        "no_legal_entity",
        "traction_inconsistent",
        "logo_wall_not_customers",
        "policy_as_file",
        "coming_soon",
        "company_size",
    }
    assert len({f.id for f in facts}) == len(facts)
    assert not any(f.is_hard_flag for f in facts)
    assert all(f.weight <= 0.3 for f in facts)
    assert_quotes_on_pages(facts, pages)

    legal = by_topic["no_legal_entity"]
    assert "https://quillnest.example/" in legal.statement
    assert "https://quillnest.example/about" in legal.statement
    assert "impressum" not in legal.statement  # the guessed 404 was not checked
    assert legal.polarity == Polarity.NEGATIVE and legal.source_kind == SourceKind.VENDOR

    traction = by_topic["traction_inconsistent"]
    assert "10,000+ users" in traction.statement and "50,000 users" in traction.statement

    wall = by_topic["logo_wall_not_customers"]
    assert wall.quote == "Trusted by"
    assert "4 of 5 logos" in wall.statement  # the unlabelled logo counts as unknown

    policies = [f for f in facts if f.topic == "policy_as_file"]
    assert {f.quote for f in policies} == {"Privacy Policy", "Terms of Service"}
    assert any(".docx" in f.statement for f in policies)

    soon = by_topic["coming_soon"]
    assert soon.axis == Axis.REAL_ALIVE

    sizes = [f for f in facts if f.topic == "company_size"]
    assert {f.quote for f in sizes} == {"2-10 employees", "1,234 followers"}
    assert all(f.source_url == "https://www.linkedin.com/company/quillnest" for f in sizes)
    assert all(f.polarity == Polarity.NEUTRAL and f.source_type == "social" for f in sizes)

    # One Tavily search, LinkedIn only; LinkedIn itself is never fetched.
    tavily = [r for r in rec.requests if r.url.host == "api.tavily.com"]
    assert len(tavily) == 1
    assert json.loads(tavily[0].content)["include_domains"] == ["linkedin.com"]
    assert not any("linkedin" in r.url.host for r in rec.requests)


async def test_solid_vendor_has_nothing_to_report(router_fetcher) -> None:
    pages = [
        page("home", SOLID, "/", "solid_home.html"),
        page("legal", SOLID, "/imprint", "solid_legal.html"),
        page("pricing", SOLID, "/pricing", "solid_pricing.html"),
        page("privacy", SOLID, "/privacy", "solid_legal.html"),
    ]
    _, facts, _, _ = await run(router_fetcher, SOLID, pages, SOLID_ID)
    # Unsourced customer logos, a feature "coming soon" and a plan's user limit are normal.
    assert facts == []


async def test_no_legal_entity_needs_the_pages_fetched(router_fetcher) -> None:
    by_topic, _, _, _ = await run(
        router_fetcher, HOLLOW, hollow_pages(about_error="HTTP 503"), HOLLOW_ID
    )
    assert "no_legal_entity" not in by_topic


async def test_no_legal_entity_when_the_site_shows_an_address(router_fetcher) -> None:
    # Same site, but the reader found nothing while the legal page shows an entity.
    pages = [*hollow_pages()[:2], page("legal", HOLLOW, "/imprint", "solid_legal.html")]
    by_topic, _, _, _ = await run(router_fetcher, HOLLOW, pages, HOLLOW_ID)
    assert "no_legal_entity" not in by_topic


async def test_unknown_logos_do_not_make_a_logo_wall_finding(router_fetcher) -> None:
    llm = FakeLLM({_Logos: lambda user: _Logos.model_validate({"logos": []})})
    by_topic, _, _, _ = await run(router_fetcher, HOLLOW, hollow_pages(), HOLLOW_ID, llm=llm)
    assert "logo_wall_not_customers" not in by_topic
    assert "coming_soon" in by_topic  # the logo section is there, whatever it holds


async def test_model_and_search_failures_lose_only_their_own_topics(router_fetcher) -> None:
    by_topic, _, _, _ = await run(router_fetcher, HOLLOW, hollow_pages(), HOLLOW_ID, llm=FakeLLM())
    assert "logo_wall_not_customers" not in by_topic and "company_size" not in by_topic
    assert {"no_legal_entity", "traction_inconsistent", "policy_as_file"} <= set(by_topic)


async def test_linkedin_snippet_without_figures_or_for_a_namesake(router_fetcher) -> None:
    answer = {
        "results": [
            {"url": "https://www.linkedin.com/company/quillnest", "title": "Quillnest",
             "content": "Quillnest builds smart notes for teams."},
            {"url": "https://www.linkedin.com/company/quillnest-labs-gmbh",
             "title": "Quillnest Labs", "content": "Quillnest Labs | 90,000 followers"},
        ]
    }  # fmt: skip
    by_topic, _, _, _ = await run(router_fetcher, HOLLOW, hollow_pages(), HOLLOW_ID, answer)
    assert "company_size" not in by_topic


async def test_no_pages_no_facts(router_fetcher) -> None:
    _, facts, _, _ = await run(router_fetcher, HOLLOW, [], HOLLOW_ID)
    assert facts == []
