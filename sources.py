"""
Source router — WebBook Audio Reader v2
=======================================

One job: turn whatever URL the user pastes into a list of chapters.

    {"page": n, "url": str, "title": str, "text": str, "chapter_num": int|None}

Everything downstream (book.txt, edge-tts chunking, ffmpeg merge, the download
buttons) is unchanged — it just gets fed from more places now.

Routing and the zero-login command set are ported from the Agent Reach skill
(https://github.com/Panniantong/Agent-Reach, MIT). Its login-backed platforms
(Reddit, Twitter/X, Instagram, Facebook, LinkedIn, XiaoHongShu) are deliberately
NOT here: they need browser cookies this app has no way to hold. Its zero-login
platforms that aren't narratable (V2EX threads, Xueqiu stock quotes, Bilibili)
are left out too.

Handler contract:
    handler(url, max_pages, on_status, on_page, on_chapter=None)
        -> {"chapters": [...], "book_title": str}
  on_status(str)   - one-line progress message for the UI
  on_page(dict)    - append to the per-page log
  on_chapter(dict) - optional ordered callback for each accepted chapter
  raises SourceError(msg) on anything the user needs to know about
"""

import base64
import glob as _glob
import email.utils
import html as _html
import ipaddress
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, urlsplit

import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    import feedparser
except ImportError:
    feedparser = None


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

REQUEST_TIMEOUT = 20
PAGE_LOAD_WAIT_MS = 2500
# Parallel fetches when the chapter list is known up front (the index path).
# Low on purpose: politeness to the site being read, and Jina's anonymous
# limit is roughly 20 requests a minute.
CHAPTER_FETCH_CONCURRENCY = 4
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 WebBookAudioReader/2.0"
)

# Jina Reader — free without a key (rate-limited to roughly 20 req/min).
# Set JINA_API_KEY in the environment to lift that.
JINA_ENDPOINT = "https://r.jina.ai/"
JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()
JINA_TIMEOUT = 45              # it renders JS server-side, so it is slower
JINA_MAX_BYTES = 5 * 1024 * 1024
# Measured 2026-08-15: sending the full Chrome UA above gets a flat 403 from
# r.jina.ai; a short honest agent string gets 200. Don't "upgrade" this to the
# browser UA.
JINA_USER_AGENT = "WebBookAudioReader/2.0"
# Anonymous Jina is 20 requests/minute enforced per client IP, and a cloud host
# shares that IP with every other tenant on its NAT gateway - so a 429 here is
# routine and says nothing about the book. Retry it properly. A free JINA_API_KEY
# raises the ceiling to 500/min tracked per key instead of per IP, which is the
# single most effective setting on a deployed instance.
JINA_ATTEMPTS = 3
JINA_BACKOFF_BASE = 2.0        # seconds; doubled per attempt, plus jitter
JINA_MAX_SLEEP = 30.0          # never sit on a Retry-After longer than this

# Below this word count a local extraction is treated as a failure and the
# Jina Reader rescue path is tried. JS-rendered pages land here on servers
# with no Chromium installed, which is exactly the Render deployment.
MIN_ARTICLE_WORDS = 120

YTDLP_TIMEOUT = 600            # playlists take a while

# YouTube's bot check is a moving target. Measured 2026-08-15: the `default`
# and `android_vr` clients hit "Sign in to confirm you're not a bot", while
# these three get through without any cookie. If captions start failing,
# this list is the knob to turn - see `yt-dlp --extractor-args "youtube:..."`.
YTDLP_PLAYER_CLIENTS = "web_safari,mweb,web_embedded"
# Explicit variants only. A glob like "en.*" makes yt-dlp request every
# auto-translated track (~40 of them) and get 429'd.
YTDLP_SUB_LANGS = "en,en-US,en-GB,en-orig"

# Video download (v2.5) uses a DIFFERENT client from captions. Measured
# 2026-08-15: the caption clients above reach subtitle tracks but report
# "only images are available" for formats; `android` is the one that returns
# a real stream without any cookie. What it returns is format 18 - a single
# combined 640x360 h264/aac file. The 720p+ adaptive streams sit behind the
# bot check, so 360p is the ceiling for anonymous downloads. Cookies would
# lift it; this project deliberately doesn't do cookies.
YTDLP_VIDEO_CLIENT = "android"
# YouTube's bot-check refusal. It shows up constantly on cloud hosts, because
# datacenter IP ranges are presumed to be scrapers, and almost never on a home
# connection. Worth its own message: the raw yt-dlp text tells the user to pass
# cookies, which is not something this app does or should suggest.
_BOT_CHECK_SIGNS = ("sign in to confirm", "confirm you're not a bot",
                    "confirm you are not a bot", "--cookies-from-browser")
YTDLP_VIDEO_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
MAX_VIDEO_MB = 500             # guard against filling the disk on a long video
_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")

GITHUB_API = "https://api.github.com"


class SourceError(Exception):
    """Anything that should stop the job and be shown to the user verbatim."""


class SourceThrottled(SourceError):
    """
    A source refused *for now* — a rate limit, not an absence of content.

    These need opposite handling and used to be the same exception, which is
    how a rate-limited index page came back as "this book has no chapter
    list" and narrated 1 chapter of the 25 asked for. A throttle is retried,
    and if it survives that, it is reported as a throttle.
    """


class JobCancelled(Exception):
    """Raised at a loop boundary when the user has asked the job to stop."""


# ponytail: module-level, safe only because the app runs one job at a time -
# the same invariant gunicorn's `-w 1` already depends on. If this ever grows
# concurrent jobs, this becomes per-job state and must move into the handler
# signatures along with everything else in job_state.
_cancel_check = None


def set_cancel_check(fn):
    """Install the predicate that check_cancelled() consults. None clears it."""
    global _cancel_check
    _cancel_check = fn


def check_cancelled():
    """Raise JobCancelled if the user pressed Stop. Call at loop boundaries."""
    if _cancel_check is not None and _cancel_check():
        raise JobCancelled("Stopped.")


def _add_chapter(chapters, chapter, on_chapter=None):
    """Append an accepted chapter, then emit it in that same reading order."""
    chapters.append(chapter)
    if on_chapter:
        on_chapter(chapter)


# ----------------------------------------------------------------------------
# URL security
# ----------------------------------------------------------------------------
# This app fetches user-supplied URLs from the server. Without this check,
# anyone who can reach the deployed page can make it fetch http://localhost,
# 169.254.169.254 (cloud metadata), or hosts on the private network, and then
# read the result back out of book.txt. Ported from Agent Reach's
# agent_reach/utils/url.py (MIT).

_BLOCKED_HOSTS = {
    "home.arpa", "instance-data", "internal", "ip6-localhost", "ip6-loopback",
    "lan", "local", "localdomain", "localhost", "metadata.google.internal",
}
_BLOCKED_SUFFIXES = (
    ".home.arpa", ".internal", ".lan", ".local", ".localdomain", ".localhost",
)


