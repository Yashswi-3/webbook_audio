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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
