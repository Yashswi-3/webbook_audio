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



def test_chapter_listing_is_sorted_into_reading_order():
    # The shape webnovel's catalog really has: a "Read" button on chapter 1,
    # then a latest-updates block, then the full list starting at chapter 2.
    base = "https://www.webnovel.com/book/36163297308457405/"
    listing = [
        ("Read", base + "chapter-1-michael-jackson._97088154485448446"),
        ("Chapter 56: Hei Fan's inheritance", base + "chapter-56-hei-fan._98448090515689749"),
        ("Upper Moon's Falna. 1 months ago", base + "upper-moons-falna._97630234253253575"),
        ("2 Chapter 2: The meeting", base + "chapter-2-the-meeting._97088154485448447"),
        ("3 Chapter 3: The fight", base + "chapter-3-the-fight._97088154485448448"),
        ("4 Chapter 4: After", base + "chapter-4-after._97088154485448449"),
        ("5 Chapter 5: Later", base + "chapter-5-later._97088154485448450"),
        ("6 Chapter 6: Even later", base + "chapter-6-even-later._97088154485448451"),
    ]
    ordered = sources.order_chapter_listing(listing)
    nums = [sources.extract_chapter_number(l, u) for l, u in ordered]
    assert nums == [1, 2, 3, 4, 5, 6, 56, None], nums


def test_chapter_listing_keeps_document_order_when_mostly_unnumbered():
    # A book whose links carry no numbers has nothing to sort by, so the page's
    # own order is the best available answer and must survive untouched.
    listing = [("Prologue", "https://x.test/b/prologue"),
               ("The Arrival", "https://x.test/b/the-arrival"),
               ("Dusk", "https://x.test/b/dusk"),
               ("Chapter 4", "https://x.test/b/chapter-4")]
    assert sources.order_chapter_listing(listing) == listing


def test_missing_chapter_numbers_reports_holes_only_inside_the_range():
    chapters = [{"chapter_num": 1}, {"chapter_num": 3}, {"chapter_num": 4},
                {"chapter_num": 7}]
    assert sources.missing_chapter_numbers(chapters) == [2, 5, 6]
    # A complete run, a single chapter and an unnumbered book all have no gaps.
    assert sources.missing_chapter_numbers([{"chapter_num": 1},
                                            {"chapter_num": 2}]) == []
    assert sources.missing_chapter_numbers([{"chapter_num": 9}]) == []
    assert sources.missing_chapter_numbers([{"chapter_num": None},
                                            {"chapter_num": None}]) == []



def test_chunk_split_prefers_a_sentence_end_and_loses_nothing():
    try:
        import app as webapp
    except ImportError:          # flask / edge-tts absent: nothing to test here
        return

    words = []
    for i in range(60):
        words += [f"w{i}"] * 9 + [f"end{i}."]
    text = " ".join(words)                      # 600 words, a stop every 10th

    chunks = webapp.split_into_word_chunks(text, words_per_chunk=100)
    # Every cut lands on a sentence end rather than mid-sentence...
    for chunk in chunks[:-1]:
        assert chunk.rstrip().endswith("."), chunk[-40:]
    # ...the target size is respected within the lookback window...
    assert all(85 <= len(c.split()) <= 100 for c in chunks[:-1]),         [len(c.split()) for c in chunks]
    # ...and not one word is dropped or duplicated.
    assert " ".join(chunks).split() == text.split()


def test_chunk_split_falls_back_to_the_word_count_without_sentence_ends():
    try:
        import app as webapp
    except ImportError:
        return

    text = " ".join(["word"] * 250)              # no punctuation anywhere
    chunks = webapp.split_into_word_chunks(text, words_per_chunk=100)
    assert [len(c.split()) for c in chunks] == [100, 100, 50]
    assert " ".join(chunks).split() == text.split()


