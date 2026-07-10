"""
WebBook Audio Reader
---------------------
A small Flask app that:
  1. Takes a starting URL
  2. Crawls forward through a chain of "next page" links (up to a max page count)
  3. Extracts the readable article text from each page (trafilatura -> BeautifulSoup fallback)
  4. Merges everything into a single book.txt
  5. Converts book.txt into a single book.mp3 using edge-tts (chunked + merged with pydub)

Intended for content you are authorized to access and convert for your own personal use
(e.g. documentation, tutorials, manuals, or books you already have the right to read).

Run with:
    python app.py
Then open http://127.0.0.1:5000
"""

import asyncio
import glob
import os
import re
import subprocess
import threading
import time
import traceback
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, render_template

# Optional heavy deps - imported lazily / guarded so the app can still start
# even if one of them isn't installed yet (with a clear error message later).
try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

MAX_PAGES_DEFAULT = 25
MAX_PAGES_HARD_CAP = 100          # absolute ceiling regardless of what the user requests
VOICE_DEFAULT = "en-IN-NeerjaNeural"
RATE_DEFAULT = "+0%"
OUTPUT_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
WORDS_PER_CHUNK = 3000
MAX_CONCURRENT_TTS = 5              # how many edge-tts chunk requests run at once
REQUEST_TIMEOUT = 20               # seconds
MAX_UPLOAD_SIZE_MB = 25            # cap on uploaded .txt file size
PAGE_LOAD_WAIT_MS = 2500           # ms to let JS settle when using Playwright
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 WebBookAudioReader/1.0"
)

AVAILABLE_VOICES = [
    "en-IN-NeerjaNeural",
    "en-IN-PrabhatNeural",
    "en-US-JennyNeural",
    "en-US-GuyNeural",
]

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# ----------------------------------------------------------------------------
# Global job state
# ----------------------------------------------------------------------------
# Single-job-at-a-time model, matching the "simple, one page, no database" spec.
# All state lives in memory and is reset every time a new job starts.

job_lock = threading.Lock()
job_state = {
    "running": False,
    "status": "idle",          # idle | collecting | writing | generating_audio | merging_audio | done | error | stopped
    "message": "",
    "pages_done": 0,
    "max_pages": MAX_PAGES_DEFAULT,
    "page_log": [],            # list of {"page": n, "url": ..., "title": ..., "words": n, "ok": bool}
    "txt_ready": False,
    "mp3_ready": False,
    "error": None,
    "audio_chunks_done": 0,
    "audio_chunks_total": 0,
}

# Rough percent-of-total weighting per stage, used only to drive the UI
# progress bar - not an exact time estimate, just a "is it moving" signal.
STAGE_PERCENT_RANGE = {
    "idle": (0, 0),
    "collecting": (0, 40),
    "writing": (40, 45),
    "generating_audio": (45, 90),
    "merging_audio": (90, 98),
    "done": (100, 100),
    "error": (0, 0),
    "stopped": (0, 0),
}


def compute_percent(state):
    status = state.get("status", "idle")
    start, end = STAGE_PERCENT_RANGE.get(status, (0, 0))
    if status == "done":
        return 100
    if status == "collecting":
        total = max(state.get("max_pages") or 1, 1)
        done = min(state.get("pages_done", 0), total)
        frac = done / total
    elif status == "generating_audio":
        total = max(state.get("audio_chunks_total") or 1, 1)
        done = min(state.get("audio_chunks_done", 0), total)
        frac = done / total
    else:
        frac = 0
    return round(start + (end - start) * frac)


def reset_job_state(max_pages):
    with job_lock:
        job_state.update({
            "running": True,
            "status": "collecting",
            "message": "Starting...",
            "pages_done": 0,
            "max_pages": max_pages,
            "page_log": [],
            "txt_ready": False,
            "mp3_ready": False,
            "error": None,
            "audio_chunks_done": 0,
            "audio_chunks_total": 0,
        })


def update_state(**kwargs):
    with job_lock:
        job_state.update(kwargs)


def append_page_log(entry):
    with job_lock:
        job_state["page_log"].append(entry)
        job_state["pages_done"] = len(job_state["page_log"])


def get_state_snapshot():
    with job_lock:
        snapshot = dict(job_state)
    snapshot["percent"] = compute_percent(snapshot)
    return snapshot


# ----------------------------------------------------------------------------
# Step 1: URL validation
# ----------------------------------------------------------------------------