def _literal_ip(host):
    """Parse canonical and legacy IPv4 spellings (e.g. 0177.0.0.1) without DNS."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def normalize_public_url(url):
    """
    Return a normalized URL, or raise ValueError if it isn't clearly a public
    http(s) target. Missing scheme is filled in as https://.
    """
    candidate = str(url or "").strip()
    if (
        not candidate
        or "\\" in candidate
        or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in candidate)
    ):
        raise ValueError("Only public http(s) URLs are allowed.")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port          # accessing .port rejects malformed authorities
    except (TypeError, ValueError):
        raise ValueError("Only public http(s) URLs are allowed.") from None

    literal = _literal_ip(host)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
        or host in _BLOCKED_HOSTS
        or host.endswith(_BLOCKED_SUFFIXES)
        or ("." not in host and literal is None)
        or (literal is not None and not literal.is_global)
    ):
        raise ValueError("Only public http(s) URLs are allowed.")

    return parsed.geturl()


def host_matches(url, *domains):
    """True if url's host is one of domains, or a real subdomain of one.

    Uses the parsed hostname rather than a substring test, so lookalikes like
    youtube.com.evil.test and userinfo disguises like youtube.com@evil.test
    do not match.
    """
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not host or parsed.username is not None or parsed.password is not None:
        return False
    for d in domains:
        d = d.lower().strip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


# ----------------------------------------------------------------------------
# Text cleaning
# ----------------------------------------------------------------------------

NAV_LABEL_PATTERNS = [
    r"^\s*(home|menu|search|login|sign in|sign up|subscribe)\s*$",
    r"^\s*(share this|share on \w+|follow us)\s*$",
    r"^\s*(accept cookies|we use cookies|cookie policy)\s*$",
    r"^\s*(next|previous|prev|back|continue reading)\s*$",
]
NAV_LABEL_RE = re.compile("|".join(NAV_LABEL_PATTERNS), re.IGNORECASE)


def clean_text(text):
    """Collapse whitespace, drop residual nav-label lines and boilerplate."""
    cleaned_lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if NAV_LABEL_RE.match(ln):
            continue
        cleaned_lines.append(re.sub(r"[ \t]+", " ", ln))

    text = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_MD_FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_MD_RULE_RE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")


def markdown_to_speech_text(md):
    """
    Flatten Markdown into something worth reading aloud.

    Code blocks are dropped outright — narrating source code is noise. Link
    URLs, image syntax, heading hashes, list bullets and table pipes all go;
    the words inside them stay.
    """
    text = _MD_FENCE_RE.sub("", md or "")

    out = []
    for ln in text.splitlines():
        if _MD_RULE_RE.match(ln) or _MD_TABLE_SEP_RE.match(ln):
            continue
        ln = _MD_IMAGE_RE.sub("", ln)
        ln = _MD_LINK_RE.sub(r"\1", ln)
        ln = re.sub(r"^\s{0,3}#{1,6}\s*", "", ln)          # headings
        ln = re.sub(r"^\s*>+\s*", "", ln)                   # blockquotes
        ln = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", ln)      # list markers
        ln = ln.replace("|", " ")                           # table cells
        ln = re.sub(r"[`*_~]", "", ln)                      # inline emphasis
        ln = re.sub(r"<[^>]+>", "", ln)                     # stray HTML
        ln = _html.unescape(ln)
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln:
            out.append(ln)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def looks_like_blocked_page(html):
    """Heuristic check for captcha / login-wall / verification pages."""
    if not html:
        return False
    lowered = html.lower()
    return any([
        "captcha" in lowered,
        "are you a robot" in lowered,
        "verify you are human" in lowered,
        "please enable cookies and reload" in lowered,
        "checking your browser" in lowered and "cloudflare" in lowered,
    ])


# ----------------------------------------------------------------------------
# Fetching raw HTML  (Playwright preferred, requests fallback)
# ----------------------------------------------------------------------------

def download_page_html(url):
    """
    Returns (html, error). Uses Playwright when installed so JS-rendered pages
    work; otherwise a plain requests GET, which is fine for static pages.
    """
    playwright_error = None
    if sync_playwright is not None:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=REQUEST_TIMEOUT * 1000, wait_until="load")
                page.wait_for_timeout(PAGE_LOAD_WAIT_MS)
                html = page.content()
                browser.close()
                return html, None
        except Exception as e:
            playwright_error = str(e)

    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return resp.text, None
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in _DIRECT_REFUSAL_STATUS:
            _direct_refused_hosts.add((urlparse(url).netloc or "").casefold())
        detail = f"{e}"
        if playwright_error:
            detail += f" (Playwright also failed: {playwright_error})"
        return None, detail


# Hosts that refused a direct fetch during this job. Cloudflare scores whole
# datacenter networks as low-trust, so a 403 from a host is a property of the
# host and this server - not of the page. Re-asking it for every chapter costs
# two wasted round-trips per page and is rude besides.
_DIRECT_REFUSAL_STATUS = {401, 403, 406, 429, 451}
_direct_refused_hosts = set()


def reset_fetch_memory():
    """Forget which hosts refused. Called once per job, never mid-crawl."""
    _direct_refused_hosts.clear()


def fetch_with_retry(url):
    """Fetch a page, retrying once on failure/timeout."""
    host = (urlparse(url).netloc or "").casefold()
    if host in _direct_refused_hosts:
        return None, ("this host refused a direct fetch earlier in this job, "
                      "so the reader was used instead")

    html, err = download_page_html(url)
    if html is not None:
        return html, None
    if host in _direct_refused_hosts:
        return None, err          # recorded during that attempt; don't retry
    time.sleep(1.5)
    return download_page_html(url)


# Statuses that mean the page genuinely isn't there. Everything else 4xx/5xx
# means "reachable, refusing this request", which the reader may still handle.
_FATAL_STATUS = {404, 410}


def validate_url(url):
    """
    (ok, error, content_type) — structural + public-target check, then a
    reachability probe. The Content-Type comes back because routing needs it:
    a feed URL is often indistinguishable from an article URL by path alone,
    and this probe already has the answer.
    """
    try:
        url = normalize_public_url(url)
    except ValueError as e:
        return False, str(e), ""

    try:
        resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                             headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            # Plenty of servers mishandle HEAD; try GET before calling it dead.
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                                headers={"User-Agent": USER_AGENT}, stream=True)
            if resp.status_code in _FATAL_STATUS:
                return False, f"URL returned HTTP {resp.status_code}", ""
            if resp.status_code >= 400:
                # Reachable but refusing THIS request - typically a datacenter
                # IP block, which the Jina fallback in fetch_crawl often gets
                # past. Failing here would veto a page the job can still read.
                return True, None, ""
    except requests.RequestException as e:
        return False, f"Could not reach URL: {e}", ""

    return True, None, resp.headers.get("Content-Type", "")


# ----------------------------------------------------------------------------
# Extractors
# ----------------------------------------------------------------------------

def extract_with_bs4(html, base_url):
    """Fallback extractor: strips obvious chrome and keeps likely article text."""
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in ["nav", "header", "footer", "aside", "script", "style",
                     "form", "noscript", "iframe"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    junk_hints = re.compile(
        r"(nav|menu|sidebar|footer|header|advert|ads|banner|cookie|popup|modal|"
        r"comment|share|social|newsletter|subscribe|breadcrumb)",
        re.IGNORECASE,
    )
    for tag in soup.find_all(attrs={"class": junk_hints}):
        tag.decompose()
    for tag in soup.find_all(attrs={"id": junk_hints}):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup.body or soup

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else base_url

    paragraphs = []
    for el in container.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
        text = el.get_text(" ", strip=True)
        if not text or NAV_LABEL_RE.match(text):
            continue
        paragraphs.append(text)

    return title, "\n\n".join(paragraphs)


def _is_jina_antibot(body):
    """Recognize Jina/Cloudflare challenge responses (from Agent Reach, MIT)."""
    sample = body[:4096].casefold()
    jina_captcha = "warning:" in sample and "requiring captcha" in sample
    challenge = any(m in sample for m in (
        "title: just a moment...",
        "## performing security verification",
        "title: attention required! | cloudflare",
    ))
    cloudflare_block = "title: attention required! | cloudflare" in sample and (
        "ray id" in sample or "/cdn-cgi/challenge-platform/" in sample
    )
    return (jina_captcha and challenge) or cloudflare_block


_JINA_HEADER_RE = re.compile(
    r"\A(?:Title:.*\n|URL Source:.*\n|Published Time:.*\n|Warning:.*\n|"
    r"Image \d+:.*\n|\s*\n)*Markdown Content:\s*\n",
    re.MULTILINE,
)


_MD_ANY_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


def _retry_delay(attempt, retry_after):
    """
    Seconds to wait before retry `attempt`. Honours Retry-After in both forms
    RFC 9110 allows - a plain integer and an HTTP-date - because a server that
    tells you when to come back is the cheapest signal available.
    """
    if retry_after:
        raw = retry_after.strip()
        try:
            return max(0.0, min(float(raw), JINA_MAX_SLEEP))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                delay = when.timestamp() - time.time()
                return max(0.0, min(delay, JINA_MAX_SLEEP))
    # No usable header: exponential backoff with jitter. The jitter matters
    # because the chapter fetches run several at a time - without it they are
    # throttled together, sleep the same length, and stampede the limit again.
    backoff = JINA_BACKOFF_BASE * (2 ** (attempt - 1))
    return min(backoff + random.uniform(0, JINA_BACKOFF_BASE), JINA_MAX_SLEEP)


# A reader can return HTTP 200 carrying the target's bot-check page instead of
# the article. Narrating "Enable JavaScript and cookies to continue" as chapter
# one is worse than failing, so short results get checked for these.
_CHALLENGE_SIGNS = (
    "enable javascript and cookies",
    "checking your browser",
    "just a moment",
    "attention required",
    "verify you are human",
    "are you a robot",
    "performing security verification",
    "cf-browser-verification",
    "/cdn-cgi/challenge-platform/",
)


def looks_like_challenge_text(text):
    """
    True when short text looks like a bot-check page rather than prose.

    Gated on length on purpose: a real chapter may well contain the words
    "just a moment", and flagging a 3,000-word chapter as a challenge would
    throw away a page that was read perfectly well.
    """
    if not text or len(text.split()) >= MIN_ARTICLE_WORDS:
        return False
    lowered = text[:4096].casefold()
    return any(sign in lowered for sign in _CHALLENGE_SIGNS)


def jina_read(url, with_links=False):
    """
    Read any page through Jina Reader and return (title, markdown).

    This is the single most valuable thing carried over from Agent Reach: it
    renders JavaScript on Jina's side, so pages that come back empty here —
    every SPA-based reader site, and anything at all on a deploy without
    Chromium installed — still produce text.

    Raises SourceError on failure so callers can decide whether to give up.
    """
    url = normalize_public_url(url)
    headers = {"User-Agent": JINA_USER_AGENT, "Accept": "text/plain"}
    if with_links:
        # Appends a "Links/Buttons:" section listing every link on the page.
        # Index pages need it: their chapter lists are rendered as controls
        # that don't survive into the plain markdown body at all.
        headers["X-With-Links-Summary"] = "true"
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    body = None
    last_error = None
    for attempt in range(1, JINA_ATTEMPTS + 1):
        check_cancelled()
        try:
            resp = requests.get(JINA_ENDPOINT + url, headers=headers,
                                timeout=JINA_TIMEOUT, stream=True)
            if resp.status_code == 429:
                last_error = "rate limited (HTTP 429)"
                if attempt == JINA_ATTEMPTS:
                    break
                time.sleep(_retry_delay(attempt, resp.headers.get("Retry-After")))
                continue
            resp.raise_for_status()
            body = resp.raw.read(JINA_MAX_BYTES + 1, decode_content=True)
            break
        except requests.RequestException as e:
            last_error = str(e)
            if attempt == JINA_ATTEMPTS:
                raise SourceError(f"Jina Reader failed: {e}") from e
            time.sleep(_retry_delay(attempt, None))

    if body is None:
        # Out of attempts against a rate limit. Say that, rather than letting a
        # caller read it as "this page has nothing" - the difference decides
        # whether the user is told to retry or told the book ended.
        raise SourceThrottled(
            f"Jina Reader is rate limiting this server ({last_error}). "
            "Anonymous use is 20 requests/minute shared across everything on "
            "this host's IP; setting a free JINA_API_KEY raises it to 500/min "
            "for this app alone."
        )

    if len(body) > JINA_MAX_BYTES:
        raise SourceError("Jina Reader response exceeded 5 MB.")

    text = body.decode("utf-8", errors="replace")
    if _is_jina_antibot(text):
        raise SourceError(
            "Jina Reader hit an anti-bot verification page instead of the "
            "article. This tool does not attempt to work around those."
        )

    title = ""
    m = re.search(r"^Title:\s*(.+)$", text[:2048], re.MULTILINE)
    if m:
        title = m.group(1).strip()

    stripped = _JINA_HEADER_RE.sub("", text, count=1)
    prose = markdown_to_speech_text(stripped)
    if looks_like_challenge_text(prose):
        # The reader answered 200 but what it read was the target's bot check.
        # Narrating that as a chapter is the worst outcome available.
        raise SourceError(
            "The reader reached that page but got the site's bot-check screen "
            "instead of the text. This tool does not work around those."
        )

    # Raw markdown comes back too: it still has the page's links in it, which
    # is the only way to keep following a chapter chain when the direct fetch
    # is blocked and there is no HTML to run find_next_link over.
    return title, prose, stripped


_MD_NEXT_LINK_RE = re.compile(r"\[([^\]]{0,40})\]\(([^)\s]+)\)")


def find_next_link_markdown(md, current_url):
    """find_next_link's equivalent for Jina's markdown output."""
    for label, href in _MD_NEXT_LINK_RE.findall(md or ""):
        if NEXT_TEXT_RE.match(label.strip()):
            if href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            return urljoin(current_url, href)
    return None


def extract_article(html, url, allow_jina=True):
    """
    Ladder: trafilatura -> BeautifulSoup -> Jina Reader rescue.

    The local extractors run first because they are free and instant. Jina is
    only paid for when they come back thin, which is the JS-rendered case.
    Returns (title, text).
    """
    best_title, best_text = "", ""

    if trafilatura is not None:
        try:
            extracted = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=False,
                favor_precision=True,
            )
            if extracted and extracted.strip():
                meta = trafilatura.extract_metadata(html, default_url=url)
                best_title = meta.title if meta and meta.title else ""
                best_text = extracted.strip()
        except Exception:
            pass

    if len(best_text.split()) < MIN_ARTICLE_WORDS:
        bs_title, bs_text = extract_with_bs4(html, url)
        if len(bs_text.split()) > len(best_text.split()):
            best_title, best_text = (bs_title or best_title), bs_text

    if allow_jina and len(best_text.split()) < MIN_ARTICLE_WORDS:
        try:
            j_title, j_text, _ = jina_read(url)
            if len(j_text.split()) > len(best_text.split()):
                best_title, best_text = (j_title or best_title), j_text
        except SourceError:
            pass          # local result stands, however thin

    return (best_title or url), best_text


# ----------------------------------------------------------------------------
# Naming — real filenames instead of "book.mp3"
# ----------------------------------------------------------------------------

# Not anchored with ^ - a chapter marker in the middle of a title ("Muzan:
# Conquering multiverse. Chapter 1 - Chapter 1: Michael Jackson. - WebNovel")
# used to be invisible to the old ^-anchored version, so nothing ever got
# truncated and the whole string became the "book title".
CHAPTER_MARKER_RE = re.compile(
    r"\b(?:chapter|ch\.?|episode|ep\.?|part)\s*#?\s*(\d+)", re.IGNORECASE)
CHAPTER_NUM_URL_RE = re.compile(r"chapter[-_ ]?(\d+)", re.IGNORECASE)
# fanfiction.net-style tail: "..., a loud house fanfic".
_FANFIC_TAIL_RE = re.compile(r",\s*a\s+.+?\bfanfic\b\s*$", re.IGNORECASE)
_BARE_NUM_SEGMENT_RE = re.compile(r"^\d{1,5}$")


def _registrable_part(netloc):
    """"www.webnovel.com" -> "webnovel" - what a title's trailing site-name
    segment actually spells, stripped of the parts that never appear in it."""
    host = (netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] if host else ""


def _clean_raw_title(raw, netloc):
    """
    Clean one raw <title> string down to just the book name.

    Order matters: drop the site-brand segment first (it can itself look
    like a "chapter" candidate on some sites), then truncate everything from
    the first chapter marker onward - that's usually where the per-chapter
    subtitle and any remaining site name live, so it clears both at once.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    raw = raw.split("|", 1)[0].strip()
    segments = [s.strip() for s in raw.split(" - ") if s.strip()]
    if len(segments) > 1:
        site_key = _registrable_part(netloc)
        last_alpha = re.sub(r"[^a-z]", "", segments[-1].lower())
        if site_key and last_alpha == site_key:
            segments = segments[:-1]
    raw = " - ".join(segments) if segments else raw

    m = CHAPTER_MARKER_RE.search(raw)
    if m:
        raw = raw[:m.start()]

    raw = _FANFIC_TAIL_RE.sub("", raw)
    return raw.strip(" \t\n-:,.–—")