def test_streaming_chunker_crosses_chapter_boundaries_without_losing_words():
    try:
        import app as webapp
    except ImportError:
        return

    chunker = webapp.StreamingWordChunker(words_per_chunk=20)
    chapters = [
        " ".join(f"a{i}" for i in range(13)),
        " ".join([*(f"b{i}" for i in range(5)), "sentence.",
                  *(f"b{i}" for i in range(6, 19))]),
        " ".join(f"c{i}" for i in range(17)),
    ]

    chunks = []
    for chapter in chapters:
        chunks.extend(chunker.add_text(chapter))
    chunks.extend(chunker.flush())

    assert " ".join(chunks).split() == " ".join(chapters).split()
    assert all(len(chunk.split()) <= 20 for chunk in chunks)
    assert chunker.flush() == []


def test_streaming_audio_starts_before_finish():
    import os
    import shutil
    import tempfile
    import threading
    import app as webapp

    folder = tempfile.mkdtemp(prefix="wba_stream_")
    started = threading.Event()
    release = threading.Event()

    def fake_tts(text, path, voice, rate):
        started.set()
        assert release.wait(2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    try:
        builder = webapp.StreamingAudioBuilder(
            folder, "voice", "+0%", words_per_chunk=10,
            max_concurrent=1, tts_fn=fake_tts)
        builder.add_text(" ".join(f"w{i}" for i in range(10)))
        assert started.wait(1), "TTS did not start before collection finished"
        release.set()
        paths = builder.finish()
        assert [os.path.basename(p) for p in paths] == ["part1.mp3"]
    finally:
        release.set()
        shutil.rmtree(folder, ignore_errors=True)


def test_streaming_audio_returns_parts_in_text_order():
    import os
    import shutil
    import tempfile
    import app as webapp

    folder = tempfile.mkdtemp(prefix="wba_stream_")

    def fake_tts(text, path, voice, rate):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    words = [f"w{i}" for i in range(25)]
    try:
        builder = webapp.StreamingAudioBuilder(
            folder, "voice", "+0%", words_per_chunk=10,
            max_concurrent=2, tts_fn=fake_tts)
        builder.add_text(" ".join(words))
        paths = builder.finish()

        narrated = []
        for path in paths:
            with open(path, encoding="utf-8") as f:
                narrated.extend(f.read().split())
        assert [os.path.basename(p) for p in paths] == [
            "part1.mp3", "part2.mp3", "part3.mp3"]
        assert narrated == words
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_streaming_audio_respects_concurrency_limit():
    import shutil
    import tempfile
    import threading
    import time
    import app as webapp

    folder = tempfile.mkdtemp(prefix="wba_stream_")
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_tts(text, path, voice, rate):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        finally:
            with lock:
                active -= 1

    try:
        builder = webapp.StreamingAudioBuilder(
            folder, "voice", "+0%", words_per_chunk=5,
            max_concurrent=2, tts_fn=fake_tts)
        builder.add_text(" ".join(f"w{i}" for i in range(30)))
        builder.finish()
        assert max_active == 2
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_streaming_audio_abort_removes_finished_parts():
    import glob
    import os
    import shutil
    import tempfile
    import threading
    import time
    import app as webapp

    folder = tempfile.mkdtemp(prefix="wba_stream_")
    started = threading.Event()

    def fake_tts(text, path, voice, rate):
        started.set()
        time.sleep(0.05)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    try:
        builder = webapp.StreamingAudioBuilder(
            folder, "voice", "+0%", words_per_chunk=5,
            max_concurrent=1, tts_fn=fake_tts)
        builder.add_text(" ".join(f"w{i}" for i in range(20)))
        assert started.wait(1)
        builder.abort()
        assert glob.glob(os.path.join(folder, "part*.mp3")) == []
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_add_chapter_appends_before_emitting_the_same_object():
    chapters = []
    seen = []
    chapter = {"page": 1, "text": "hello"}

    sources._add_chapter(
        chapters, chapter,
        lambda emitted: seen.append((len(chapters), emitted)))

    assert chapters == [chapter]
    assert seen == [(1, chapter)]


def test_uploaded_text_emits_its_chapter():
    seen = []
    result = sources.fetch_uploaded_text(
        "one two three", "sample.txt", lambda page: None, seen.append)

    assert seen == result["chapters"]


def test_known_index_emits_chapters_in_listing_order():
    import threading

    first = "https://example.test/book/chapter-1"
    second = "https://example.test/book/chapter-2"
    second_finished = threading.Event()
    completion_order = []
    seen = []
    original = sources._read_one_chapter

    def fake_read(url):
        if url == first:
            assert second_finished.wait(1)
        else:
            second_finished.set()
        completion_order.append(url)
        return f"Title {url[-1]}", f"Text {url[-1]}"

    sources._read_one_chapter = fake_read
    try:
        chapters = sources.read_chapter_urls(
            [("Chapter 1", first), ("Chapter 2", second)],
            1, 2, lambda message: None, lambda page: None, seen.append)
    finally:
        sources._read_one_chapter = original

    assert completion_order == [second, first]
    assert [chapter["url"] for chapter in chapters] == [first, second]
    assert seen == chapters


def _run_fake_streaming_pipeline(handler, fake_tts, max_concurrent=2):
    import contextlib
    import glob
    import io
    import os
    import shutil
    import tempfile
    import app as webapp

    folder = tempfile.mkdtemp(prefix="wba_pipeline_")
    merged_parts = []
    original_output = webapp.OUTPUT_FOLDER
    original_merge = webapp._merge_via_ffmpeg_concat
    original_fallback = webapp._merge_via_pydub
    original_state = dict(webapp.job_state)
    missing = object()
    original_factory = getattr(webapp, "STREAMING_TTS_FACTORY", missing)
    original_handler = sources.HANDLERS.get("fake", missing)
    original_label = sources.SOURCE_LABELS.get("fake", missing)

    def factory(out_folder, voice, rate, progress_cb=None):
        return webapp.StreamingAudioBuilder(
            out_folder, voice, rate, progress_cb=progress_cb,
            words_per_chunk=10, max_concurrent=max_concurrent, tts_fn=fake_tts)

    def fake_merge(paths, final_path):
        merged_parts.extend(os.path.basename(path) for path in paths)
        with open(final_path, "wb") as f:
            f.write(b"merged")

    webapp.OUTPUT_FOLDER = folder
    webapp.STREAMING_TTS_FACTORY = factory
    webapp._merge_via_ffmpeg_concat = fake_merge
    webapp._merge_via_pydub = lambda paths, final_path: fake_merge(paths, final_path)
    sources.HANDLERS["fake"] = handler
    sources.SOURCE_LABELS["fake"] = "fake source"
    webapp.reset_job_state(1, source="fake")

    try:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            webapp.run_pipeline(
                "https://example.test/book/chapter-1", 1,
                "voice", "+0%", "fake")
        state = dict(webapp.job_state)
        result = {
            "state": state,
            "merged_parts": list(merged_parts),
            "part_files": glob.glob(os.path.join(folder, "part*.mp3")),
            "txt_exists": os.path.exists(os.path.join(folder, "book.txt")),
            "mp3_exists": os.path.exists(os.path.join(folder, "book.mp3")),
            "stderr": stderr.getvalue(),
        }
    finally:
        sources.set_cancel_check(None)
        webapp.OUTPUT_FOLDER = original_output
        webapp._merge_via_ffmpeg_concat = original_merge
        webapp._merge_via_pydub = original_fallback
        if original_factory is missing:
            delattr(webapp, "STREAMING_TTS_FACTORY")
        else:
            webapp.STREAMING_TTS_FACTORY = original_factory
        if original_handler is missing:
            sources.HANDLERS.pop("fake", None)
        else:
            sources.HANDLERS["fake"] = original_handler
        if original_label is missing:
            sources.SOURCE_LABELS.pop("fake", None)
        else:
            sources.SOURCE_LABELS["fake"] = original_label
        with webapp.job_lock:
            webapp.job_state.clear()
            webapp.job_state.update(original_state)
        shutil.rmtree(folder, ignore_errors=True)
    return result


def test_run_pipeline_starts_tts_before_source_returns():
    import threading

    tts_started = threading.Event()
    source_saw_tts = []

    def fake_tts(text, path, voice, rate):
        tts_started.set()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def handler(url, max_pages, on_status, on_page, on_chapter=None):
        assert on_chapter is not None
        chapter = {"page": 1, "url": url, "title": "Chapter 1",
                   "text": " ".join(f"w{i}" for i in range(25)),
                   "chapter_num": 1}
        on_chapter(chapter)
        source_saw_tts.append(tts_started.wait(1))
        return {"chapters": [chapter], "book_title": "Book"}

    result = _run_fake_streaming_pipeline(handler, fake_tts)

    assert source_saw_tts == [True]
    assert result["state"]["status"] == "done"
    assert result["state"]["mp3_ready"]
    assert result["merged_parts"] == [
        "part1.mp3", "part2.mp3", "part3.mp3"]
    assert result["part_files"] == []
    assert result["mp3_exists"]


def test_source_failure_aborts_streaming_audio_and_removes_parts():
    import threading
    import time

    tts_started = threading.Event()

    def fake_tts(text, path, voice, rate):
        tts_started.set()
        time.sleep(0.05)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def handler(url, max_pages, on_status, on_page, on_chapter=None):
        assert on_chapter is not None
        on_chapter({"page": 1, "url": url, "title": "Chapter 1",
                    "text": " ".join(["word"] * 10), "chapter_num": 1})
        assert tts_started.wait(1)
        raise sources.SourceError("source broke")

    result = _run_fake_streaming_pipeline(handler, fake_tts)

    assert tts_started.is_set()
    assert result["state"]["status"] == "error"
    assert not result["state"]["running"]
    assert not result["state"]["mp3_ready"]
    assert result["part_files"] == []
    assert not result["mp3_exists"]


def test_tts_failure_after_collection_keeps_txt_but_removes_audio_parts():
    import threading

    tts_failed = threading.Event()

    def fake_tts(text, path, voice, rate):
        tts_failed.set()
        raise RuntimeError("tts broke")

    def handler(url, max_pages, on_status, on_page, on_chapter=None):
        assert on_chapter is not None
        chapter = {"page": 1, "url": url, "title": "Chapter 1",
                   "text": " ".join(["word"] * 10), "chapter_num": 1}
        on_chapter(chapter)
        assert tts_failed.wait(1)
        return {"chapters": [chapter], "book_title": "Book"}

    result = _run_fake_streaming_pipeline(handler, fake_tts)

    assert result["state"]["status"] == "error"
    assert result["state"]["txt_ready"]
    assert result["txt_exists"]
    assert "RuntimeError: tts broke" in result["stderr"]
    assert result["part_files"] == []
    assert not result["mp3_exists"]


def test_cancellation_waits_for_streaming_workers_and_removes_parts():
    import threading
    import time
    import app as webapp

    tts_started = threading.Event()

    def fake_tts(text, path, voice, rate):
        tts_started.set()
        time.sleep(0.05)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def handler(url, max_pages, on_status, on_page, on_chapter=None):
        assert on_chapter is not None
        on_chapter({"page": 1, "url": url, "title": "Chapter 1",
                    "text": " ".join(["word"] * 10), "chapter_num": 1})
        assert tts_started.wait(1)
        webapp.update_state(cancel_requested=True)
        sources.check_cancelled()

    result = _run_fake_streaming_pipeline(handler, fake_tts)

    assert result["state"]["status"] == "stopped"
    assert not result["state"]["running"]
    assert result["part_files"] == []
    assert not result["mp3_exists"]


def test_cancellation_during_audio_wait_prevents_queued_tts_from_starting():
    import app as webapp

    calls = []

    def fake_tts(text, path, voice, rate):
        calls.append(text)
        if len(calls) == 1:
            webapp.update_state(cancel_requested=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def handler(url, max_pages, on_status, on_page, on_chapter=None):
        assert on_chapter is not None
        chapter = {
            "page": 1,
            "url": url,
            "title": "Chapter 1",
            "text": " ".join(f"w{i}" for i in range(30)),
            "chapter_num": 1,
        }
        on_chapter(chapter)
        return {"chapters": [chapter], "book_title": "Book"}

    result = _run_fake_streaming_pipeline(
        handler, fake_tts, max_concurrent=1)

    assert len(calls) == 1
    assert result["state"]["status"] == "stopped"
    assert result["part_files"] == []
    assert not result["mp3_exists"]


def test_retry_delay_honours_retry_after_in_both_legal_forms():
    import email.utils
    import time

    # Plain seconds.
    assert sources._retry_delay(1, "7") == 7.0
    # An HTTP-date, which RFC 9110 allows and a naive int() parse would drop
    # on the floor - retrying instantly and earning a longer ban.
    when = email.utils.formatdate(time.time() + 9, usegmt=True)
    assert 5 <= sources._retry_delay(1, when) <= 12
    # Nonsense header falls back to backoff rather than raising.
    assert sources._retry_delay(1, "soon") > 0
    # No header: backoff grows, and jitter keeps concurrent callers apart.
    assert sources._retry_delay(3, None) > sources._retry_delay(1, None) - 0.001
    assert sources._retry_delay(9, None) <= sources.JINA_MAX_SLEEP


def test_jina_retries_a_429_then_reports_it_as_a_throttle():
    calls = []

    class Fake429:
        status_code = 429
        headers = {"Retry-After": "0"}

    original_get = sources.requests.get
    original_sleep = sources.time.sleep
    sources.requests.get = lambda *a, **k: (calls.append(1), Fake429())[1]
    sources.time.sleep = lambda seconds: None
    try:
        raised = None
        try:
            sources.jina_read("https://example.test/chapter-1")
        except sources.SourceError as e:
            raised = e
    finally:
        sources.requests.get = original_get
        sources.time.sleep = original_sleep

    assert len(calls) == sources.JINA_ATTEMPTS, calls
    # A throttle, specifically: callers branch on this to keep a rate limit
    # from being reported as a book that has no more chapters.
    assert isinstance(raised, sources.SourceThrottled), raised
    assert "rate limit" in str(raised).lower()


def test_a_bot_check_page_is_never_narrated_as_a_chapter():
    challenge = ("Just a moment... Enable JavaScript and cookies to continue. "
                 "Ray ID 8f2c")
    assert sources.looks_like_challenge_text(challenge)
    # Length gate: a real chapter is allowed to contain those words.
    real_chapter = challenge + " " + " ".join(f"word{i}" for i in range(400))
    assert not sources.looks_like_challenge_text(real_chapter)
    assert not sources.looks_like_challenge_text("")


def test_a_host_that_refused_once_is_not_asked_again_this_job():
    attempts = []

    def fake_download(url):
        attempts.append(url)
        error = sources.requests.exceptions.HTTPError("403 Client Error")
        error.response = type("R", (), {"status_code": 403})()
        raise error

    def guarded(url):
        try:
            fake_download(url)
        except sources.requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in sources._DIRECT_REFUSAL_STATUS:
                sources._direct_refused_hosts.add(
                    sources.urlparse(url).netloc.casefold())
            return None, str(e)

    original = sources.download_page_html
    sources.download_page_html = guarded
    sources.reset_fetch_memory()
    try:
        first_html, first_err = sources.fetch_with_retry(
            "https://blocked.test/chapter-1")
        second_html, second_err = sources.fetch_with_retry(
            "https://blocked.test/chapter-2")
    finally:
        sources.download_page_html = original
        sources.reset_fetch_memory()

    assert first_html is None and second_html is None
    # One refused request, not four: the second page skipped the direct fetch
    # and the retry entirely and went straight to the reader.
    assert len(attempts) == 1, attempts
    assert "refused a direct fetch earlier" in second_err


def test_a_throttled_index_is_not_reported_as_a_book_without_chapters():
    def throttled(url):
        raise sources.SourceThrottled("Jina Reader is rate limiting this server")

    original = sources._page_links
    sources._page_links = throttled
    try:
        raised = None
        try:
            sources.discover_chapter_list("https://example.test/book/123456/c1",
                                          lambda message: None)
        except sources.SourceThrottled as e:
            raised = e
    finally:
        sources._page_links = original

    # Not [] - an empty listing here reads as "this site has no chapter list",
    # which is how a rate limit became a one-chapter audiobook.
    assert raised is not None


def test_health_reports_configuration_without_leaking_the_key():
    import app as webapp

    original = sources.JINA_API_KEY
    try:
        sources.JINA_API_KEY = "jina_secret_value"
        body = webapp.app.test_client().get("/health").get_json()
    finally:
        sources.JINA_API_KEY = original

    assert body["ok"] is True
    assert body["jina_key"] is True
    # The whole point: a boolean, never the value. Anyone can call /health.
    assert "jina_secret_value" not in str(body)
    for field in ("ffmpeg", "edge_tts", "video_download_enabled", "job_running"):
        assert isinstance(body[field], bool), field

    original2 = sources.JINA_API_KEY
    try:
        sources.JINA_API_KEY = ""
        empty = webapp.app.test_client().get("/health").get_json()
    finally:
        sources.JINA_API_KEY = original2
    assert empty["jina_key"] is False


def test_markdown_links_survive_parentheses_in_the_url():
    # The real shape that broke it: a book slug containing "(cote)", with the
    # optional markdown link title after the URL. The old regex stopped at the
    # first ")", so every chapter link lost the work id that scopes it to this
    # book, all 296 were discarded, and a 148-chapter novel narrated as one.
    md = (
        '1. [_1_ **Childhood Friend!**](https://www.webnovel.com/book/'
        'classroom-of-the-elite-i-have-the-ability-to-read-minds!-(cote)'
        '_28776837400218905/childhood-friend!_77247248521555336 '
        '"Childhood Friend!")' + chr(10) +
        '2. [Next](https://example.test/plain/chapter-2)' + chr(10)
    )
    links = sources.parse_markdown_links(md)
    assert len(links) == 2, links

    first_url = links[0][1]
    assert first_url.endswith("childhood-friend!_77247248521555336"), first_url
    assert "(cote)_28776837400218905" in first_url
    # The title must not be glued onto the URL.
    assert '"' not in first_url
    assert links[1][1] == "https://example.test/plain/chapter-2"

    # And the id survives, which is the whole point - it is what scopes a
    # chapter to this book rather than to the site's other novels.
    assert "28776837400218905" in first_url


def test_mobile_urls_use_the_desktop_host_for_the_chapter_index():
    mobile = ("https://m.webnovel.com/book/some-book_12345678901/"
              "a-chapter_22345678901")
    assert sources.desktop_equivalent(mobile) == (
        "https://www.webnovel.com/book/some-book_12345678901/"
        "a-chapter_22345678901")
    # Not a mobile host: left alone, so nothing is rewritten that shouldn't be.
    assert sources.desktop_equivalent("https://www.webnovel.com/book/x") is None
    assert sources.desktop_equivalent("https://example.test/mobile/x") is None
    # Same chapter on either host compares equal, so the opening chapter is
    # not collected twice when the index lists the desktop URLs.
    assert (sources._chapter_path(mobile)
            == sources._chapter_path(sources.desktop_equivalent(mobile)))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