def validate_url(url):
    """Basic structural validation + a lightweight reachability check."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "URL must be a full http(s) URL, e.g. https://example.com/page"

    try:
        resp = requests.head(
            url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
            headers={"User-Agent": USER_AGENT}
        )
        if resp.status_code >= 400:
            # Some servers don't support HEAD properly; fall back to GET before failing.
            resp = requests.get(
                url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                headers={"User-Agent": USER_AGENT}, stream=True
            )
            if resp.status_code >= 400:
                return False, f"URL returned HTTP {resp.status_code}"
    except requests.RequestException as e:
        return False, f"Could not reach URL: {e}"

    return True, None


# ----------------------------------------------------------------------------
# Step 2: Page download (Playwright preferred, requests fallback)
# ----------------------------------------------------------------------------

def download_page_html(url):
    """
    Returns fully rendered HTML for a URL.
    Uses Playwright (handles JS-rendered pages) if available, otherwise
    falls back to a plain requests GET (works fine for static pages).
    """
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
            # Fall through to requests-based fetch below.
            playwright_error = str(e)
    else:
        playwright_error = None

    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
        return resp.text, None
    except requests.RequestException as e:
        detail = f"{e}"
        if playwright_error:
            detail += f" (Playwright also failed: {playwright_error})"
        return None, detail


def fetch_with_retry(url):
    """Fetch a page, retrying once on failure/timeout, per the spec's error handling."""
    html, err = download_page_html(url)
    if html is not None:
        return html, None
    # one retry
    time.sleep(1.5)
    html, err = download_page_html(url)
    return html, err


# ----------------------------------------------------------------------------
# Step 3: Text extraction
# ----------------------------------------------------------------------------

NAV_LABEL_PATTERNS = [
    r"^\s*(home|menu|search|login|sign in|sign up|subscribe)\s*$",
    r"^\s*(share this|share on \w+|follow us)\s*$",
    r"^\s*(accept cookies|we use cookies|cookie policy)\s*$",
    r"^\s*(next|previous|prev|back|continue reading)\s*$",
]
NAV_LABEL_RE = re.compile("|".join(NAV_LABEL_PATTERNS), re.IGNORECASE)


def extract_with_bs4(html, base_url):
    """Fallback extractor: strips obvious chrome and keeps likely article text."""
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in ["nav", "header", "footer", "aside", "script", "style",
                      "form", "noscript", "iframe"]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Drop elements that look like ads / popups / comments / sidebars by class or id.
    junk_hints = re.compile(
        r"(nav|menu|sidebar|footer|header|advert|ads|banner|cookie|popup|modal|"
        r"comment|share|social|newsletter|subscribe|breadcrumb)",
        re.IGNORECASE,
    )
    for tag in soup.find_all(attrs={"class": junk_hints}):
        tag.decompose()
    for tag in soup.find_all(attrs={"id": junk_hints}):
        tag.decompose()

    # Prefer <article>, then <main>, then the whole body.
    container = soup.find("article") or soup.find("main") or soup.body or soup

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else base_url

    paragraphs = []
    for el in container.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if NAV_LABEL_RE.match(text):
            continue
        paragraphs.append(text)

    body_text = "\n\n".join(paragraphs)
    return title, body_text


def extract_article(html, url):
    """
    Preferred order: trafilatura, then BeautifulSoup fallback.
    Returns (title, text).
    """
    if trafilatura is not None:
        try:
            extracted = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=False,
                favor_precision=True,
            )
            if extracted and len(extracted.strip()) > 0:
                meta = trafilatura.extract_metadata(html, default_url=url)
                title = (meta.title if meta and meta.title else url)
                return title, extracted.strip()
        except Exception:
            pass  # fall back below

    return extract_with_bs4(html, url)


def clean_text(text):
    """Collapse whitespace, drop residual nav-label lines and cookie/share boilerplate."""
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned_lines = []
    for ln in lines:
        if not ln:
            continue
        if NAV_LABEL_RE.match(ln):
            continue
        ln = re.sub(r"[ \t]+", " ", ln)
        cleaned_lines.append(ln)

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ----------------------------------------------------------------------------
# Step: find the "next page" link
# ----------------------------------------------------------------------------

NEXT_TEXT_RE = re.compile(
    r"^\s*(next( page)?|continue( reading)?|»|›|>>|read more)\s*$",
    re.IGNORECASE,
)


