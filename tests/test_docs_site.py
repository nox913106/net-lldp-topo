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
