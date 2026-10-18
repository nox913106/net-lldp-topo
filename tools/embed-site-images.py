#!/usr/bin/env python3
"""Put docs/img/*.webp inside docs/index.html as data: URIs.

The site is published as a GitHub Pages static page, and it is easier to
hand around, mirror or open from a file:// path when it is one file. So
every <img> carries two things: data-img, which names the file in
docs/img/ it came from, and a src that holds that file's bytes.

data-img is what makes this repeatable - replace a screenshot in
docs/img/, run this again, and the page picks it up. Running it when
nothing changed rewrites nothing.

    python3 tools/embed-site-images.py           # embed
    python3 tools/embed-site-images.py --check   # only say if it is stale
"""
import argparse
import base64
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PAGE = REPO / "docs" / "index.html"
IMG = REPO / "docs" / "img"

TYPES = {".webp": "image/webp", ".png": "image/png", ".svg": "image/svg+xml"}


def data_uri(name):
    path = IMG / name
    if not path.is_file():
        raise SystemExit("docs/img/%s does not exist" % name)
    media = TYPES.get(path.suffix)
    if media is None:
        raise SystemExit("no media type for %s" % name)
    return "data:%s;base64,%s" % (
        media,
        base64.b64encode(path.read_bytes()).decode("ascii"),
    )


def embed(html):
    """Rewrite every <img> src from its data-img, adding data-img if the tag
    still points at img/ directly."""
    uris = {}
    embedded = []

    def one(match):
        tag = match.group(0)
        if tag.startswith("<!--"):
            # An <img> written inside a comment is documentation, not markup.
            return tag
        name = re.search(r'data-img="([^"]+)"', tag)
        if name is None:
            # First run: the tag still has src="img/x.webp".
            from_src = re.search(r'src="img/([^"]+)"', tag)
            if from_src is None:
                return tag
            name_text = from_src.group(1)
            tag = tag.replace(
                'src="img/%s"' % name_text,
                'data-img="%s" src=""' % name_text,
                1,
            )
        else:
            name_text = name.group(1)

        if name_text not in uris:
            uris[name_text] = data_uri(name_text)
        embedded.append(name_text)
        return re.sub(r'src="[^"]*"', 'src="%s"' % uris[name_text], tag, count=1)

    return (
        re.sub(r"<!--.*?-->|<img\b[^>]*>", one, html, flags=re.S),
        embedded,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/index.html does not match docs/img/",
    )
    args = parser.parse_args(argv)

    before = PAGE.read_text(encoding="utf-8")
    after, embedded = embed(before)

    if not embedded:
        raise SystemExit("found no <img> to embed in docs/index.html")

    if args.check:
        if after != before:
            print(
                "docs/index.html is out of date with docs/img/ - run "
                "python3 tools/embed-site-images.py",
                file=sys.stderr,
            )
            return 1
        print("docs/index.html matches docs/img/ (%d images)" % len(embedded))
        return 0

    if after == before:
        print("already up to date (%d images)" % len(embedded))
        return 0

    PAGE.write_text(after, encoding="utf-8")
    print(
        "embedded %d images (%d unique), docs/index.html is now %.1f MB"
        % (len(embedded), len(set(embedded)), len(after.encode()) / 1e6)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