def find_next_link(html, current_url):
    """
    Looks for a "next page" link using, in order:
      1. <link rel="next" href="...">
      2. <a rel="next" href="...">
      3. An <a> whose visible text matches common "next" wording (Next, Continue, », ›, >>)
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
# Step 4: Merge chapters
# ----------------------------------------------------------------------------

def build_book_text(chapters):
    """chapters: list of dicts {page, url, title, text}"""
    parts = []
    for ch in chapters:
        parts.append("=" * 40)
        parts.append(f"PAGE {ch['page']}")
        parts.append(ch["url"])
        parts.append("=" * 40)
        parts.append("")
        parts.append(ch["text"])
        parts.append("")
    return "\n".join(parts).strip() + "\n"


# ----------------------------------------------------------------------------
# Step 5: Audio generation
# ----------------------------------------------------------------------------

def split_into_word_chunks(text, words_per_chunk=WORDS_PER_CHUNK):
    words = text.split()
    chunks = []
    for i in range(0, len(words), words_per_chunk):
        chunk = " ".join(words[i:i + words_per_chunk])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


async def _tts_chunk_to_file(text, out_path, voice, rate):
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
    await communicate.save(out_path)


async def _generate_chunks_concurrently(chunks, out_folder, voice, rate,
                                         progress_cb=None,
                                         max_concurrent=MAX_CONCURRENT_TTS):
    """
    Runs all chunk TTS requests concurrently (bounded by a semaphore so we
    don't blast edge-tts with unlimited parallel connections / risk being
    rate-limited). Returns part file paths in the original chunk order
    regardless of which one finishes first.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    part_paths = [None] * len(chunks)
    completed = 0
    completed_lock = asyncio.Lock()

    async def worker(idx, chunk_text):
        nonlocal completed
        part_path = os.path.join(out_folder, f"part{idx + 1}.mp3")
        async with semaphore:
            await _tts_chunk_to_file(chunk_text, part_path, voice, rate)
        part_paths[idx] = part_path
        if progress_cb:
            async with completed_lock:
                completed += 1
                progress_cb(completed, len(chunks))

    await asyncio.gather(*(worker(i, c) for i, c in enumerate(chunks)))
    return part_paths


def _merge_via_ffmpeg_concat(part_paths, final_path):
    """
    Fast path: ffmpeg's concat demuxer with stream copy (-c copy).
    Stitches the mp3 parts together WITHOUT decoding/re-encoding, so it
    finishes in a couple of seconds even for a 90-minute audiobook. This
    is the merge strategy used by default.
    """
    list_path = final_path + ".concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in part_paths:
            # Use forward slashes and escape single quotes - required by
            # ffmpeg's concat demuxer file format, including on Windows.
            normalized = os.path.abspath(p).replace("\\", "/")
            escaped = normalized.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", final_path],
            capture_output=True, text=True, timeout=300,
        )
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass

    if result.returncode != 0 or not os.path.exists(final_path):
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-500:]}")


def _merge_via_pydub(part_paths, final_path):
    """
    Slow fallback: decode every part to raw PCM, concatenate in memory,
    re-encode to mp3. Only used if the fast ffmpeg concat path fails for
    some reason (e.g. ffmpeg missing from PATH, mismatched stream params).
    """
    combined = AudioSegment.empty()
    for p in part_paths:
        combined += AudioSegment.from_file(p, format="mp3")
    combined.export(final_path, format="mp3")


def generate_audio(text, out_folder, voice, rate, progress_cb=None,
                    on_merge_start=None, max_concurrent=MAX_CONCURRENT_TTS):
    """
    Splits text into chunks, generates part{n}.mp3 via edge-tts (chunks run
    concurrently, bounded by max_concurrent), merges into book.mp3 via a
    fast ffmpeg stream-copy concat (falling back to pydub decode/re-encode
    if that fails), then removes the temporary part files. Returns path to
    book.mp3.
    """
    if edge_tts is None:
        raise RuntimeError(
            "edge-tts is not installed. Run: pip install edge-tts"
        )
    if AudioSegment is None:
        raise RuntimeError(
            "pydub is not installed (or ffmpeg is missing). "
            "Run: pip install pydub  and make sure ffmpeg is on your PATH."
        )

    chunks = split_into_word_chunks(text)
    if not chunks:
        raise RuntimeError("No text available to convert to audio.")

    part_paths = asyncio.run(
        _generate_chunks_concurrently(
            chunks, out_folder, voice, rate,
            progress_cb=progress_cb, max_concurrent=max_concurrent,
        )
    )

    if on_merge_start:
        on_merge_start(len(part_paths))

    final_path = os.path.join(out_folder, "book.mp3")
    try:
        _merge_via_ffmpeg_concat(part_paths, final_path)
    except Exception:
        traceback.print_exc()
        print("Fast ffmpeg concat merge failed, falling back to pydub "
              "decode/re-encode merge (slower)...")
        _merge_via_pydub(part_paths, final_path)

    for p in part_paths:
        try:
            os.remove(p)
        except OSError:
            pass

    return final_path


