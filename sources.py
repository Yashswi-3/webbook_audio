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
    handler(url, max_pages, on_status, on_page) -> {"chapters": [...],
                                                    "book_title": str}
  on_status(str)   - one-line progress message for the UI
  on_page(dict)    - append to the per-page log
  raises SourceError(msg) on anything the user needs to know about
"""

import base64
import glob as _glob
import html as _html
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import shutil
import time
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
        detail = f"{e}"
        if playwright_error:
            detail += f" (Playwright also failed: {playwright_error})"
        return None, detail


def fetch_with_retry(url):
    """Fetch a page, retrying once on failure/timeout."""
    html, err = download_page_html(url)
    if html is not None:
        return html, None
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


def jina_read(url):
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
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    try:
        resp = requests.get(JINA_ENDPOINT + url, headers=headers,
                            timeout=JINA_TIMEOUT, stream=True)
        resp.raise_for_status()
        body = resp.raw.read(JINA_MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as e:
        raise SourceError(f"Jina Reader failed: {e}") from e

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
    # Raw markdown comes back too: it still has the page's links in it, which
    # is the only way to keep following a chapter chain when the direct fetch
    # is blocked and there is no HTML to run find_next_link over.
    return title, markdown_to_speech_text(stripped), stripped


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

CHAPTER_WORD_RE = re.compile(r"^(chapter|ch\.?|episode|ep\.?|part)\s*#?\s*\d+",
                             re.IGNORECASE)
CHAPTER_NUM_RE = re.compile(r"chapter\s*#?\s*(\d+)", re.IGNORECASE)
CHAPTER_NUM_URL_RE = re.compile(r"chapter[-_](\d+)", re.IGNORECASE)


def extract_book_title(html, fallback_url):
    """
    Page <title> tags are usually "Chapter Title - Book Title | Site Name"
    (or the reverse). Drop the site name, then prefer whichever segment
    doesn't look like a chapter label.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    title_tag = soup.find("title")
    raw = title_tag.get_text(strip=True) if title_tag else ""
    raw = raw.split("|")[0].strip()

    segments = [s.strip() for s in raw.split(" - ") if s.strip()]
    candidates = [s for s in segments if not CHAPTER_WORD_RE.match(s)]
    if candidates:
        title = max(candidates, key=len)
    elif segments:
        title = segments[-1]
    else:
        title = urlparse(fallback_url).netloc

    return title or urlparse(fallback_url).netloc


def extract_chapter_number(chapter_title, url):
    """Best-effort chapter number from the page's own title, else the URL slug."""
    m = CHAPTER_NUM_RE.search(chapter_title or "")
    if not m:
        m = CHAPTER_NUM_URL_RE.search(url or "")
    return int(m.group(1)) if m else None


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


def fetch_youtube(url, max_pages, on_status, on_page):
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
                chapters.append({
                    "page": i, "url": video_url, "title": title, "text": text,
                    "chapter_num": i if len(entries) > 1 else None,
                })
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


def fetch_rss(url, max_pages, on_status, on_page):
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
            chapters.append({"page": i, "url": link, "title": title,
                             "text": text, "chapter_num": i})
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


