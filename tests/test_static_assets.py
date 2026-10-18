"""
The interface has to work on a network with no way out to the internet.

That is the normal case for a management segment, and it is the one place
where a missing file is silent: the browser cannot fetch the script, the
topology page draws nothing, and nothing in the logs says why. So every
asset the interface needs ships with it, and this file is what stops a
convenient CDN URL from creeping back in.

The second half is about the language switch. The dictionary in
static/js/i18n.js was written for pages that then went on to hard-code
their English anyway, so here we check the other direction too: that every
key the pages ask for exists, in all three languages.
"""
import hashlib
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
STATIC = REPO / "static"
PAGES = sorted(STATIC.glob("*.html"))
SCRIPTS = sorted((STATIC / "js").glob("*.js"))

# static/vendor/README.md records this too, and says how to change it.
D3 = STATIC / "vendor" / "d3-7.9.0.min.js"
D3_SHA256 = "f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539"

# http://localhost:9200 is an example value in a form field, not a fetch.
ALLOWED_EXTERNAL = {"http://localhost:9200"}


def read(path):
    return path.read_text(encoding="utf-8")


# -- nothing is fetched from the internet ----------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_a_page_never_reaches_outside_for_an_asset(page):
    urls = set(re.findall(r"""["'(](https?://[^"')\s]+)""", read(page)))
    assert urls - ALLOWED_EXTERNAL == set(), (
        "%s loads something over the network. A management segment usually "
        "has no route to the internet, and the browser fails quietly. Ship "
        "the file in static/vendor/ instead." % page.name
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_never_reaches_outside_for_an_asset(script):
    urls = set(re.findall(r"""["'`](https?://[^"'`)\s]+)""", read(script)))
    assert urls - ALLOWED_EXTERNAL == set(), (
        "%s fetches from the network at runtime." % script.name
    )


def test_the_topology_page_loads_the_d3_that_ships_with_it():
    html = read(STATIC / "index.html")
    src = "/static/vendor/d3-7.9.0.min.js"
    assert '<script src="%s">' % src in html
    assert D3.is_file(), "%s is referenced but not in the repository" % src


def test_the_vendored_d3_is_the_file_we_checked():
    digest = hashlib.sha256(D3.read_bytes()).hexdigest()
    assert digest == D3_SHA256, (
        "static/vendor/d3-7.9.0.min.js is not the npm d3@7.9.0 build any more. "
        "If that is deliberate, follow static/vendor/README.md and update the "
        "hash here as well."
    )
    assert "d3js.org v7.9.0" in read(D3)[:200]


def test_the_vendored_file_ships_its_licence():
    licence = read(STATIC / "vendor" / "LICENSE.d3")
    assert "Mike Bostock" in licence
    assert "Permission to use, copy, modify" in licence


def test_the_server_serves_the_vendored_directory():
    # static/ is mounted whole, but a .gitignore or a stray build step could
    # leave the directory out of the image, so ask the application for it.
    import asyncio

    from httpx import ASGITransport, AsyncClient

    import app.main as main_module

    async def fetch():
        async with AsyncClient(
            transport=ASGITransport(app=main_module.app), base_url="http://test"
        ) as client:
            return await client.get("/static/vendor/d3-7.9.0.min.js")

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == D3_SHA256


# -- the language switch ---------------------------------------------------


def dictionaries():
    """The three translation tables in static/js/i18n.js, as key sets."""
    src = read(STATIC / "js" / "i18n.js")
    heads = [
        (m.group(1).strip("'"), m.start())
        for m in re.finditer(r"^        ('?[\w-]+'?): \{$", src, re.M)
    ]
    assert [name for name, _ in heads] == ["en", "zh-TW", "zh-CN"], heads
    bounds = [start for _, start in heads] + [len(src)]
    return {
        name: set(re.findall(r"^\s{12}([A-Za-z0-9_]+):", src[start:end], re.M))
        for (name, start), end in zip(heads, bounds[1:])
    }


def referenced_keys():
    keys = set()
    for page in PAGES:
        keys |= set(
            re.findall(
                r'data-i18n(?:-placeholder|-title)?="([A-Za-z0-9_]+)"', read(page)
            )
        )
    for script in SCRIPTS:
        keys |= set(re.findall(r"\bt\('([A-Za-z0-9_]+)'\)", read(script)))
    return keys


def test_no_language_shadows_its_own_key():
    # A key written twice in one object literal is not an error in
    # JavaScript - the later one quietly wins. The dictionary had thirteen
    # of them, four with different wording, so the page showed one string
    # while the file suggested another.
    src = read(STATIC / "js" / "i18n.js")
    heads = [
        (m.group(1).strip("'"), m.start())
        for m in re.finditer(r"^        ('?[\w-]+'?): \{$", src, re.M)
    ]
    bounds = [start for _, start in heads] + [len(src)]
    for (name, start), end in zip(heads, bounds[1:]):
        keys = re.findall(r"^\s{12}([A-Za-z0-9_]+):", src[start:end], re.M)
        twice = sorted({key for key in keys if keys.count(key) > 1})
        assert not twice, "%s defines %s more than once" % (name, twice)


def test_every_language_has_the_same_keys():
    tables = dictionaries()
    en = tables["en"]
    for lang, keys in tables.items():
        assert keys == en, "%s differs from en: %s" % (
            lang,
            sorted(keys ^ en),
        )


def test_nothing_asks_for_a_key_that_does_not_exist():
    tables = dictionaries()
    wanted = referenced_keys()
    assert wanted, "the parser found no i18n references at all"
    for lang, keys in tables.items():
        assert not (wanted - keys), "%s is missing %s" % (
            lang,
            sorted(wanted - keys),
        )


# -- the topology page in particular ---------------------------------------
#
# These are the strings that were visible in English on a Chinese page: the
# dictionary had them all along, the page just never asked.


@pytest.mark.parametrize(
    "english",
    [
        "Select Group...",
        "Refresh",
        "Fit",
        "Active Alerts",
        "Loading topology",
        "Details",
    ],
)
def test_a_topology_control_is_marked_for_translation(english):
    html = read(STATIC / "index.html")
    for match in re.finditer(r"<([a-z0-9]+)\b([^>]*)>([^<>]*)</\1>", html):
        if english in match.group(3):
            assert "data-i18n" in match.group(2), (
                "%r is on the topology page without data-i18n, so it stays "
                "English whatever language is chosen" % english
            )
            return
    pytest.fail("could not find %r in static/index.html" % english)


def test_the_timestamp_does_not_eat_its_own_label():
    # updateLastUpdate() used to overwrite #last-update wholesale, which threw
    # away the data-i18n span inside it: the label came back in English after
    # the first refresh and stayed there.
    html = read(STATIC / "index.html")
    assert 'id="last-update-time"' in html
    app_js = read(STATIC / "js" / "app.js")
    assert "last-update-time" in app_js
    assert "Last update:" not in app_js