# ----------------------------------------------------------------------------
# Orchestration: the full crawl -> extract -> merge -> audio pipeline
# ----------------------------------------------------------------------------

def looks_like_blocked_page(html):
    """Heuristic check for captcha / login-wall / verification pages."""
    if not html:
        return False
    lowered = html.lower()
    signals = [
        "captcha", "are you a robot", "verify you are human",
        "please sign in", "please log in", "access denied",
        "cloudflare" in lowered and "checking your browser" in lowered,
    ]
    text_signals = [
        "captcha" in lowered,
        "are you a robot" in lowered,
        "verify you are human" in lowered,
        "please enable cookies and reload" in lowered,
    ]
    return any(text_signals)


def run_pipeline(start_url, max_pages, voice, rate):
    try:
        chapters = []
        current_url = start_url
        visited = set()
        page_num = 0

        while current_url and page_num < max_pages:
            if current_url in visited:
                break
            visited.add(current_url)
            page_num += 1

            update_state(status="collecting", message=f"Fetching page {page_num}...")

            html, err = fetch_with_retry(current_url)

            if html is None:
                append_page_log({
                    "page": page_num, "url": current_url, "title": None,
                    "words": 0, "ok": False, "note": f"Failed: {err}",
                })
                update_state(status="error",
                              message=f"Page {page_num} failed after retry: {err}",
                              running=False, error=err)
                return

            if looks_like_blocked_page(html):
                append_page_log({
                    "page": page_num, "url": current_url, "title": None,
                    "words": 0, "ok": False,
                    "note": "Captcha / login / verification detected",
                })
                update_state(
                    status="error", running=False,
                    message="Manual intervention required (captcha, login, or "
                             "verification page detected).",
                    error="blocked_page",
                )
                return

            title, raw_text = extract_article(html, current_url)
            text = clean_text(raw_text)

            if not text:
                append_page_log({
                    "page": page_num, "url": current_url, "title": title,
                    "words": 0, "ok": False, "note": "Empty article, skipped",
                })
            else:
                chapters.append({
                    "page": page_num, "url": current_url,
                    "title": title, "text": text,
                })
                append_page_log({
                    "page": page_num, "url": current_url, "title": title,
                    "words": len(text.split()), "ok": True, "note": "Complete",
                })

            update_state(message=f"Page {page_num} complete "
                                  f"({len(chapters)} kept so far)")

            next_url = find_next_link(html, current_url)
            current_url = next_url

        if not chapters:
            update_state(status="error", running=False,
                         message="No readable content was collected.",
                         error="no_content")
            return

        update_state(status="writing", message="Merging pages into book.txt...")
        book_text = build_book_text(chapters)
        txt_path = os.path.join(OUTPUT_FOLDER, "book.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(book_text)
        update_state(txt_ready=True)

        # Text used for audio: strip the "PAGE n / URL / ===" markers so
        # narration doesn't read out separators, just keep flowing content.
        audio_source = "\n\n".join(ch["text"] for ch in chapters)

        update_state(status="generating_audio",
                      message="Generating audio (this can take a while)...")

        def audio_progress(done, total):
            update_state(audio_chunks_done=done, audio_chunks_total=total,
                          message=f"Generating audio: chunk {done}/{total}")

        def merge_start(n):
            update_state(status="merging_audio",
                          message=f"Merging {n} audio chunks into book.mp3...")

        generate_audio(audio_source, OUTPUT_FOLDER, voice, rate,
                        progress_cb=audio_progress, on_merge_start=merge_start)

        update_state(status="done", running=False,
                      message="Done. book.txt and book.mp3 are ready.",
                      mp3_ready=True)

    except Exception as e:
        traceback.print_exc()
        update_state(status="error", running=False,
                      message=f"Unexpected error: {e}", error=str(e))


def run_pipeline_from_text(raw_text, source_name, voice, rate):
    """
    Same writing/audio stages as run_pipeline, but skips the crawl entirely -
    used when the user uploads a .txt file directly instead of a URL.
    """
    try:
        update_state(status="writing", message=f"Reading {source_name}...")

        text = clean_text(raw_text)
        if not text:
            update_state(status="error", running=False,
                          message="The uploaded file has no readable text.",
                          error="empty_file")
            return

        append_page_log({
            "page": 1, "url": source_name, "title": source_name,
            "words": len(text.split()), "ok": True, "note": "Uploaded file",
        })

        txt_path = os.path.join(OUTPUT_FOLDER, "book.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        update_state(txt_ready=True, message="book.txt written from upload.")

        update_state(status="generating_audio",
                      message="Generating audio (this can take a while)...")

        def audio_progress(done, total):
            update_state(audio_chunks_done=done, audio_chunks_total=total,
                          message=f"Generating audio: chunk {done}/{total}")

        def merge_start(n):
            update_state(status="merging_audio",
                          message=f"Merging {n} audio chunks into book.mp3...")

        generate_audio(text, OUTPUT_FOLDER, voice, rate,
                        progress_cb=audio_progress, on_merge_start=merge_start)

        update_state(status="done", running=False,
                      message="Done. book.txt and book.mp3 are ready.",
                      mp3_ready=True)

    except Exception as e:
        traceback.print_exc()
        update_state(status="error", running=False,
                      message=f"Unexpected error: {e}", error=str(e))


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", voices=AVAILABLE_VOICES,
                            max_pages_default=MAX_PAGES_DEFAULT,
                            max_pages_hard_cap=MAX_PAGES_HARD_CAP,
                            max_upload_size_mb=MAX_UPLOAD_SIZE_MB)


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True, silent=True) or {}
    start_url = (data.get("url") or "").strip()
    max_pages = data.get("max_pages", MAX_PAGES_DEFAULT)
    voice = data.get("voice") or VOICE_DEFAULT
    speed = data.get("speed", 1.0)

    if job_state["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        max_pages = MAX_PAGES_DEFAULT
    max_pages = max(1, min(max_pages, MAX_PAGES_HARD_CAP))

    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(speed, 2.0))
    pct = int(round((speed - 1.0) * 100))
    rate = f"{'+' if pct >= 0 else ''}{pct}%"

    ok, err = validate_url(start_url)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    # clear old output files for a fresh run
    for f in glob.glob(os.path.join(OUTPUT_FOLDER, "*")):
        try:
            os.remove(f)
        except OSError:
            pass

    reset_job_state(max_pages)

    thread = threading.Thread(
        target=run_pipeline, args=(start_url, max_pages, voice, rate), daemon=True
    )
    thread.start()

    return jsonify({"ok": True})