def extract_book_title_from_titles(raw_titles, fallback_url):
    """
    Book name from every chapter's raw <title>, cleaned by _clean_raw_title.

    A single cleaned title is already reliable (that's the whole point of
    truncating at the chapter marker), but the longest common prefix across
    every page collected catches the sites where one page's non-chapter part
    differs slightly from another's, and is site-agnostic - no per-site rule.
    """
    netloc = urlparse(fallback_url).netloc
    cleaned = [c for c in (_clean_raw_title(t, netloc) for t in (raw_titles or [])) if c]
    if not cleaned:
        return netloc or fallback_url or "book"
    if len(cleaned) == 1:
        return cleaned[0]

    prefix = os.path.commonprefix(cleaned)
    # Only trim back if the prefix actually cuts a word in half - i.e. some
    # cleaned title is longer than it and the very next character there
    # isn't whitespace. Identical (or prefix-of) cleaned titles already end
    # on a real word and must keep their last word.
    cuts_mid_word = any(len(s) > len(prefix) and not s[len(prefix)].isspace()
                        for s in cleaned)
    if cuts_mid_word and prefix and not prefix[-1].isspace():
        prefix = prefix.rsplit(" ", 1)[0] if " " in prefix else ""
    prefix = prefix.strip(" \t\n-:,.")
    return prefix if len(prefix) >= 3 else cleaned[0]


def extract_book_title(html, fallback_url):
    """Single-page convenience wrapper around extract_book_title_from_titles."""
    raw = _raw_title_tag(html)
    return extract_book_title_from_titles([raw] if raw else [], fallback_url)


def _raw_title_tag(html):
    """The page's raw <title> text, not the per-chapter title extract_article
    finds inside the article body - that's a different string and the one
    the book name has to come from."""
    soup = BeautifulSoup(html or "", "html.parser")
    tag = soup.find("title")
    return tag.get_text(strip=True) if tag else ""


def extract_chapter_number(chapter_title, url):
    """
    Best-effort chapter number: the page's own title, then the URL's
    chapter-N token, then a bare numeric path segment of at most 5 digits -
    fanfiction.net's /s/13857537/1/ has no other signal, and the 8-digit
    story id right before it must not be mistaken for the chapter number.
    """
    m = CHAPTER_MARKER_RE.search(chapter_title or "")
    if m:
        return int(m.group(1))
    m = CHAPTER_NUM_URL_RE.search(url or "")
    if m:
        return int(m.group(1))
    segments = [s for s in urlsplit(url or "").path.split("/") if s]
    if segments and _BARE_NUM_SEGMENT_RE.match(segments[-1]):
        return int(segments[-1])
    return None


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|]', "", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or "book"


# ----------------------------------------------------------------------------
# "Next page" discovery
# ----------------------------------------------------------------------------

