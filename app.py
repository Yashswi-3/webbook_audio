"""
WebBook Audio Reader — v2
-------------------------
A small Flask app that:
  1. Takes a URL (or an uploaded .txt)
  2. Routes it to the right reader — see sources.py:
       YouTube  -> captions via yt-dlp
       GitHub   -> README + docs via the public API
       RSS/Atom -> one chapter per entry
       anything else -> the "next page" crawler, with a Jina Reader rescue
         for pages the local extractors can't read
  3. Merges everything into a single book.txt
  4. Converts it into a single book.mp3 using edge-tts (chunked, merged by ffmpeg)

Intended for content you are authorized to access and convert for your own
personal use. Nothing here bypasses a login, paywall, or CAPTCHA.

Run with:
    python app.py
Then open http://127.0.0.1:5000
"""

import asyncio
import glob
import os
import re
import subprocess
import sys
import threading
import traceback
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory, render_template

import sources
from sources import SourceError

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
MAX_PAGES_HARD_CAP = 100          # absolute ceiling regardless of the request
VOICE_DEFAULT = "en-IN-NeerjaNeural"
OUTPUT_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
# A chunk is one edge-tts request, and a request takes about as long as the
# text is long, so chunk size sets how much of the book can be spoken at once.
# Measured on 24,000 words: 3000-word chunks 5 at a time took 55s, 1500-word
# chunks 10 at a time took 22s. Past ~10 concurrent the gain disappears into
# noise, so the smaller chunk is what actually buys the time.
WORDS_PER_CHUNK = 1500
MAX_CONCURRENT_TTS = 10            # how many edge-tts chunk requests run at once
TTS_CHUNK_ATTEMPTS = 2             # one retry; a dropped chunk killed the job
MAX_UPLOAD_SIZE_MB = 25            # cap on uploaded .txt file size

# Video download (v2.5). On by default for local use. Set
# ALLOW_VIDEO_DOWNLOAD=0 on a public deployment: serving MP4s to anyone who
# finds the URL is bandwidth you pay for, and it's the kind of endpoint that
# gets a host account terminated.
ALLOW_VIDEO_DOWNLOAD = os.environ.get("ALLOW_VIDEO_DOWNLOAD", "1") != "0"

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
    "status": "idle",          # idle | collecting | writing | generating_audio
                               # | merging_audio | done | error
    "message": "",
    "source": None,            # web | youtube | github | rss | upload
    "pages_done": 0,
    "max_pages": MAX_PAGES_DEFAULT,
    "page_log": [],            # {"page", "url", "title", "words", "ok", "note"}
    "txt_ready": False,
    "mp3_ready": False,
    "video_ready": False,
    "error": None,
    "audio_chunks_done": 0,
    "audio_chunks_total": 0,
    "video_percent": 0,
    "cancel_requested": False,
    "warning": None,           # job finished, but not the way you asked
    "download_base": None,     # e.g. "That Time an American 300-332"
}

