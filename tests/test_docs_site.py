"""
docs/index.html is the public page, published by GitHub Pages from /docs.

It is meant to be one file: the screenshots are base64 inside it, so the
page can be mirrored, mailed or opened from a file:// path without the
img/ directory following it around. The copies in docs/img/ stay in the
repository as the editable originals, and each <img> remembers which one
it came from in data-img - that is what makes the embedding repeatable,
and what these tests check has not drifted.

Google Fonts is the one thing still fetched from outside. It has a
system-font fallback, and anyone reading a public marketing page has
internet, so that one stays.
"""
import importlib.util
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PAGE = REPO / "docs" / "index.html"
IMG_DIR = REPO / "docs" / "img"
HTML = PAGE.read_text(encoding="utf-8")
# The build note at the top of the page talks about <img>; comments are not
# markup, so take them out before looking for tags.
MARKUP = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
TAGS = re.findall(r"<img\b[^>]*>", MARKUP)


def load_embedder():
    spec = importlib.util.spec_from_file_location(
        "embed_site_images", REPO / "tools" / "embed-site-images.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_page_has_its_screenshots():
    assert len(TAGS) >= 13, "expected the whole screenshot set, found %d" % len(TAGS)


@pytest.mark.parametrize("tag", TAGS, ids=range(len(TAGS)))
def test_every_image_is_embedded_and_says_where_it_came_from(tag):
    name = re.search(r'data-img="([^"]+)"', tag)
    assert name, (
        "an <img> has no data-img, so tools/embed-site-images.py cannot "
        "refresh it: %s" % tag[:120]
    )
    assert (IMG_DIR / name.group(1)).is_file()
    src = re.search(r'src="([^"]{0,40})', tag)
    assert src and src.group(1).startswith("data:image/"), (
        "%s is not embedded - the page would need docs/img/ beside it"
        % name.group(1)
    )


def test_no_image_is_still_loaded_from_the_img_directory():
    assert 'src="img/' not in MARKUP


def test_the_embedded_bytes_are_what_is_in_docs_img():
    # Re-derive the page from docs/img/ and compare. This is the same check
    # the script does with --check; running it here means a screenshot
    # swapped without re-embedding cannot reach main.
    module = load_embedder()
    again, embedded = module.embed(HTML)
    assert embedded, "the embedder found no images"
    assert again == HTML, (
        "docs/index.html is out of date with docs/img/ - run "
        "python3 tools/embed-site-images.py"
    )


def test_no_screenshot_in_the_repository_is_orphaned():
    used = set(re.findall(r'data-img="([^"]+)"', MARKUP))
    on_disk = {p.name for p in IMG_DIR.iterdir() if p.suffix in {".webp", ".png"}}
    assert on_disk == used, "docs/img/ and the page disagree: %s" % sorted(
        on_disk ^ used
    )


def test_the_page_needs_no_local_file_beside_it():
    # Any relative href/src other than a data: URI or an anchor would break
    # the "one file" promise. og:image is allowed to point at img/ because a
    # link preview scraper needs a URL, not a data: blob.
    refs = re.findall(r'(?:href|src)="([^"]+)"', MARKUP)
    local = [
        ref
        for ref in refs
        if not ref.startswith(("data:", "http://", "https://", "#", "mailto:"))
    ]
    assert local == [], "the page still depends on %s" % local


def test_the_social_preview_still_points_at_a_real_file():
    preview = re.search(r'property="og:image" content="([^"]+)"', MARKUP)
    assert preview, "og:image is gone"
    assert (REPO / "docs" / preview.group(1)).is_file()


# ---------------------------------------------------------------------------
# 點圖放大
#
# The screenshots are 1760px wide and sit in a column a third of that, so
# every one of them is wrapped in a button that opens it full screen. Three
# things can quietly break that: a new screenshot added without the wrapper
# (it would look clickable next to the others and do nothing), an id renamed
# on one side only, or a control added to the lightbox without translations.


SCRIPT = HTML[HTML.rindex("<script>") + len("<script>") : HTML.rindex("</script>")]


def dictionaries():
    """The I18N blocks in the page's script, as {language: set of keys}."""
    blocks = re.findall(r'\n    "([\w-]+)": \{\n(.*?)\n    \}', SCRIPT, re.S)
    found = {lang: set(re.findall(r'"([\w.]+)":', body)) for lang, body in blocks}
    assert sorted(found) == ["en", "zh-CN"], (
        "expected the two translated dictionaries, found %s" % sorted(found)
    )
    return found


def test_every_screenshot_opens_when_it_is_clicked():
    wrapped = re.findall(r'<button type="button" class="zoom"><img\b[^>]*>', MARKUP)
    assert len(wrapped) == len(TAGS), (
        "%d of %d screenshots are inside a .zoom button - the rest would "
        "look clickable and do nothing" % (len(wrapped), len(TAGS))
    )


def test_the_zoomed_layer_is_on_the_page_and_starts_closed():
    layer = re.search(r'<div class="lightbox" id="lightbox"[^>]*>', MARKUP)
    assert layer, "the lightbox layer is gone, so clicking a screenshot has nowhere to go"
    assert "hidden" in layer.group(0), "the lightbox would cover the page on load"
    assert 'role="dialog"' in layer.group(0)
    assert 'aria-modal="true"' in layer.group(0)


def test_the_script_finds_every_element_it_reaches_for():
    ids = set(re.findall(r'\bid="([^"]+)"', MARKUP))
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', SCRIPT))
    assert wanted, "the script stopped looking anything up"
    assert wanted <= ids, "the script asks for %s, which is not in the page" % sorted(
        wanted - ids
    )


@pytest.mark.parametrize("key", ["Escape", "ArrowLeft", "ArrowRight"])
def test_the_zoomed_layer_answers_the_keyboard(key):
    assert '"%s"' % key in SCRIPT, (
        "%s is not handled - the lightbox would trap a keyboard user" % key
    )


@pytest.mark.parametrize(
    "key", ["lb.actual", "lb.fit", "lb.close", "lb.prev", "lb.next", "lb.hint"]
)
def test_every_lightbox_control_is_marked_for_translation(key):
    assert 'data-i18n="%s"' % key in MARKUP


def test_every_language_translates_every_string_the_page_marks():
    # zh-TW is the page itself, the other two come from the dictionaries, so a
    # string added to the markup without a translation is a visible gap.
    marked = set(re.findall(r'data-i18n="([^"]+)"', MARKUP))
    for lang, keys in dictionaries().items():
        assert marked - keys == set(), "%s is missing %s" % (
            lang,
            sorted(marked - keys),
        )
        assert keys - marked == set(), "%s translates %s, which the page no longer uses" % (
            lang,
            sorted(keys - marked),
        )


def test_the_zoomed_layer_sits_above_the_sticky_navigation():
    # The first cut of this put the lightbox under the sticky nav, and the
    # close button could not be clicked at the top of the screen. Compare the
    # two numbers instead of hard-coding either.
    style = "\n".join(re.findall(r"<style>(.*?)</style>", HTML, re.S))

    def layer(selector):
        rule = re.search(re.escape(selector) + r" \{(.*?)\n\}", style, re.S)
        assert rule, "%s has no rule" % selector
        found = re.search(r"z-index: (\d+)", rule.group(1))
        assert found, "%s has no z-index" % selector
        return int(found.group(1))

    assert layer(".lightbox") > layer(".nav"), (
        "the navigation would cover the lightbox toolbar and swallow clicks "
        "on the close button"
    )