@app.route("/start_from_file", methods=["POST"])
def start_from_file():
    if job_state["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    uploaded = request.files.get("file")
    if uploaded is None or uploaded.filename == "":
        return jsonify({"ok": False, "error": "No file was uploaded."}), 400

    filename = uploaded.filename
    if not filename.lower().endswith(".txt"):
        return jsonify({"ok": False, "error": "Only .txt files are supported."}), 400

    try:
        raw_bytes = uploaded.read()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not read the file: {e}"}), 400

    if not raw_bytes:
        return jsonify({"ok": False, "error": "The uploaded file is empty."}), 400

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            raw_text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            raw_text = raw_bytes.decode("latin-1", errors="ignore")

    voice = request.form.get("voice") or VOICE_DEFAULT
    speed = request.form.get("speed", 1.0)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(speed, 2.0))
    pct = int(round((speed - 1.0) * 100))
    rate = f"{'+' if pct >= 0 else ''}{pct}%"

    # clear old output files for a fresh run
    for f in glob.glob(os.path.join(OUTPUT_FOLDER, "*")):
        try:
            os.remove(f)
        except OSError:
            pass

    reset_job_state(max_pages=1)

    thread = threading.Thread(
        target=run_pipeline_from_text, args=(raw_text, filename, voice, rate),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True})


@app.route("/progress")
def progress():
    return jsonify(get_state_snapshot())


@app.route("/download/<kind>")
def download(kind):
    if kind == "txt":
        filename = "book.txt"
    elif kind == "mp3":
        filename = "book.mp3"
    else:
        return jsonify({"ok": False, "error": "Unknown file type"}), 404

    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "File not ready yet"}), 404

    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)


@app.errorhandler(413)
def handle_file_too_large(e):
    return jsonify({
        "ok": False,
        "error": f"File is too large. Max upload size is {MAX_UPLOAD_SIZE_MB} MB.",
    }), 413


if __name__ == "__main__":
    missing = []
    if trafilatura is None:
        missing.append("trafilatura")
    if edge_tts is None:
        missing.append("edge-tts")
    if AudioSegment is None:
        missing.append("pydub")
    if sync_playwright is None:
        missing.append("playwright (optional, improves JS-heavy page support)")
    if missing:
        print("NOTE: some optional/required packages are not installed:")
        for m in missing:
            print(f"  - {m}")
        print("Install with: pip install -r requirements.txt")
        print("If using playwright, also run: playwright install chromium")
        print()

    app.run(debug=True, host="127.0.0.1", port=5000)