NEXT_TEXT_RE = re.compile(
    r"^\s*(next( page| chapter)?|continue( reading)?|»|›|>>|read more)\s*$",
    re.IGNORECASE,
)


_NEXT_GUESS_CHAPTER_RE = re.compile(r"(chapter[-_]?)(\d{1,5})$", re.IGNORECASE)


def _too_thin_to_be_a_chapter(text):
    """
    Past the end of a book, a guessed URL still returns a page - it just
    isn't a chapter. fanfiction.net answers a chapter number it doesn't have
    with a 30-word "Chapter not found" notice, which is not empty and would
    otherwise be narrated as the final chapter. A real chapter clears the
    same floor the Jina rescue uses to decide a page came back thin.
    """
    return len((text or "").split()) < MIN_ARTICLE_WORDS


def guess_next_numeric_url(url):
    """
    Last-resort next-page guess: increment the URL's trailing chapter number.

    Only for fanfiction.net-style sites whose chapter nav is JS-only (a
    <select> plus onclick buttons, no <a href> anywhere), so neither
    find_next_link nor find_next_link_markdown can ever find anything. Only
    touches a short numeric segment (<=5 digits) or an explicit chapter-N
    token - never a long site-assigned id. webnovel's chapter URLs end in a
    17-18 digit id; incrementing that would walk into nonsense, so a bare
    numeric segment over 5 digits is left alone. Returns None when there's
    nothing safe to increment.
    """
    parsed = urlsplit(url or "")
    path = parsed.path
    trailing_slash = path.endswith("/")
    trimmed = path.rstrip("/")

    m = _NEXT_GUESS_CHAPTER_RE.search(trimmed)
    if m:
        new_path = trimmed[:m.start(2)] + str(int(m.group(2)) + 1)
    else:
        segs = trimmed.split("/")
        if not segs or not _BARE_NUM_SEGMENT_RE.match(segs[-1]):
            return None
        segs[-1] = str(int(segs[-1]) + 1)
        new_path = "/".join(segs)

    if trailing_slash:
        new_path += "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{new_path}{query}"


def find_next_link(html, current_url):
    """
    Looks for a "next page" link using, in order:
      1. <link rel="next" href="...">
      2. <a rel="next" href="...">
      3. An <a> whose visible text matches common "next" wording
    Returns an absolute URL or None.
    """
    soup = BeautifulSoup(html, "html.parser")

    link_rel = soup.find("link", rel=lambda v: v and "next" in v)
    if link_rel and link_rel.get("href"):
        return urljoin(current_url, link_rel["href"])

    a_rel = soup.find("a", rel=lambda v: v and "next" in v)
    if a_rel and a_rel.get("href"):
        return urljoin(current_url, a_rel["href"])

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if text and NEXT_TEXT_RE.match(text):
            href = a["href"]
            if href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            return urljoin(current_url, href)

    return None


# ----------------------------------------------------------------------------
# Handler: YouTube  (yt-dlp, no login)
# ----------------------------------------------------------------------------

_VTT_NOISE = ("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")


def parse_vtt(raw):
    """
    Turn a .vtt subtitle file into flowing prose.

    Auto-generated YouTube captions use rolling cues, where each cue repeats
    the previous one plus a few new words. Plain line collection produces text
    read three times over, so consecutive lines that contain one another are
    collapsed into the longer of the two.
    """
    lines = []
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln or "-->" in ln or ln.isdigit():
            continue
        if ln.startswith(_VTT_NOISE):
            continue
        ln = re.sub(r"<[^>]+>", "", ln)          # <c>, <00:00:01.000> karaoke
        ln = _html.unescape(ln).strip()
        if not ln:
            continue
        if lines:
            if ln == lines[-1] or ln in lines[-1]:
                continue
            if lines[-1] in ln:
                lines[-1] = ln                    # rolling cue grew
                continue
        lines.append(ln)

    # ponytail: adjacent-pair dedupe only. Captions that repeat across a gap of
    # 2+ cues still slip through; swap in a shingle/n-gram dedupe if that shows up.
    return " ".join(lines)