def fetch_github(url, max_pages, on_status, on_page):
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
        return {"chapters": [{"page": 1, "url": url, "title": path,
                              "text": text, "chapter_num": None}],
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
            chapters.append({"page": i, "url": page_url, "title": title,
                             "text": text, "chapter_num": None})
            on_page({"page": i, "url": page_url, "title": title,
                     "words": len(text.split()), "ok": True, "note": "GitHub doc"})
        else:
            on_page({"page": i, "url": page_url, "title": title, "words": 0,
                     "ok": False, "note": "Empty after stripping code, skipped"})

    if not chapters:
        raise SourceError(f"{owner}/{repo} has docs but no prose to narrate.")

    return {"chapters": chapters, "book_title": f"{owner} {repo}"}


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


def fetch_crawl(url, max_pages, on_status, on_page):
    """
    Follow the "next page" chain from a starting URL, extracting article text
    from each. Unchanged from v1 except that extraction now falls through to
    Jina Reader when the local extractors come back thin.
    """
    current_url = normalize_public_url(url)
    start_url = current_url
    chapters = []
    visited = set()
    page_num = 0
    first_page_html = None
    book_title_override = None
    used_reader_fallback = False

    while current_url and page_num < max_pages:
        check_cancelled()
        if current_url in visited:
            break
        visited.add(current_url)
        page_num += 1

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
                j_title, j_text, j_md = jina_read(current_url)
            except SourceError as jina_err:
                on_page({"page": page_num, "url": current_url, "title": None,
                         "words": 0, "ok": False, "note": f"Failed: {err}"})
                raise SourceError(
                    f"Page {page_num} could not be read. Direct fetch: {err}. "
                    f"Reader: {jina_err}"
                ) from None

            used_reader_fallback = True
            text = clean_text(j_text)
            if text:
                chapters.append({
                    "page": page_num, "url": current_url,
                    "title": j_title or current_url, "text": text,
                    "chapter_num": extract_chapter_number(j_title, current_url),
                })
                on_page({"page": page_num, "url": current_url,
                         "title": j_title or current_url,
                         "words": len(text.split()), "ok": True,
                         "note": "Read via Jina (direct fetch blocked)"})
            else:
                on_page({"page": page_num, "url": current_url, "title": j_title,
                         "words": 0, "ok": False, "note": "Empty article, skipped"})

            if first_page_html is None and j_title:
                book_title_override = j_title

            next_url = find_next_link_markdown(j_md, current_url)
            try:
                current_url = normalize_public_url(next_url) if next_url else None
            except ValueError:
                current_url = None
            continue

        # A feed handed in as a plain URL: hand off rather than scraping XML.
        if page_num == 1 and _FEED_SNIFF_RE.search(html[:2048]):
            on_status("That URL is a feed — switching to the RSS reader.")
            return fetch_rss(current_url, max_pages, on_status, on_page)

        if looks_like_blocked_page(html):
            on_page({"page": page_num, "url": current_url, "title": None,
                     "words": 0, "ok": False,
                     "note": "Captcha / login / verification detected"})
            raise SourceError(
                "Manual intervention required (captcha, login, or verification "
                "page detected)."
            )

        if first_page_html is None:
            first_page_html = html

        title, raw_text = extract_article(html, current_url)
        text = clean_text(raw_text)

        if not text:
            on_page({"page": page_num, "url": current_url, "title": title,
                     "words": 0, "ok": False, "note": "Empty article, skipped"})
        else:
            chapters.append({"page": page_num, "url": current_url, "title": title,
                             "text": text,
                             "chapter_num": extract_chapter_number(title, current_url)})
            on_page({"page": page_num, "url": current_url, "title": title,
                     "words": len(text.split()), "ok": True, "note": "Complete"})

        on_status(f"Page {page_num} complete ({len(chapters)} kept so far)")

        next_url = find_next_link(html, current_url)
        try:
            current_url = normalize_public_url(next_url) if next_url else None
        except ValueError:
            current_url = None          # a next-link pointing somewhere private

    if not chapters:
        raise SourceError("No readable content was collected.")

    if first_page_html is not None:
        book_title = extract_book_title(first_page_html, start_url)
    else:
        book_title = book_title_override or urlparse(start_url).netloc

    # Silently returning 1 chapter of the 50 that were asked for looks like a
    # bug. Say which limit was hit instead.
    warning = None
    if used_reader_fallback and len(chapters) < max_pages:
        warning = (
            f"Stopped after {len(chapters)} of {max_pages} requested. This "
            "server can't fetch that site directly, so pages came through the "
            "reader, and the reader doesn't expose next-chapter links. "
            "Running the app locally fetches the site directly and follows "
            "the whole chain."
        )
    elif len(chapters) < max_pages and current_url is None:
        warning = (
            f"Stopped after {len(chapters)} of {max_pages} requested: no "
            "\"next page\" link was found. Sites that paginate with a "
            "JavaScript button instead of a real link can't be followed."
        )

    return {"chapters": chapters, "book_title": book_title, "warning": warning}


# ----------------------------------------------------------------------------
# Handler: pasted text (upload path)
# ----------------------------------------------------------------------------

def fetch_uploaded_text(raw_text, source_name, on_page):
    text = clean_text(raw_text)
    if not text:
        raise SourceError("The uploaded file has no readable text.")
    on_page({"page": 1, "url": source_name, "title": source_name,
             "words": len(text.split()), "ok": True, "note": "Uploaded file"})
    return {"chapters": [{"page": 1, "url": source_name, "title": source_name,
                          "text": text, "chapter_num": None}],
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
