"""
Offline checks for sources.py — no network, no ffmpeg, no API keys.

    python test_sources.py        (or: python -m pytest test_sources.py -q)
"""

import sources


def test_url_security_blocks_private_targets():
    for bad in [
        "http://localhost/x", "http://127.0.0.1/x", "http://0177.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/", "http://[::1]/x",
        "http://10.0.0.5/x", "http://192.168.1.1/x", "http://box.local/x",
        "file:///etc/passwd", "http://user:pw@example.com/x",
        "http://metadata.google.internal/", "gopher://example.com/", "",
    ]:
        try:
            sources.normalize_public_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"should have been rejected: {bad!r}")

    assert sources.normalize_public_url("example.com/a") == "https://example.com/a"
    assert sources.normalize_public_url("http://example.com/a") == "http://example.com/a"


def test_host_matches_rejects_lookalikes():
    assert sources.host_matches("https://www.youtube.com/watch?v=1", "youtube.com")
    assert sources.host_matches("https://youtu.be/abc", "youtu.be")
    assert not sources.host_matches("https://youtube.com.evil.test/x", "youtube.com")
    assert not sources.host_matches("https://youtube.com@evil.test/x", "youtube.com")
    assert not sources.host_matches("https://notyoutube.com/x", "youtube.com")


def test_router_dispatch():
    cases = {
        "https://www.youtube.com/watch?v=abc": "youtube",
        "https://youtu.be/abc": "youtube",
        "https://github.com/Yashswi-3/webbook_audio": "github",
        "https://example.com/blog/feed": "rss",
        "https://example.com/index.xml": "rss",
        "https://example.com/chapter-1": "web",
        "https://youtube.com.evil.test/watch": "web",
    }
    for url, expected in cases.items():
        assert sources.route(url) == expected, url
    assert set(sources.HANDLERS) == {"web", "youtube", "github", "rss"}


def test_content_type_routes_unmarked_feeds_to_rss():
    # hnrss.org/frontpage has no /feed or .xml in the path - only the
    # Content-Type from validate_url's HEAD identifies it.
    url = "https://hnrss.org/frontpage"
    assert sources.route(url) == "web"
    assert sources.route(url, "application/xml; charset=utf-8") == "rss"
    assert sources.route(url, "text/html; charset=utf-8") == "web"


def test_vtt_dedupes_rolling_captions():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
the quick brown

00:00:03.000 --> 00:00:05.000
the quick brown fox

00:00:05.000 --> 00:00:07.000
<c>jumps over</c> the lazy dog

00:00:07.000 --> 00:00:09.000
jumps over the lazy dog
"""
    assert sources.parse_vtt(vtt) == "the quick brown fox jumps over the lazy dog"


def test_markdown_is_stripped_for_speech():
    md = """# Title

