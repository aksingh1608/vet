"""Blocked homepage (403/429): the site reader reads the newest Wayback capture instead."""

import httpx

from tests.conftest import FakeLLM
from vet.collectors.base import Context
from vet.site_reader import _ClaimOut, _ExtractOut, capture_date, read_site, unwrap_wayback

TS = "20261020120000"
SNAP = f"https://web.archive.org/web/{TS}/https://blocked.example/"
HOME = f"""<html><body><h1>Blocked Inc</h1>
<a href="/web/{TS}/https://blocked.example/impressum">Impressum</a>
<a href="/web/{TS}/https://blocked.example/docs">Help</a>
<p>Founded in 2011 in Berlin.</p><footer>&copy; 2026 Blocked GmbH</footer></body></html>"""
IMPRESSUM = "<html><body><p>Blocked GmbH, Amtsgericht Berlin HRB 111111</p></body></html>"


def wayback(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/cdx/search/cdx" in url:
        # newest capture with status 200 (newer captures of the blocked page are 403s)
        assert "statuscode%3A200" in url or "statuscode:200" in url
        return httpx.Response(200, json=[["timestamp"], ["20250101000000"], [TS]])
    if f"/web/{TS}/https://blocked.example/impressum" in url:
        return httpx.Response(200, text=IMPRESSUM)
    if f"/web/{TS}/" in url:
        return httpx.Response(200, text=HOME)
    # /web/<today>/<url>: Wayback redirects to the closest capture
    original = url.split("/web/", 1)[1].split("/", 1)[1]
    return httpx.Response(302, headers={"Location": f"https://web.archive.org/web/{TS}/{original}"})


def extract(user: str) -> _ExtractOut:
    return _ExtractOut(
        vendor_name="Blocked",
        product_name="",
        legal_entity="Blocked GmbH",
        register_court="Amtsgericht Berlin",
        register_number="HRB 111111",
        country="DE",
        claims=[
            _ClaimOut(
                type="founded_year",
                text="Vendor says it was founded in 2011",
                source_url=SNAP,
                quote="Founded in 2011 in Berlin",
            )
        ],
    )


async def test_blocked_homepage_is_read_from_the_newest_archived_copy(router_fetcher) -> None:
    fetcher, rec = router_fetcher(
        [("web.archive.org/", wayback), ("https://blocked.example/", 429)]
    )
    llm = FakeLLM({_ExtractOut: extract})
    ctx = Context(
        url="https://blocked.example/",
        domain="blocked.example",
        settings=fetcher.settings,
        fetcher=fetcher,
        llm=llm,
        tavily=None,
    )
    async with fetcher:
        report = await read_site(ctx)

    home = report.pages[0]
    assert home.archived == "2026-10-20" and home.final_url == SNAP
    kinds = {p.kind: p for p in report.pages}
    assert kinds["legal"].archived  # key page also read from the archive
    assert "docs" not in kinds  # not a key page: not fetched at all while blocked
    # the claim's source is the snapshot, so a reader sees it is an archived copy
    assert report.claims[0].source_url == SNAP
    copyright_fact = next(f for f in report.facts if f.topic == "copyright")
    assert copyright_fact.source_url == SNAP
    assert copyright_fact.statement.endswith("(archived copy of 2026-10-20)")
    assert not any(f.topic == "missing_page" for f in report.facts)
    assert any("archived copy of 2026-10-20" in e for e in report.errors)
    # the live site is only asked for the homepage (with the fetcher's polite 429 retries),
    # never for subpages once it has refused
    live = {r.url.path for r in rec.requests if r.url.host == "blocked.example"}
    assert live <= {"/", "/robots.txt"}


def test_wayback_links_are_mapped_back_to_the_live_site() -> None:
    assert unwrap_wayback(
        f"https://web.archive.org/web/{TS}/https://blocked.example/impressum"
    ) == ("https://blocked.example/impressum")
    assert unwrap_wayback("https://blocked.example/about") == "https://blocked.example/about"
    assert capture_date(SNAP) == "2026-10-20"