def _run_ytdlp(args, timeout=YTDLP_TIMEOUT):
    """Invoke yt-dlp as a module so it works without depending on PATH."""
    try:
        return subprocess.run([sys.executable, "-m", "yt_dlp", *args],
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise SourceError("yt-dlp is not installed. Run: pip install yt-dlp") from e
    except subprocess.TimeoutExpired as e:
        raise SourceError("yt-dlp timed out fetching subtitles.") from e


def blocked_by_bot_check(output):
    """True if yt-dlp's output is YouTube's 'prove you're not a bot' refusal."""
    lowered = (output or "").lower()
    return any(sign in lowered for sign in _BOT_CHECK_SIGNS)


_BOT_CHECK_MESSAGE = (
    "YouTube refused this request with its bot check. That almost always "
    "means this server is on a datacenter IP, which YouTube blocks by "
    "default. The same link usually works from a home connection. Getting "
    "past it needs account cookies, which this tool deliberately does not use."
)


def parse_progress_percent(line):
    """Pull the percentage out of a yt-dlp --newline progress line, else None."""
    m = _PROGRESS_RE.search(line or "")
    if not m:
        return None
    try:
        return max(0.0, min(float(m.group(1)), 100.0))
    except ValueError:
        return None


def download_video(url, out_folder, on_status, on_percent=None):
    """
    Download a single YouTube video as MP4 into out_folder/video.mp4.
    Returns the title, for naming the file the user gets.

    Single video only - --no-playlist stops a pasted playlist link from
    pulling down hundreds of files. Capped at MAX_VIDEO_MB.
    """
    url = normalize_public_url(url)
    if not host_matches(url, "youtube.com", "youtu.be", "music.youtube.com"):
        raise SourceError("Video download only supports YouTube links.")

    out_path = os.path.join(out_folder, "video.mp4")
    title_path = os.path.join(out_folder, ".video_title")
    for stale in (out_path, title_path):
        try:
            os.remove(stale)
        except OSError:
            pass

    on_status("Starting video download...")
    args = [
        "--no-playlist",
        "--extractor-args", f"youtube:player_client={YTDLP_VIDEO_CLIENT}",
        "-f", YTDLP_VIDEO_FORMAT,
        "--merge-output-format", "mp4",
        "--max-filesize", f"{MAX_VIDEO_MB}M",
        "--newline", "--no-warnings",
        # Keep the title out of the progress stream so parsing stays simple.
        "--print-to-file", "%(title)s", title_path,
        "-o", os.path.join(out_folder, "video.%(ext)s"),
        url,
    ]

    tail = []
    try:
        proc = subprocess.Popen([sys.executable, "-m", "yt_dlp", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError as e:
        raise SourceError("yt-dlp is not installed. Run: pip install yt-dlp") from e

    try:
        for line in proc.stdout:
            # A half-downloaded file is worthless, so Stop kills yt-dlp
            # outright rather than waiting for the transfer to finish.
            try:
                check_cancelled()
            except JobCancelled:
                proc.kill()
                proc.wait(timeout=10)
                raise
            line = line.rstrip()
            tail.append(line)
            del tail[:-25]
            pct = parse_progress_percent(line)
            if pct is not None:
                if on_percent:
                    on_percent(pct)
                on_status(f"Downloading video: {pct:.0f}%")
            elif line.startswith("[Merger]"):
                on_status("Merging video and audio...")
        proc.wait(timeout=YTDLP_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise SourceError("Video download timed out.")

    if not os.path.exists(out_path):
        joined = "\n".join(tail)
        if "File is larger than max-filesize" in joined:
            raise SourceError(
                f"That video is larger than the {MAX_VIDEO_MB} MB limit."
            )
        if blocked_by_bot_check(joined):
            raise SourceError(_BOT_CHECK_MESSAGE)
        # Last resort: show yt-dlp's own words, but only whole lines, so the
        # message doesn't start mid-word the way a raw tail slice does.
        detail = " ".join(ln for ln in tail[-3:] if ln.strip())
        raise SourceError(f"Video download failed. {detail[:300]}")

    title = ""
    try:
        with open(title_path, encoding="utf-8", errors="replace") as f:
            title = f.read().strip()
    except OSError:
        pass
    finally:
        try:
            os.remove(title_path)
        except OSError:
            pass

    return title or "video"


def fetch_youtube(url, max_pages, on_status, on_page, on_chapter=None):
    """
    Video or playlist -> transcript chapters, using the official/auto captions.

    No login, no video download: --skip-download pulls only the subtitle track.
    Playlists become one chapter per video, capped at max_pages.
    """
    url = normalize_public_url(url)
    on_status("Fetching YouTube captions via yt-dlp...")

    tmp = tempfile.mkdtemp(prefix="wba_yt_")
    try:
        result = _run_ytdlp([
            "--skip-download", "--no-simulate",
            "--write-sub", "--write-auto-sub",
            "--sub-langs", YTDLP_SUB_LANGS,
            "--sub-format", "vtt",
            # Only subtitles are wanted, so a video with no downloadable
            # format is not an error - without this yt-dlp aborts on it.
            "--ignore-no-formats-error",
            "--extractor-args", f"youtube:player_client={YTDLP_PLAYER_CLIENTS}",
            "--playlist-end", str(max_pages),
            "--ignore-errors", "--no-warnings",
            "--print", "%(id)s\t%(title)s",
            "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
            url,
        ])

        entries = []
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            vid, _, title = line.partition("\t")
            vid, title = vid.strip(), title.strip()
            if vid:
                entries.append((vid, title or vid))

        if not entries:
            combined = f"{result.stdout or ''}\n{result.stderr or ''}"
            if blocked_by_bot_check(combined):
                raise SourceError(_BOT_CHECK_MESSAGE)
            raise SourceError(
                "yt-dlp returned no videos for that URL. "
                f"{combined.strip()[-300:]}"
            )

        chapters = []
        for i, (vid, title) in enumerate(entries[:max_pages], start=1):
            matches = sorted(_glob.glob(os.path.join(tmp, f"{vid}*.vtt")))
            # Prefer a manually uploaded English track over the auto one.
            matches.sort(key=lambda p: ("auto" in os.path.basename(p).lower(),
                                        len(os.path.basename(p))))
            text = ""
            for path in matches:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text = clean_text(parse_vtt(f.read()))
                except OSError:
                    text = ""
                if text:
                    break

            video_url = f"https://www.youtube.com/watch?v={vid}"
            if text:
                _add_chapter(chapters, {
                    "page": i, "url": video_url, "title": title, "text": text,
                    "chapter_num": i if len(entries) > 1 else None,
                }, on_chapter)
                on_page({"page": i, "url": video_url, "title": title,
                         "words": len(text.split()), "ok": True,
                         "note": "YouTube captions"})
            else:
                on_page({"page": i, "url": video_url, "title": title,
                         "words": 0, "ok": False,
                         "note": "No captions available, skipped"})
            on_status(f"YouTube: {i}/{len(entries[:max_pages])} processed")

        if not chapters:
            raise SourceError(
                "None of those videos have captions. This tool reads existing "
                "captions only — it does not transcribe audio."
            )

        book_title = entries[0][1] if len(chapters) == 1 else (
            os.path.commonprefix([c["title"] for c in chapters]).strip(" -–:|")
            or entries[0][1]
        )
        return {"chapters": chapters, "book_title": book_title}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------
# Handler: RSS / Atom  (feedparser, no login)
# ----------------------------------------------------------------------------

def _entry_text(entry):
    """Best available body for a feed entry, HTML stripped."""
    raw = ""
    content = getattr(entry, "content", None)
    if content:
        raw = max((c.get("value", "") for c in content), key=len, default="")
    if not raw:
        raw = getattr(entry, "summary", "") or getattr(entry, "description", "")
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)


def fetch_rss(url, max_pages, on_status, on_page, on_chapter=None):
    """
    Each feed entry becomes a chapter. Feeds that only publish a summary get
    the linked article pulled in full through the normal extractor ladder.
    """
    if feedparser is None:
        raise SourceError("feedparser is not installed. Run: pip install feedparser")

    url = normalize_public_url(url)
    on_status("Parsing feed...")
    parsed = feedparser.parse(url)

    entries = list(parsed.entries or [])[:max_pages]
    if not entries:
        raise SourceError("That feed has no readable entries.")

    chapters = []
    for i, entry in enumerate(entries, start=1):
        check_cancelled()
        title = (getattr(entry, "title", "") or f"Entry {i}").strip()
        link = getattr(entry, "link", "") or url
        on_status(f"Feed entry {i}/{len(entries)}: {title[:60]}")

        text = clean_text(_entry_text(entry))

        # Summary-only feeds: go get the real article.
        if len(text.split()) < MIN_ARTICLE_WORDS and link and link != url:
            html, _ = fetch_with_retry(link)
            if html:
                _, full = extract_article(html, link)
                full = clean_text(full)
                if len(full.split()) > len(text.split()):
                    text = full

        if text:
            _add_chapter(chapters,
                         {"page": i, "url": link, "title": title,
                          "text": text, "chapter_num": i},
                         on_chapter)
            on_page({"page": i, "url": link, "title": title,
                     "words": len(text.split()), "ok": True, "note": "Feed entry"})
        else:
            on_page({"page": i, "url": link, "title": title, "words": 0,
                     "ok": False, "note": "Empty entry, skipped"})

    if not chapters:
        raise SourceError("No readable content in that feed.")

    book_title = (getattr(parsed.feed, "title", "") or "").strip() \
        or urlparse(url).netloc
    return {"chapters": chapters, "book_title": book_title}


# ----------------------------------------------------------------------------
# Handler: GitHub public docs  (REST API, no auth)
# ----------------------------------------------------------------------------

def _gh_get(path):
    """Unauthenticated GitHub API call. 60 req/hr per IP, plenty for one repo."""
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(GITHUB_API + path, headers=headers,
                            timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise SourceError(f"GitHub request failed: {e}") from e
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise SourceError(
            "GitHub's unauthenticated rate limit is exhausted (60/hr). "
            "Set GITHUB_TOKEN in the environment to raise it."
        )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise SourceError(f"GitHub returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise SourceError("GitHub returned a response that wasn't JSON.") from e


def _gh_decode(node):
    """Pull the text out of a GitHub contents-API node."""
    if not node or node.get("encoding") != "base64" or not node.get("content"):
        return ""
    try:
        return base64.b64decode(node["content"]).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def fetch_github(url, max_pages, on_status, on_page, on_chapter=None):
    """
    A public repo's prose: README first, then docs/*.md, then any other
    top-level .md. A direct /blob/ link to one file reads just that file.
    Requires no `gh auth login` — this is the public REST API.
    """
    url = normalize_public_url(url)
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        raise SourceError("That GitHub URL has no owner/repo in it.")
    owner, repo = parts[0], parts[1].removesuffix(".git")

    # Direct file link: github.com/owner/repo/blob/<ref>/<path>
    if len(parts) > 4 and parts[2] in ("blob", "tree"):
        ref, path = parts[3], "/".join(parts[4:])
        node = _gh_get(f"/repos/{owner}/{repo}/contents/{path}?ref={ref}")
        text = clean_text(markdown_to_speech_text(_gh_decode(node)))
        if not text:
            raise SourceError(f"Could not read text from {path}.")
        on_page({"page": 1, "url": url, "title": path,
                 "words": len(text.split()), "ok": True, "note": "GitHub file"})
        chapters = []
        _add_chapter(chapters,
                     {"page": 1, "url": url, "title": path,
                      "text": text, "chapter_num": None},
                     on_chapter)
        return {"chapters": chapters,
                "book_title": f"{repo} {os.path.basename(path)}"}

    on_status(f"Reading {owner}/{repo} docs from GitHub...")

    wanted = []                                    # (title, api_path)
    readme = _gh_get(f"/repos/{owner}/{repo}/readme")
    if readme:
        wanted.append((readme.get("name", "README"), None, _gh_decode(readme)))

    for folder in ("docs", ""):
        if len(wanted) >= max_pages:
            break
        listing = _gh_get(f"/repos/{owner}/{repo}/contents/{folder}") or []
        if not isinstance(listing, list):
            continue
        for node in sorted(listing, key=lambda n: n.get("name", "")):
            if len(wanted) >= max_pages:
                break
            name = node.get("name", "")
            # .md/.rst only - .txt sweeps in requirements.txt and friends.
            if node.get("type") != "file" or not name.lower().endswith((".md", ".rst")):
                continue
            if readme and name == readme.get("name"):
                continue
            wanted.append((f"{folder}/{name}" if folder else name,
                           node.get("path"), None))

    if not wanted:
        raise SourceError(f"No README or docs found in {owner}/{repo}.")

    chapters = []
    for i, (title, api_path, preloaded) in enumerate(wanted[:max_pages], start=1):
        check_cancelled()
        on_status(f"GitHub: {title} ({i}/{len(wanted[:max_pages])})")
        raw = preloaded if preloaded is not None else \
            _gh_decode(_gh_get(f"/repos/{owner}/{repo}/contents/{api_path}"))
        text = clean_text(markdown_to_speech_text(raw))
        page_url = f"https://github.com/{owner}/{repo}"
        if text:
            _add_chapter(chapters,
                         {"page": i, "url": page_url, "title": title,
                          "text": text, "chapter_num": None},
                         on_chapter)
            on_page({"page": i, "url": page_url, "title": title,
                     "words": len(text.split()), "ok": True, "note": "GitHub doc"})
        else:
            on_page({"page": i, "url": page_url, "title": title, "words": 0,
                     "ok": False, "note": "Empty after stripping code, skipped"})

    if not chapters:
        raise SourceError(f"{owner}/{repo} has docs but no prose to narrate.")

    return {"chapters": chapters, "book_title": f"{owner} {repo}"}


# ----------------------------------------------------------------------------
# Handler: chapter index / table of contents
# ----------------------------------------------------------------------------
# Chapter pages on serial-fiction sites often have no "next" link at all - the
# navigation is JavaScript, and it survives neither a JS-less fetch nor Jina.
# The table of contents does link every chapter, so read that instead and take
# the chapters from it.

_DIGIT_RUN_RE = re.compile(r"\d+")
# A real chapter list is long. Requiring a big group stops a chapter page's
# handful of sibling links from being mistaken for one - measured: a webnovel
# chapter page's dominant shape has 3 links, its book page has 280.
MIN_INDEX_LINKS = 8
# How much of a listing has to carry a chapter number before those numbers are
# trusted enough to sort by. Measured on webnovel's catalog: 52 of 59 links.
MIN_NUMBERED_RATIO = 0.6
_INDEX_LABEL_RE = re.compile(
    r"table of contents|catalog|contents|chapter list|all chapters|index",
    re.IGNORECASE,
)
MAX_INDEX_PAGES = 5
# Conventional index paths, tried under the work's own URL when the pages we
# can see carry no link to the contents. Not per-site rules: these are the
# names serial-fiction sites reuse. Only reached after everything else failed.
INDEX_PATH_GUESSES = ("catalog", "contents", "toc", "chapters")


def _url_shape(url):
    """Path with digit runs collapsed, so sibling chapter URLs share a shape."""
    parsed = urlsplit(url)
    return (parsed.netloc, _DIGIT_RUN_RE.sub("#", parsed.path))


def _coarse_url_shape(url):
    """
    Second-pass shape: also collapses slug-like path segments to "*".

    Sites that bake each chapter's own title into its URL never clear
    MIN_INDEX_LINKS under _url_shape alone - measured on webnovel, 59 real
    chapters land in 56 distinct fine shapes because the digit-run collapse
    doesn't touch the slug text. Only reached as a fallback when the fine
    pass comes up short, and only safe because the work-prefix guard already
    scoped every candidate to one work before this ever runs.
    """
    parsed = urlsplit(url)
    path = _DIGIT_RUN_RE.sub("#", parsed.path)
    segs = [
        "*" if seg not in ("", "#") and ("-" in seg or "_" in seg or len(seg) > 24)
        else seg
        for seg in path.split("/")
    ]
    return (parsed.netloc, "/".join(segs))


def _page_links(url):
    """
    [(label, absolute_url)] in document order, via direct fetch or the reader.
    Returns (links, page_title).
    """
    html, _ = fetch_with_retry(url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        links = [(a.get_text(" ", strip=True), a["href"])
                 for a in soup.find_all("a", href=True)]
    else:
        title, _text, md = jina_read(url, with_links=True)
        links = _MD_ANY_LINK_RE.findall(md)

    out = []
    for label, href in links:
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        try:
            absolute = normalize_public_url(urljoin(url, href))
        except ValueError:
            continue
        out.append((label.strip(), absolute))
    return out, title


_WORK_ID_RE = re.compile(r"\d{6,}")


def work_ids(url):
    """The long numeric ids in a URL, which identify the work it belongs to."""
    return set(_WORK_ID_RE.findall(url or ""))


def _work_prefix(url):
    """
    The pasted URL minus its last path segment.

    Site-agnostic and derived from the URL the user actually pasted, not
    from whatever index page discovery ends up on - verified against
    webnovel (/book/<bookslug>_<id>), freewebnovel (/novel/<slug>) and
    fanfiction.net (/s/13857537): every chapter of the work starts with it,
    no other novel does, and it works even when the URL has no numeric id at
    all (the slug-only sites that broke the old id-only guard).
    """
    parsed = urlsplit(url)
    trimmed = parsed.path.rstrip("/").rsplit("/", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{trimmed}"


def _belongs_to_work(url, ids, index_url, work_prefix=None):
    """
    Is this link part of the same book as the page the user pasted?

    Without this test the largest link group on a book page is the
    "recommended for you" carousel, and the crawler happily narrates the
    first chapter of fifteen unrelated novels. Every chapter of one book
    carries that book's id; every recommendation carries a different one.
    The work-prefix test is the primary guard now - it still catches
    cross-novel links on sites whose URLs carry no numeric id to compare.
    """
    if work_prefix and not url.startswith(work_prefix):
        return False
    if ids:
        return bool(ids & work_ids(url))
    if work_prefix:
        return True
    # No id and no prefix supplied, so fall back to "lives under the index page".
    index_path = urlsplit(index_url).path.rstrip("/")
    return urlsplit(url).path.startswith(index_path + "/") if index_path else False


def find_chapter_links(links, index_url, ids=None, work_prefix=None):
    """
    Pick the chapter list out of a page's links.

    A table of contents is mostly one repeated link shape - every chapter has
    the same URL pattern differing only in its numbers - surrounded by site
    chrome that doesn't. So group by shape and take the biggest group, in
    document order, after discarding anything belonging to a different work.
    No per-site rules. When the fine-grained shape splits real siblings into
    too many small groups (a chapter's own title slug baked into the URL),
    a coarser second pass regroups slug-like segments together.
    """
    host = urlsplit(index_url).netloc
    if ids is None:
        ids = work_ids(index_url)

    groups = {}
    for label, url in links:
        if urlsplit(url).netloc != host:
            continue                     # skip offsite chrome
        if url.rstrip("/") == index_url.rstrip("/"):
            continue
        if not _belongs_to_work(url, ids, index_url, work_prefix):
            continue
        groups.setdefault(_url_shape(url), []).append((label, url))

    if not groups:
        return []

    best = max(groups.values(), key=len)
    if len(best) < MIN_INDEX_LINKS:
        coarse_groups = {}
        for label, url in [item for grp in groups.values() for item in grp]:
            coarse_groups.setdefault(_coarse_url_shape(url), []).append((label, url))
        coarse_best = max(coarse_groups.values(), key=len) if coarse_groups else []
        if len(coarse_best) >= MIN_INDEX_LINKS:
            best = coarse_best
        else:
            return []

    seen, ordered = set(), []
    for label, url in best:
        if url not in seen:
            seen.add(url)
            ordered.append((label, url))
    return order_chapter_listing(ordered)


def order_chapter_listing(listing):
    """
    Put a chapter listing into reading order.

    An index page is not written in reading order. webnovel's catalog opens
    with a "Read" button pointing at chapter 1, follows it with a "latest
    updates" block holding the newest chapters, and only then lists the book
    from chapter 2 - so reading it in document order narrates 1, 56, three
    late chapters, 2, 3. Measured on the real catalog: the first fifteen
    chapter numbers in document order are 1, 56, -, -, -, 2, 3, 4...

    Sort by chapter number when most of the listing carries one. When it
    doesn't, document order is the only signal there is, so leave it alone.
    Entries with no number keep document order at the end: on the sites that
    mix the two, those are the newest chapters.
    """
    numbered, plain = [], []
    for label, url in listing:
        num = extract_chapter_number(label, url)
        if num is None:
            plain.append((label, url))
        else:
            numbered.append((num, label, url))

    if len(numbered) < 2 or len(numbered) < MIN_NUMBERED_RATIO * len(listing):
        return listing

    numbered.sort(key=lambda item: item[0])
    return [(label, url) for _num, label, url in numbered] + plain


def missing_chapter_numbers(chapters):
    """
    Chapter numbers between the first and last collected that never arrived.

    A chapter that fails to fetch or comes back empty is skipped and the crawl
    continues, which is right - but a book that jumps from 1 to 3 with nothing
    said about it reads like the app scrambled the order.
    """
    nums = sorted({c["chapter_num"] for c in chapters
                   if c.get("chapter_num") is not None})
    if len(nums) < 2:
        return []
    present = set(nums)
    return [n for n in range(nums[0], nums[-1] + 1) if n not in present]


def _parent_url(url):
    """One path segment up, or None at the root."""
    parsed = urlsplit(url)
    trimmed = parsed.path.rstrip("/").rsplit("/", 1)[0]
    if not trimmed or trimmed == parsed.path.rstrip("/"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}{trimmed}"


def _work_root(url, ids):
    """
    The shallowest URL still identifying this work - the book page, given a
    chapter page. Conventional index paths hang off this, not off the chapter.
    """
    candidate = url
    while True:
        parent = _parent_url(candidate)
        if not parent or (ids and not (ids & work_ids(parent))):
            return candidate
        candidate = parent


def discover_chapter_list(url, on_status):
    """
    Find the site's chapter list starting from whatever page the user pasted.

    Looks at the page's own links first; if that page is a chapter rather than
    an index, follows a link labelled like a table of contents and looks
    again. Entirely label- and shape-driven, so it isn't tied to any one site.
    Returns [(label, url)] in the order the site lists them.
    """
    # Taken from the URL the user pasted, so hopping to the book page can't
    # drift onto a different book's links.
    ids = work_ids(url)
    work_prefix = _work_prefix(url)

    seen = set()
    queue = [url]
    guessed = False

    while queue and len(seen) < MAX_INDEX_PAGES:
        current = queue.pop(0)
        if not current or current in seen:
            continue
        seen.add(current)

        try:
            links, _title = _page_links(current)
        except SourceThrottled:
            # Deliberately not caught: a throttled index page is not a page
            # without chapters, and treating it as one is what silently
            # produced a one-chapter book out of a fifty-chapter request.
            raise
        except SourceError:
            links = []

        found = find_chapter_links(links, current, ids, work_prefix)
        if len(found) >= MIN_INDEX_LINKS:
            return found

        # Not an index page. Queue anything labelled like the contents...
        for label, href in links:
            if _INDEX_LABEL_RE.search(label) or _INDEX_LABEL_RE.search(href):
                queue.append(href)

        # ...the page one segment up, since a chapter lives under its book and
        # the book page is what lists the chapters. Never climb above the
        # work root - "no id" used to mean "always climb", which is how a
        # slug-only chapter page (no 6+ digit id anywhere in the URL) walked
        # all the way to the site's front-page listing and admitted every
        # novel on it. work_prefix's own guess-path fallback below covers
        # that case instead.
        parent = _parent_url(current)
        if parent and ids and ids & work_ids(parent):
            queue.append(parent)

        # ...and, once everything else is exhausted, the conventional index
        # paths under the work's own URL (the pasted URL minus its last
        # segment - reliable even with no numeric id to anchor on). Some
        # sites render their contents link with JavaScript, so it never
        # reaches us to be followed.
        if not queue and not guessed:
            guessed = True
            queue.extend(f"{work_prefix.rstrip('/')}/{seg}" for seg in INDEX_PATH_GUESSES)
            on_status("Looking for the chapter list...")

    return []


def _read_one_chapter(chapter_url):
    """Fetch and extract a single chapter. Returns (title, text) or raises."""
    check_cancelled()
    html, _err = fetch_with_retry(chapter_url)
    if html:
        title, raw = extract_article(html, chapter_url)
    else:
        title, raw, _ = jina_read(chapter_url)
    return title, clean_text(raw)


def read_chapter_urls(chapter_urls, start_page, max_count, on_status, on_page,
                      on_chapter=None):
    """
    Read a known list of chapter URLs. Shared by the crawl's index fallback.

    The whole list is known up front, so the fetches run in parallel - the
    chain-following crawl can't do that, since it only learns page n+1 by
    reading page n. Results are consumed strictly in list order, so both the
    progress log and the finished book stay in reading order no matter which
    fetch finishes first. Kept deliberately modest: this is somebody's site
    being read, not a load test, and every page may go through Jina, whose
    anonymous limit is about 20 requests a minute.
    """
    chapters = []
    wanted = chapter_urls[:max_count]
    total = len(wanted)
    if not total:
        return chapters

    check_cancelled()
    pool = ThreadPoolExecutor(max_workers=min(CHAPTER_FETCH_CONCURRENCY, total))
    futures = [pool.submit(_read_one_chapter, url) for _label, url in wanted]

    try:
        for offset, (label, chapter_url) in enumerate(wanted):
            check_cancelled()
            page_num = start_page + offset
            on_status(f"Chapter {offset + 1}/{total}: {label[:50]}")

            try:
                title, text = futures[offset].result()
            except SourceError as e:
                on_page({"page": page_num, "url": chapter_url, "title": label,
                         "words": 0, "ok": False, "note": f"Failed: {e}"})
                continue

            if text:
                _add_chapter(chapters, {
                    "page": page_num, "url": chapter_url,
                    "title": title or label, "text": text,
                    "chapter_num": extract_chapter_number(label or title,
                                                          chapter_url),
                }, on_chapter)
                on_page({"page": page_num, "url": chapter_url,
                         "title": title or label, "words": len(text.split()),
                         "ok": True, "note": "Complete"})
            else:
                on_page({"page": page_num, "url": chapter_url, "title": label,
                         "words": 0, "ok": False,
                         "note": "Empty chapter, skipped"})
    finally:
        # cancel_futures so pressing Stop doesn't sit through the whole queue;
        # wait=False so it doesn't sit through the in-flight fetches either.
        pool.shutdown(wait=False, cancel_futures=True)

    return chapters


# ----------------------------------------------------------------------------
# Handler: generic web crawl  (the original v1 pipeline, now with Jina rescue)
# ----------------------------------------------------------------------------

# Backstop for when no Content-Type was captured. Chromium's XML viewer is
# matched too: with Playwright installed, a feed arrives wrapped in it rather
# than as raw <?xml, so the naive prefix test alone silently never fires.
_FEED_SNIFF_RE = re.compile(
    r"<(\?xml|rss\b|feed\b)|webkit-xml-viewer-source-xml|xml-viewer-style",
    re.IGNORECASE,
)


def fetch_crawl(url, max_pages, on_status, on_page, on_chapter=None):
    """
    Follow the "next page" chain from a starting URL, extracting article text
    from each. Unchanged from v1 except that extraction now falls through to
    Jina Reader when the local extractors come back thin.
    """
    current_url = normalize_public_url(url)
    start_url = current_url
    reset_fetch_memory()
    chapters = []
    visited = set()
    page_num = 0
    raw_titles = []             # every page's raw <title>, for the book name
    used_reader_fallback = False
    index_total = None          # set once the chapter-index fallback finds one
    next_is_guess = False       # the upcoming current_url came from a numeric guess
    prev_title = None           # to detect a guessed page that repeats the last one
    ran_past_last_chapter = False   # a guess landed past the end of the book

    while current_url and page_num < max_pages:
        check_cancelled()
        if current_url in visited:
            break
        visited.add(current_url)
        page_num += 1
        is_guess = next_is_guess
        next_is_guess = False

        on_status(f"Fetching page {page_num}...")
        html, err = fetch_with_retry(current_url)

        if html is None:
            # Direct fetch refused. Plenty of sites block datacenter IPs
            # outright, so a deployed instance gets 403 on pages any browser
            # loads fine. Jina reads from its own infrastructure, so try it
            # before giving up on the page.
            on_status(f"Page {page_num} refused the direct fetch, "
                      "trying the reader...")
            try:
                # with_links=True: without it the markdown carries no links at
                # all, so find_next_link_markdown could never match anything -
                # every reader-only crawl silently stopped after one page.
                j_title, j_text, j_md = jina_read(current_url, with_links=True)
            except SourceError as jina_err:
                on_page({"page": page_num, "url": current_url, "title": None,
                         "words": 0, "ok": False, "note": f"Failed: {err}"})
                raise SourceError(
                    f"Page {page_num} could not be read. Direct fetch: {err}. "
                    f"Reader: {jina_err}"
                ) from None

            used_reader_fallback = True
            text = clean_text(j_text)

            if is_guess and (_too_thin_to_be_a_chapter(text)
                             or (prev_title and j_title == prev_title)):
                # The numeric guess landed on a page with nothing new to say -
                # fanfiction.net's /s/13857537/3/ comes back as a 30-word
                # "Chapter not found" notice. That's the end of the book, not
                # a real page; stop here instead of burning the rest of the
                # page budget and narrating the notice as a chapter.
                on_page({"page": page_num, "url": current_url, "title": j_title,
                         "words": 0, "ok": False,
                         "note": "Guessed next-chapter URL led nowhere, stopping"})
                ran_past_last_chapter = True
                break

            if text:
                _add_chapter(chapters, {
                    "page": page_num, "url": current_url,
                    "title": j_title or current_url, "text": text,
                    "chapter_num": extract_chapter_number(j_title, current_url),
                }, on_chapter)
                on_page({"page": page_num, "url": current_url,
                         "title": j_title or current_url,
                         "words": len(text.split()), "ok": True,
                         "note": "Read via Jina (direct fetch blocked)"})
            else:
                on_page({"page": page_num, "url": current_url, "title": j_title,
                         "words": 0, "ok": False, "note": "Empty article, skipped"})

            if j_title:
                raw_titles.append(j_title)
                prev_title = j_title

            next_url = find_next_link_markdown(j_md, current_url)
            if not next_url:
                # Reader-only sites like fanfiction.net put chapter nav in a
                # <select> plus JS onclick buttons - there is no <a href> to
                # another chapter anywhere on the page, so this is the only
                # way to keep the chain going.
                guess = guess_next_numeric_url(current_url)
                if guess and guess not in visited:
                    next_url = guess
                    next_is_guess = True
            try:
                current_url = normalize_public_url(next_url) if next_url else None
            except ValueError:
                current_url = None
            continue

        # A feed handed in as a plain URL: hand off rather than scraping XML.
        if page_num == 1 and _FEED_SNIFF_RE.search(html[:2048]):
            on_status("That URL is a feed — switching to the RSS reader.")
            return fetch_rss(current_url, max_pages, on_status, on_page,
                             on_chapter)

        if looks_like_blocked_page(html):
            on_page({"page": page_num, "url": current_url, "title": None,
                     "words": 0, "ok": False,
                     "note": "Captcha / login / verification detected"})
            raise SourceError(
                "Manual intervention required (captcha, login, or verification "
                "page detected)."
            )

        raw_titles.append(_raw_title_tag(html))

        title, raw_text = extract_article(html, current_url)
        text = clean_text(raw_text)

        if is_guess and (_too_thin_to_be_a_chapter(text)
                         or (prev_title and title == prev_title)):
            # Same reasoning as the reader-path guard above: a guessed URL
            # that comes back thin or repeats the previous page's title
            # means the chain has run out, not that the page is a dud worth
            # skipping and continuing past.
            on_page({"page": page_num, "url": current_url, "title": title,
                     "words": 0, "ok": False,
                     "note": "Guessed next-chapter URL led nowhere, stopping"})
            ran_past_last_chapter = True
            break

        if not text:
            on_page({"page": page_num, "url": current_url, "title": title,
                     "words": 0, "ok": False, "note": "Empty article, skipped"})
        else:
            _add_chapter(
                chapters,
                {"page": page_num, "url": current_url, "title": title,
                 "text": text,
                 "chapter_num": extract_chapter_number(title, current_url)},
                on_chapter)
            on_page({"page": page_num, "url": current_url, "title": title,
                     "words": len(text.split()), "ok": True, "note": "Complete"})
        prev_title = title or prev_title

        on_status(f"Page {page_num} complete ({len(chapters)} kept so far)")

        next_url = find_next_link(html, current_url)
        if not next_url:
            guess = guess_next_numeric_url(current_url)
            if guess and guess not in visited:
                next_url = guess
                next_is_guess = True
        try:
            current_url = normalize_public_url(next_url) if next_url else None
        except ValueError:
            current_url = None          # a next-link pointing somewhere private

    # The "next page" chain ran out early. Plenty of serial-fiction sites have
    # no next link at all - the navigation is JavaScript, which survives
    # neither a JS-less fetch nor the reader. Their index page does list every
    # chapter, so go find it rather than returning 1 of the 50 asked for.
    # Gate on the page looking like part of a series. A standalone article has
    # no chapter number anywhere, and searching a site-wide index for one costs
    # two reader round-trips that can only ever come back empty.
    throttled_looking_for_index = False
    looks_serial = any(c.get("chapter_num") for c in chapters) or \
        extract_chapter_number("", start_url) is not None

    if (chapters and current_url is None and len(chapters) < max_pages
            and looks_serial):
        on_status("No next link. Looking for the site's chapter list...")
        try:
            listing = discover_chapter_list(start_url, on_status)
        except SourceThrottled:
            throttled_looking_for_index = True
            listing = []
        except (SourceError, ValueError):
            listing = []

        if len(listing) >= MIN_INDEX_LINKS:
            # Start where the user pointed us, if that page is in the list.
            already = {c["url"] for c in chapters}
            start_at = next((i for i, (_l, u) in enumerate(listing)
                             if u in already), None)
            remaining = listing[start_at + 1:] if start_at is not None else listing
            remaining = [(l, u) for l, u in remaining if u not in already]

            # Only "that's the whole book" if the index really did run out
            # first. Coming up short because chapters failed to read is a
            # different thing and must not be reported as a complete book.
            if len(remaining) <= max_pages - len(chapters):
                index_total = len(listing)

            if remaining:
                on_status(f"Found {len(listing)} chapters in the index.")
                chapters.extend(read_chapter_urls(
                    remaining, len(chapters) + 1, max_pages - len(chapters),
                    on_status, on_page, on_chapter))
                used_reader_fallback = False   # the index path carried it

    if not chapters:
        raise SourceError("No readable content was collected.")

    book_title = extract_book_title_from_titles(raw_titles, start_url)

    # Silently returning 1 chapter of the 50 that were asked for looks like a
    # bug. Say which limit was hit instead.
    warning = None
    if throttled_looking_for_index and len(chapters) < max_pages:
        # Only branch here that means "ask again shortly" rather than "this is
        # everything there is", so it has to be checked before the others.
        warning = (
            f"Stopped after {len(chapters)} of {max_pages} requested: the "
            "reader was rate limited while looking for this book's chapter "
            "list, so the rest could not be found. This is temporary - try "
            "again in a minute. Setting a free JINA_API_KEY on this server "
            "raises that limit 25x and stops it happening."
        )
    elif ran_past_last_chapter and len(chapters) < max_pages:
        # First, because the reader message below would otherwise blame the
        # reader for a chain that was in fact followed all the way to the
        # end of the book.
        warning = (
            f"Reached the end: {len(chapters)} chapters is everything this "
            f"book has, rather than the {max_pages} requested."
        )
    elif used_reader_fallback and len(chapters) < max_pages:
        warning = (
            f"Stopped after {len(chapters)} of {max_pages} requested. This "
            "server can't fetch that site directly, so pages came through the "
            "reader, and the reader doesn't expose next-chapter links. "
            "Running the app locally fetches the site directly and follows "
            "the whole chain."
        )
    elif index_total is not None and len(chapters) < max_pages:
        # The index list was found and read to the end - this book simply has
        # fewer chapters than were asked for. Say so instead of "no next page
        # link was found", which reads like a failure when it isn't one.
        warning = (
            f"Reached the end of the book: the index lists {index_total} "
            f"chapters, every one was tried, and {len(chapters)} came back "
            f"readable - so {len(chapters)} rather than the {max_pages} "
            "requested."
        )
    elif len(chapters) < max_pages and current_url is None:
        warning = (
            f"Stopped after {len(chapters)} of {max_pages} requested: no "
            "\"next page\" link was found. Sites that paginate with a "
            "JavaScript button instead of a real link can't be followed."
        )

    # A hole in the numbering means a chapter was skipped mid-book. Say which
    # ones rather than shipping an mp3 that jumps from 1 to 3.
    gaps = missing_chapter_numbers(chapters)
    if gaps:
        listed = ", ".join(str(n) for n in gaps[:10])
        more = " and others" if len(gaps) > 10 else ""
        gap_note = (f"Chapters {listed}{more} came back unreadable, so the "
                    "book skips from the chapter before them to the one after.")
        warning = f"{warning} {gap_note}" if warning else gap_note

    return {"chapters": chapters, "book_title": book_title, "warning": warning}


# ----------------------------------------------------------------------------
# Handler: pasted text (upload path)
# ----------------------------------------------------------------------------

def fetch_uploaded_text(raw_text, source_name, on_page, on_chapter=None):
    text = clean_text(raw_text)
    if not text:
        raise SourceError("The uploaded file has no readable text.")
    on_page({"page": 1, "url": source_name, "title": source_name,
             "words": len(text.split()), "ok": True, "note": "Uploaded file"})
    chapters = []
    _add_chapter(chapters,
                 {"page": 1, "url": source_name, "title": source_name,
                  "text": text, "chapter_num": None},
                 on_chapter)
    return {"chapters": chapters,
            "book_title": os.path.splitext(source_name)[0]}


# ----------------------------------------------------------------------------
# The router
# ----------------------------------------------------------------------------

def _is_feed_url(url):
    path = (urlparse(url).path or "").lower()
    query = (urlparse(url).query or "").lower()
    return (path.endswith((".xml", ".rss", ".atom"))
            or any(seg in path for seg in ("/feed", "/rss", "/atom"))
            or "feed" in query)


_FEED_CONTENT_TYPES = ("application/rss", "application/atom", "application/xml",
                       "text/xml", "application/rdf")


def route(url, content_type=""):
    """
    Return the source name for a URL. Order matters: the specific handlers
    claim what they recognise, the crawler takes everything else.

    Pass the Content-Type from validate_url when you have it — plenty of feeds
    live at paths with no /feed or .xml in them and are only identifiable here.
    """
    try:
        url = normalize_public_url(url)
    except ValueError:
        return "web"                  # validate_url reports the real error

    if host_matches(url, "youtube.com", "youtu.be", "music.youtube.com"):
        return "youtube"
    if host_matches(url, "github.com"):
        return "github"
    if _is_feed_url(url):
        return "rss"
    if any(t in (content_type or "").lower() for t in _FEED_CONTENT_TYPES):
        return "rss"
    return "web"


HANDLERS = {
    "web": fetch_crawl,
    "youtube": fetch_youtube,
    "github": fetch_github,
    "rss": fetch_rss,
}


def resolve_source(url):
    """
    (source_name, error) for a pasted URL. Always applies the public-target
    check; only probes the network when the answer would mean something.

    The reachability probe is for the generic crawler, which fetches the
    pasted URL directly. The other handlers don't: YouTube goes through
    yt-dlp's InnerTube API, GitHub through api.github.com, RSS through
    feedparser. Probing the page URL for those proves nothing about whether
    the handler will work, and it actively vetoes URLs that would have worked
    - youtube.com answers HEAD with 403 from datacenter IPs, so a cloud deploy
    rejected every YouTube link with a misleading "URL returned HTTP 403"
    before yt-dlp was ever given a chance.
    """
    try:
        url = normalize_public_url(url)
    except ValueError as e:
        return None, str(e)

    name = route(url)
    if name != "web":
        return name, None

    ok, err, content_type = validate_url(url)
    if not ok:
        return None, err
    # The probe's Content-Type can still reveal an unmarked feed.
    return route(url, content_type), None

SOURCE_LABELS = {
    "web": "web page / article chain",
    "youtube": "YouTube captions",
    "github": "GitHub docs",
    "rss": "RSS / Atom feed",
    "upload": "uploaded text file",
}