# Rough percent-of-total weighting per stage, used only to drive the UI
# progress bar - not an exact time estimate, just a "is it moving" signal.
STAGE_PERCENT_RANGE = {
    "idle": (0, 0),
    "collecting": (0, 40),
    "writing": (40, 45),
    "generating_audio": (45, 90),
    "merging_audio": (90, 98),
    "downloading_video": (0, 98),
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
    elif status == "downloading_video":
        frac = min(state.get("video_percent", 0), 100) / 100
    else:
        frac = 0
    return round(start + (end - start) * frac)


def reset_job_state(max_pages, source=None):
    with job_lock:
        job_state.update({
            "running": True,
            "status": "collecting",
            "message": "Starting...",
            "source": source,
            "pages_done": 0,
            "max_pages": max_pages,
            "page_log": [],
            "txt_ready": False,
            "mp3_ready": False,
            "video_ready": False,
            "error": None,
            "audio_chunks_done": 0,
            "audio_chunks_total": 0,
            "video_percent": 0,
            "cancel_requested": False,
            "warning": None,
            "download_base": None,
        })
    # One job at a time, so a module-level hook in sources is enough.
    sources.set_cancel_check(lambda: job_state["cancel_requested"])


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
# Merging chapters into book.txt
# ----------------------------------------------------------------------------

def build_book_text(chapters):
    """chapters: list of dicts {page, url, title, text}"""
    parts = []
    for ch in chapters:
        parts.append("=" * 40)
        parts.append(f"PAGE {ch['page']}: {ch.get('title') or ''}".rstrip(": "))
        parts.append(ch["url"])
        parts.append("=" * 40)
        parts.append("")
        parts.append(ch["text"])
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def build_download_base(chapters, book_title):
    """"Book Title 300-332" when the range is meaningful, else just the title."""
    if len(chapters) <= 1:
        return sources.sanitize_filename(book_title)
    nums = [c["chapter_num"] for c in chapters if c.get("chapter_num") is not None]
    if nums:
        span = f"{min(nums)}-{max(nums)}"
    else:
        span = f"{chapters[0]['page']}-{chapters[-1]['page']}"
    return sources.sanitize_filename(f"{book_title} {span}")


# ----------------------------------------------------------------------------
# Audio generation
# ----------------------------------------------------------------------------

_SENTENCE_END_RE = re.compile(r'[.!?]["\')\]]?$')


def split_into_word_chunks(text, words_per_chunk=WORDS_PER_CHUNK):
    """
    Split into chunks of roughly words_per_chunk, preferring a sentence end.

    Every chunk boundary is a join in the finished mp3, so landing one in the
    middle of a sentence is audible. Smaller chunks mean more boundaries, so
    the cut looks back over the last 15% of the chunk for a word that ends a
    sentence and cuts there instead. Falls back to the hard word count when
    there's no sentence end in reach.
    """
    words = text.split()
    lookback = max(1, words_per_chunk // 7)
    chunks = []
    i = 0
    while i < len(words):
        end = min(i + words_per_chunk, len(words))
        if end < len(words):
            for j in range(end, max(end - lookback, i + 1), -1):
                if _SENTENCE_END_RE.search(words[j - 1]):
                    end = j
                    break
        chunk = " ".join(words[i:end])
        if chunk.strip():
            chunks.append(chunk)
        i = end
    return chunks


async def _tts_chunk_to_file(text, out_path, voice, rate):
    # One retry: a single dropped connection used to take the whole job down
    # after every other chunk had already been generated. Observed for real -
    # a transient DNS failure to speech.platform.bing.com.
    for attempt in range(1, TTS_CHUNK_ATTEMPTS + 1):
        try:
            communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
            await communicate.save(out_path)
            return
        except Exception:
            if attempt == TTS_CHUNK_ATTEMPTS:
                raise
            await asyncio.sleep(2)


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
            # Checked inside the semaphore so queued chunks stop starting the
            # moment Stop is pressed, instead of after the whole batch.
            sources.check_cancelled()
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
    if AudioSegment is None:
        raise RuntimeError(
            "ffmpeg concat failed and pydub isn't usable, so there's no way "
            "to merge the audio. Install ffmpeg and put it on your PATH "
            "(recommended), or run: pip install pydub audioop-lts"
        )
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
        raise RuntimeError("edge-tts is not installed. Run: pip install edge-tts")
    # pydub is deliberately NOT required here: the ffmpeg concat below is the
    # real merge path, and pydub only backs it up. Demanding it up front made
    # the whole app unusable on Python 3.13, where pydub can't import at all
    # because the stdlib `audioop` module it needs was removed in PEP 594.

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

    final_path = os.path.join(OUTPUT_FOLDER, "book.mp3")
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
# Orchestration
# ----------------------------------------------------------------------------

def _write_and_narrate(chapters, book_title, voice, rate):
    """Shared tail of every job: book.txt, then book.mp3."""
    update_state(download_base=build_download_base(chapters, book_title))
    update_state(status="writing", message="Merging into book.txt...")

    txt_path = os.path.join(OUTPUT_FOLDER, "book.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(build_book_text(chapters))
    update_state(txt_ready=True)

    # Narration source drops the "PAGE n / URL / ===" markers so the reader
    # doesn't say the separators out loud - just flowing content.
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

    update_state(status="done", running=False, mp3_ready=True,
                 message="Done. book.txt and book.mp3 are ready.")


def run_pipeline(start_url, max_pages, voice, rate, source_name):
    """Run the reader picked in /start, then narrate whatever it returns."""
    try:
        handler = sources.HANDLERS[source_name]
        label = sources.SOURCE_LABELS.get(source_name, source_name)
        update_state(source=source_name, message=f"Reading {label}...")

        def on_status(msg):
            update_state(message=msg)

        result = handler(start_url, max_pages, on_status, append_page_log)
        if result.get("warning"):
            update_state(warning=result["warning"])
        _write_and_narrate(result["chapters"], result["book_title"], voice, rate)

    except sources.JobCancelled:
        update_state(status="stopped", running=False,
                     message="Stopped. Anything already finished is still "
                             "downloadable below.")
    except SourceError as e:
        update_state(status="error", running=False, message=str(e), error=str(e))
    except Exception as e:
        traceback.print_exc()
        update_state(status="error", running=False,
                     message=f"Unexpected error: {e}", error=str(e))
    finally:
        sources.set_cancel_check(None)


def run_video_job(url):
    """Download one YouTube video as MP4. No text, no narration."""
    try:
        update_state(status="downloading_video", source="video",
                     message="Starting video download...")

        def on_status(msg):
            update_state(message=msg)

        def on_percent(pct):
            update_state(video_percent=pct)

        title = sources.download_video(url, OUTPUT_FOLDER, on_status, on_percent)

        append_page_log({"page": 1, "url": url, "title": title, "words": 0,
                         "ok": True, "note": "MP4 downloaded"})
        update_state(status="done", running=False, video_ready=True,
                     video_percent=100,
                     download_base=sources.sanitize_filename(title),
                     message="Done. video.mp4 is ready.")

    except sources.JobCancelled:
        update_state(status="stopped", running=False,
                     message="Stopped. Anything already finished is still "
                             "downloadable below.")
    except SourceError as e:
        update_state(status="error", running=False, message=str(e), error=str(e))
    except Exception as e:
        traceback.print_exc()
        update_state(status="error", running=False,
                     message=f"Unexpected error: {e}", error=str(e))
    finally:
        sources.set_cancel_check(None)


def run_pipeline_from_text(raw_text, source_name, voice, rate):
    """Uploaded .txt — skips fetching entirely, same narration tail."""
    try:
        update_state(source="upload", message=f"Reading {source_name}...")
        result = sources.fetch_uploaded_text(raw_text, source_name,
                                             append_page_log)
        _write_and_narrate(result["chapters"], result["book_title"], voice, rate)
    except sources.JobCancelled:
        update_state(status="stopped", running=False,
                     message="Stopped. Anything already finished is still "
                             "downloadable below.")
    except SourceError as e:
        update_state(status="error", running=False, message=str(e), error=str(e))
    except Exception as e:
        traceback.print_exc()
        update_state(status="error", running=False,
                     message=f"Unexpected error: {e}", error=str(e))
    finally:
        sources.set_cancel_check(None)


# ----------------------------------------------------------------------------
# Request helpers
# ----------------------------------------------------------------------------

def parse_rate(speed):
    """0.5-2.0 multiplier -> the "+15%" / "-20%" string edge-tts expects."""
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(speed, 2.0))
    pct = int(round((speed - 1.0) * 100))
    return f"{'+' if pct >= 0 else ''}{pct}%"


def clear_output_folder():
    for f in glob.glob(os.path.join(OUTPUT_FOLDER, "*")):
        try:
            os.remove(f)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", voices=AVAILABLE_VOICES,
                           max_pages_default=MAX_PAGES_DEFAULT,
                           max_pages_hard_cap=MAX_PAGES_HARD_CAP,
                           max_upload_size_mb=MAX_UPLOAD_SIZE_MB,
                           allow_video=ALLOW_VIDEO_DOWNLOAD,
                           max_video_mb=sources.MAX_VIDEO_MB)


@app.route("/detect")
def detect():
    """What would we do with this URL? Drives the live hint under the URL box."""
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"source": None, "label": None})
    name = sources.route(url)
    return jsonify({"source": name, "label": sources.SOURCE_LABELS.get(name)})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True, silent=True) or {}
    start_url = (data.get("url") or "").strip()

    if job_state["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    try:
        max_pages = int(data.get("max_pages", MAX_PAGES_DEFAULT))
    except (TypeError, ValueError):
        max_pages = MAX_PAGES_DEFAULT
    max_pages = max(1, min(max_pages, MAX_PAGES_HARD_CAP))

    voice = data.get("voice") or VOICE_DEFAULT
    rate = parse_rate(data.get("speed", 1.0))

    source_name, err = sources.resolve_source(start_url)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    clear_output_folder()
    reset_job_state(max_pages, source=source_name)

    threading.Thread(target=run_pipeline,
                     args=(start_url, max_pages, voice, rate, source_name),
                     daemon=True).start()

    return jsonify({"ok": True, "source": source_name,
                    "label": sources.SOURCE_LABELS.get(source_name)})


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
    rate = parse_rate(request.form.get("speed", 1.0))

    clear_output_folder()
    reset_job_state(max_pages=1, source="upload")

    threading.Thread(target=run_pipeline_from_text,
                     args=(raw_text, filename, voice, rate),
                     daemon=True).start()

    return jsonify({"ok": True})


@app.route("/start_video", methods=["POST"])
def start_video():
    if not ALLOW_VIDEO_DOWNLOAD:
        return jsonify({"ok": False,
                        "error": "Video download is disabled on this server."}), 403

    if job_state["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 409

    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()

    source_name, err = sources.resolve_source(url)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    if source_name != "youtube":
        return jsonify({"ok": False,
                        "error": "Video download only supports YouTube links."}), 400

    clear_output_folder()
    reset_job_state(max_pages=1, source="video")

    threading.Thread(target=run_video_job, args=(url,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    """
    Ask the running job to stop. Cooperative: the flag is checked at loop
    boundaries (between pages, between audio chunks) and kills yt-dlp
    outright, so a stop lands within seconds rather than instantly.
    """
    if not job_state["running"]:
        return jsonify({"ok": False, "error": "No job is running."}), 409
    update_state(cancel_requested=True, message="Stopping...")
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
    elif kind == "video":
        if not ALLOW_VIDEO_DOWNLOAD:
            return jsonify({"ok": False,
                            "error": "Video download is disabled."}), 403
        filename = "video.mp4"
    else:
        return jsonify({"ok": False, "error": "Unknown file type"}), 404

    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "File not ready yet"}), 404

    base = job_state.get("download_base")
    ext = "mp4" if kind == "video" else kind
    download_name = f"{base}.{ext}" if base else filename
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True,
                               download_name=download_name)


@app.errorhandler(413)
def handle_file_too_large(e):
    return jsonify({
        "ok": False,
        "error": f"File is too large. Max upload size is {MAX_UPLOAD_SIZE_MB} MB.",
    }), 413


if __name__ == "__main__":
    missing = []
    if sources.trafilatura is None:
        missing.append("trafilatura")
    if edge_tts is None:
        missing.append("edge-tts")
    if AudioSegment is None:
        missing.append("pydub (optional - only the fallback audio merger; "
                       "on Python 3.13+ also needs: pip install audioop-lts)")
    if sources.feedparser is None:
        missing.append("feedparser (needed for RSS/Atom feeds)")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append("yt-dlp (needed for YouTube captions AND MP4 download)")
    if sources.sync_playwright is None:
        missing.append("playwright (optional - Jina Reader covers JS pages)")
    if missing:
        print("NOTE: some optional/required packages are not installed:")
        for m in missing:
            print(f"  - {m}")
        print(f"Install into THIS interpreter: {sys.executable} -m pip "
              "install -r requirements.txt")
        print("If using playwright, also run: playwright install chromium")
        print()

    app.run(debug=True, host="127.0.0.1", port=5000)