Some [linked text](https://example.com) and `inline code`.

```python
print("this should not be narrated")
```

- bullet one
> a quote

| a | b |
|---|---|
| 1 | 2 |
"""
    out = sources.markdown_to_speech_text(md)
    assert "print(" not in out and "```" not in out
    assert "https://example.com" not in out
    assert "linked text" in out and "inline code" in out
    assert out.splitlines()[0] == "Title"
    assert "---" not in out


def test_jina_header_is_stripped():
    body = ("Title: My Article\n"
            "URL Source: https://example.com/a\n"
            "Markdown Content:\n"
            "# My Article\n\nReal body text here.\n")
    stripped = sources._JINA_HEADER_RE.sub("", body, count=1)
    assert not stripped.startswith("Title:")
    assert "URL Source" not in stripped
    assert "Real body text here." in stripped


def test_antibot_detection():
    assert sources._is_jina_antibot(
        "Title: Attention Required! | Cloudflare\nRay ID: 8ab\n")
    assert not sources._is_jina_antibot("Title: A normal article\nSome text\n")


def test_ytdlp_progress_parsing():
    assert sources.parse_progress_percent(
        "[download]  89.1% of   34.84MiB at    5.05MiB/s ETA 00:00") == 89.1
    assert sources.parse_progress_percent(
        "[download] 100% of   34.84MiB in 00:00:05 at 6.84MiB/s") == 100.0
    assert sources.parse_progress_percent("[download]   0.0% of ~1.00MiB") == 0.0
    for noise in ["[Merger] Merging formats into \"video.mp4\"",
                  "[youtube] aircAruvnKk: Downloading webpage", "", None]:
        assert sources.parse_progress_percent(noise) is None


def test_resolve_source_skips_network_probe_for_handler_sources():
    """
    YouTube/GitHub/RSS must resolve without any HTTP call. Regression test for
    a cloud deploy rejecting every YouTube link with "URL returned HTTP 403",
    because youtube.com 403s HEAD requests from datacenter IPs even though
    yt-dlp (which uses a different API entirely) would have worked.
    """
    import sources as s
    called = []
    original = s.validate_url
    s.validate_url = lambda u: (called.append(u), (True, None, ""))[1]
    try:
        assert s.resolve_source("https://www.youtube.com/watch?v=x") == ("youtube", None)
        assert s.resolve_source("https://github.com/a/b") == ("github", None)
        assert s.resolve_source("https://example.com/blog/feed") == ("rss", None)
        assert called == [], f"probed the network for: {called}"

        # The generic crawler still gets probed - it fetches the URL directly.
        assert s.resolve_source("https://example.com/article") == ("web", None)
        assert called == ["https://example.com/article"]
    finally:
        s.validate_url = original

    # Security check still applies to every source.
    for bad in ["http://127.0.0.1/x", "http://169.254.169.254/"]:
        name, err = s.resolve_source(bad)
        assert name is None and err


def test_markdown_next_link():
    md = ("Some text [end-users](https://x.test/a) more.\n"
          "[Next](/s/123/45/) and [Previous](/s/123/43/)\n")
    assert sources.find_next_link_markdown(md, "https://m.example.test/s/123/44/") \
        == "https://m.example.test/s/123/45/"
    assert sources.find_next_link_markdown("no links here", "https://x.test/") is None
    # Must not follow anchors or javascript: hrefs.
    assert sources.find_next_link_markdown("[Next](#bottom)", "https://x.test/") is None
    assert sources.find_next_link_markdown(
        "[Next](javascript:go())", "https://x.test/") is None


def test_validate_url_lets_refusals_through():
    # 404/410 are fatal; a 403 must not veto the job, because the Jina
    # fallback in fetch_crawl routinely reads pages that refuse a direct
    # fetch from a datacenter IP.
    assert sources._FATAL_STATUS == {404, 410}


def test_url_shape_groups_siblings():
    shape = sources._url_shape
    a = shape("https://x.test/book/123/9001")
    b = shape("https://x.test/book/123/9002")
    assert a == b, "sibling chapters must share a shape"
    assert shape("https://x.test/profile/7") != a
    assert shape("https://other.test/book/123/9001") != a, "host is part of it"


def test_parent_url():
    p = sources._parent_url
    assert p("https://x.test/book/123/9001") == "https://x.test/book/123"
    assert p("https://x.test/book/123/") == "https://x.test/book"
    assert p("https://x.test/book") is None
    assert p("https://x.test/") is None


def test_find_chapter_links_picks_the_biggest_group():
    index = "https://x.test/book/123"
    chrome = [
        ("Home", "https://x.test/"),
        ("Profile", "https://x.test/profile/7"),
        ("Twitter", "https://twitter.test/someone"),      # offsite, ignored
    ]
    chapters = [(f"Chapter {n}", f"https://x.test/book/123/90{n:02d}")
                for n in range(1, 11)]
    links = chrome + chapters + [chapters[-1]]            # trailing dupe

    found = sources.find_chapter_links(links, index)
    assert [u for _l, u in found] == [u for _l, u in chapters], \
        "document order, deduped, chrome excluded"

    # A handful of siblings must NOT be mistaken for a chapter list: a chapter
    # page's own nav has a few, and treating those as the book is worse than
    # finding nothing.
    few = chrome + chapters[:3]
    assert sources.find_chapter_links(few, index) == []


def test_chapter_links_reject_other_works():
    """
    Regression: the largest link group on a book page was the "recommended
    for you" carousel, so the crawler narrated the first chapter of fifteen
    unrelated novels. Chapters of one book share that book's id; every
    recommendation carries a different one.
    """
    index = "https://m.x.test/book/35069975308849905"
    ids = sources.work_ids(
        "https://m.x.test/book/35069975308849905/97494949762745599")

    recommendations = [(f"Some Other Novel {n}", f"https://m.x.test/book/3179471110087{n:04d}")
                       for n in range(20)]
    chapters = [(f"Chapter {n}", f"https://m.x.test/book/35069975308849905/9424742338742{n:04d}")
                for n in range(10)]

    picked = sources.find_chapter_links(recommendations + chapters, index, ids)
    assert len(picked) == 10, "must pick the chapters, not the bigger carousel"
    for _label, url in picked:
        assert "35069975308849905" in url, f"different book leaked in: {url}"

    # With only the carousel present, finding nothing beats finding the wrong book.
    assert sources.find_chapter_links(recommendations, index, ids) == []


def test_work_root_stops_at_the_book():
    ids = sources.work_ids("https://x.test/book/12345678/99887766")
    assert sources._work_root("https://x.test/book/12345678/99887766", ids) \
        == "https://x.test/book/12345678"
    # Already at the book page: stay there, don't climb to /book.
    assert sources._work_root("https://x.test/book/12345678", ids) \
        == "https://x.test/book/12345678"


def test_bot_check_detection():
    # The exact wording yt-dlp emits, as seen on the Render deploy.
    assert sources.blocked_by_bot_check(
        "ERROR: [youtube] X: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication.")
    assert sources.blocked_by_bot_check("Use --cookies-from-browser")
    assert not sources.blocked_by_bot_check(
        "ERROR: [youtube] X: Video unavailable")
    assert not sources.blocked_by_bot_check("[download] 100% of 34MiB")
    assert not sources.blocked_by_bot_check("")


def test_video_download_rejects_non_youtube_before_any_request():
    # Both checks happen before yt-dlp is invoked, so this stays offline.
    for bad in ["https://example.com/video", "http://127.0.0.1/v",
                "https://youtube.com.evil.test/watch?v=1"]:
        try:
            sources.download_video(bad, "/tmp", lambda m: None)
        except (sources.SourceError, ValueError):
            continue
        raise AssertionError(f"should have been rejected: {bad}")


def test_clean_text_drops_nav_boilerplate():
    out = sources.clean_text("Home\n\nReal    sentence here.\nNext\nWe use cookies")
    assert out == "Real sentence here."


# ----------------------------------------------------------------------------
# Bug 1 - download filenames were garbage
# ----------------------------------------------------------------------------

def test_extract_book_title_strips_site_brand_and_chapter_marker():
    # Real <title> tags captured from webnovel and fanfiction.net. The old
    # code only split the site brand off on "|" (so "- WebNovel" survived),
    # anchored its chapter-word check with ^ (so a mid-string "Chapter 1"
    # wasn't recognised), then picked the longest segment - producing the
    # whole raw title as the "book name" in both cases.
    html = ("<title>Muzan: Conquering multiverse. Chapter 1 - Chapter 1: "
            "Michael Jackson. - WebNovel</title>")
    got = sources.extract_book_title(html, "https://www.webnovel.com/book/x/y")
    assert got == "Muzan: Conquering multiverse"
    assert sources.sanitize_filename(f"{got} 1-19") == "Muzan Conquering multiverse 1-19"

    html2 = ("<title>Tales From Camp Lakewood Chapter 1: School's Out!, "
             "a loud house fanfic</title>")
    got2 = sources.extract_book_title(html2, "https://www.fanfiction.net/s/13857537/1/")
    assert got2 == "Tales From Camp Lakewood"
    assert sources.sanitize_filename(f"{got2} 1-44") == "Tales From Camp Lakewood 1-44"


def test_extract_book_title_from_titles_prefers_common_prefix():
    # fetch_crawl now collects every page's raw <title>, not just the first -
    # the longest common prefix across all of them (after per-title cleaning)
    # is the reliable, site-agnostic signal for the book name.
    titles = [
        "Muzan: Conquering multiverse. Chapter 1 - Chapter 1: Michael Jackson. - WebNovel",
        "Muzan: Conquering multiverse. Chapter 2 - Chapter 2: Assigning Tasks. - WebNovel",
        "Muzan: Conquering multiverse. Chapter 3 - Chapter 3: Entertainment District. - WebNovel",
    ]
    got = sources.extract_book_title_from_titles(
        titles, "https://www.webnovel.com/book/muzan-conquering-multiverse._x/y")
    assert got == "Muzan: Conquering multiverse"

    # A single collected title still works (non-serial pages, or a crawl that
    # only ever got one page).
    assert sources.extract_book_title_from_titles(
        ["Tales From Camp Lakewood Chapter 1: School's Out!, a loud house fanfic"],
        "https://www.fanfiction.net/s/13857537/1/") == "Tales From Camp Lakewood"

    # No titles at all: fall back to the netloc, never crash.
    assert sources.extract_book_title_from_titles(
        [], "https://example.com/a") == "example.com"


def test_extract_chapter_number_tries_title_then_url_then_bare_segment():
    # Title match, any of the recognised words.
    assert sources.extract_chapter_number("Chapter 12: Battle", "https://x.test/a") == 12
    assert sources.extract_chapter_number("Episode 4", "https://x.test/a") == 4
    assert sources.extract_chapter_number("Part #7", "https://x.test/a") == 7

    # No title match: fall back to a chapter-N token in the URL.
    assert sources.extract_chapter_number("", "https://x.test/book/chapter-9") == 9

    # No title, no URL token: a bare numeric path segment of at most 5
    # digits - this is what makes fanfiction.net's /s/13857537/1/ yield 1.
    assert sources.extract_chapter_number("", "https://www.fanfiction.net/s/13857537/1/") == 1

    # The 8-digit story id must never be mistaken for the chapter number.
    assert sources.extract_chapter_number("", "https://www.fanfiction.net/s/13857537/") is None
    assert sources.extract_chapter_number(
        "", "https://www.webnovel.com/book/x/97088154485448446") is None


# ----------------------------------------------------------------------------
# Bug 2 - chapter-index fallback never fired on slug-heavy sites, and mixed
# up novels on id-only sites
# ----------------------------------------------------------------------------

# 10 real chapter URLs from webnovel's /catalog page for "Muzan: Conquering
# multiverse" (site-relative, as captured), plus 2 synthetic URLs for a
# different book in the same URL shape, to prove cross-novel rejection.
_WEBNOVEL_BOOK = "https://www.webnovel.com/book/muzan-conquering-multiverse._36163297308457405"
_WEBNOVEL_CHAPTER_PATHS = [
    "/chapter-1-michael-jackson._97088154485448446",
    "/chapter-2-assigning-tasks._97103132378587911",
    "/chapter-3-entertainment-district._97125659079403012",
    "/chapter-4-hashiras-arrive._97131854301290125",
    "/chapter-5-final-boss-appears._97142982158898403",
    "/chapter-6-immune-to-sun._97156987107732416",
    "/chapter-7-nezuko-cured-reluctant-akaza-otherworld._97171119697613874",
    "/chapter-8-nakime!-close-that-fcking-door!._97172264977486661",
    "/chapter-9-danmachi._97198733116103153",
    "/chapter-10-gather-six-divinities._97210017874238150",
]
_OTHER_BOOK = "https://www.webnovel.com/book/some-other-novel._24681357924681357"
_OTHER_BOOK_PATHS = [
    "/chapter-1-first._13579246813579246",
    "/chapter-2-second._13579246813579247",
]


def test_find_chapter_links_coarsens_slug_heavy_urls():
    # Regression: each chapter's own title slug is baked into its URL, so
    # digit-run collapse alone spreads 10+ real chapters across as many
    # distinct shapes - none near MIN_INDEX_LINKS. The coarse second pass
    # (slug segments -> "*") must still find them once the work-prefix guard
    # has scoped candidates to one work.
    chapter_urls = [_WEBNOVEL_BOOK + p for p in _WEBNOVEL_CHAPTER_PATHS]
    other_urls = [_OTHER_BOOK + p for p in _OTHER_BOOK_PATHS]
    links = ([("Home", "https://www.webnovel.com/")]
             + [(f"Chapter {i+1}", u) for i, u in enumerate(chapter_urls)]
             + [("Some Other Novel", u) for u in other_urls])

    fine_shapes = {sources._url_shape(u) for u in chapter_urls}
    assert len(fine_shapes) > 1, "fixture should reproduce the fine-shape split"

    index_url = _WEBNOVEL_BOOK + "/catalog"
    pasted_url = chapter_urls[0]          # what the user actually pasted
    ids = sources.work_ids(pasted_url)
    work_prefix = sources._work_prefix(pasted_url)

    found = sources.find_chapter_links(links, index_url, ids, work_prefix)
    assert [u for _l, u in found] == chapter_urls
    for _l, u in found:
        assert "some-other-novel" not in u


def test_work_prefix_scopes_slug_only_sites_with_no_numeric_id():
    # freewebnovel-style URLs carry no 6+ digit id anywhere, so the old
    # id-only guard (`ids` empty) fell back to "lives under the index page",
    # which admits every novel listed on a site-wide index. The work-prefix
    # guard (pasted URL minus its last segment) catches this with no id at all.
    pasted = "https://freewebnovel.test/novel/some-great-story/chapter-5"
    prefix = sources._work_prefix(pasted)
    assert prefix == "https://freewebnovel.test/novel/some-great-story"

    same_work = [(f"Chapter {n}",
                  f"https://freewebnovel.test/novel/some-great-story/chapter-{n}")
                 for n in range(1, 12)]
    other_work = [(f"Other {n}",
                   f"https://freewebnovel.test/novel/a-different-story/chapter-{n}")
                  for n in range(1, 12)]
    index_url = "https://freewebnovel.test/novel/some-great-story"

    found = sources.find_chapter_links(same_work + other_work, index_url,
                                       ids=set(), work_prefix=prefix)
    assert len(found) == 11
    for _l, u in found:
        assert "a-different-story" not in u


# ----------------------------------------------------------------------------
# Bug 3 - fanfiction.net reads one chapter and stops
# ----------------------------------------------------------------------------

def test_guess_next_numeric_url_increments_short_trailing_numbers():
    # The real case: fanfiction.net's chapter nav is a <select> plus JS
    # onclick buttons, so there is no <a href> to another chapter anywhere on
    # the page and both find_next_link and find_next_link_markdown are blind.
    assert sources.guess_next_numeric_url("https://www.fanfiction.net/s/13857537/1/") \
        == "https://www.fanfiction.net/s/13857537/2/"
    assert sources.guess_next_numeric_url("https://x.test/story/chapter-5") \
        == "https://x.test/story/chapter-6"
    assert sources.guess_next_numeric_url("https://x.test/story/chapter_9") \
        == "https://x.test/story/chapter_10"

    # Must never increment a long site-assigned id - webnovel's chapter URLs
    # end in a 17-18 digit id, and incrementing that walks into nonsense.
    assert sources.guess_next_numeric_url(
        "https://www.webnovel.com/book/x/97088154485448446") is None
    assert sources.guess_next_numeric_url(
        "https://www.fanfiction.net/s/13857537/") is None  # 8-digit story id alone

    # Nothing to increment at all.
    assert sources.guess_next_numeric_url("https://x.test/about") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
