# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python app.py                          # dev server on http://127.0.0.1:5000
python test_sources.py                 # full offline suite (no network/ffmpeg/keys)
python -m pytest test_sources.py -q    # same tests under pytest
python -m pytest test_sources.py -q -k url_security   # one test
```

Tests are plain `test_*` functions in `test_sources.py`; the `__main__` block
runs them all in sorted order. Adding a test means adding a function — no
registration, no fixtures.

Setup needs `pip install -r requirements.txt` plus **ffmpeg on PATH** (pydub /
the fast concat merge). `playwright install chromium` is optional; without it,
JS-rendered pages fall through to the Jina Reader rescue.

Production entry: `gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 600 app:app`.
The `-w 1` is load-bearing — see *One job at a time*.

## Architecture

Two files carry everything. Keep it that way.

**`sources.py` — all input handling.** A pasted URL goes through
`normalize_public_url()` (SSRF guard: rejects localhost, private ranges,
`169.254.169.254`, non-http schemes, embedded credentials), then `route()`
picks one of four handlers in `HANDLERS`: `fetch_crawl` (web), `fetch_youtube`
(captions), `fetch_github`, `fetch_rss`. Uploaded `.txt` bypasses routing via
`fetch_uploaded_text`. Every handler has the same shape —
`(url, max_pages, on_status, on_page)` — and yields chapters, so a new source
type is one function plus a `HANDLERS` entry.

`resolve_source()` deliberately only runs the reachability probe for the `web`
route. YouTube/GitHub/RSS never fetch the pasted URL themselves (yt-dlp uses
InnerTube, GitHub uses the API, RSS uses feedparser), and probing it vetoed
working URLs — youtube.com answers HEAD with 403 from datacenter IPs.

**Text extraction is a three-rung ladder** in `extract_article()`, cheapest
first, stopping at the first rung that returns real prose: trafilatura →
BeautifulSoup boilerplate stripping → Jina Reader (`r.jina.ai`), fired only
when the first two come back under `MIN_ARTICLE_WORDS` (120). Rung 3 exists
because the Docker image skips Chromium on purpose.

**Chapter discovery has two paths.** Normally `find_next_link()` follows
`rel="next"` or common link text. When there's no next link,
`discover_chapter_list()` finds the site's index page and `find_chapter_links()`
filters it — and that filter is where the bugs live. It scopes links by work
ID (`_WORK_ID_RE`, 6+ digit ids) and URL shape so an index doesn't drag in
fifteen unrelated novels; a change here needs a test.

**`app.py` — Flask, job orchestration, TTS.** `run_pipeline()` collects
chapters → `build_book_text()` → `split_into_word_chunks()` (3000 words) →
edge-tts, max 5 concurrent → merged by ffmpeg concat, with `_merge_via_pydub`
as fallback. Routes: `/detect`, `/start`, `/start_from_file`, `/start_video`,
`/stop`, `/progress`, `/download/<kind>`. Single Jinja template,
`templates/index.html`.

### One job at a time

All job state is the in-memory `job_state` dict behind `job_lock`, and
`output/` is one shared folder that a new job clears. This is why gunicorn runs
one worker — a second worker would poll a job it cannot see. Anything
multi-user needs real job storage first, not a worker bump.

The browser holds no job state: the page reconnects on load by asking
`/progress`. Cancellation is cooperative — `check_cancelled()` is polled
between pages and between audio chunks, and kills yt-dlp outright, so a stop
lands in a few seconds. Partial output stays downloadable.

`STAGE_PERCENT_RANGE` is a UI "is it moving" signal, not a time estimate.

## Constraints that look like bugs

- **YouTube is local-only.** Both the caption and MP4 paths fail on any cloud
  host with "Sign in to confirm you're not a bot" — YouTube blocks datacenter
  IPs. This is not fixable in code without cookies, which this project does not
  use. Set `ALLOW_VIDEO_DOWNLOAD=0` on any deployed instance (403s both video
  routes and drops the UI).
- **MP4 is 360p.** Format 18 is the only real format served anonymously.
- **Two different yt-dlp player clients**, on purpose:
  `YTDLP_PLAYER_CLIENTS = "web_safari,mweb,web_embedded"` reaches subtitles but
  reports "only images are available" for formats; `YTDLP_VIDEO_CLIENT =
  "android"` returns a real stream. Changing one does not fix the other.
  `YTDLP_PLAYER_CLIENTS` is the knob when captions start failing — YouTube's
  bot check moves.
- **No login bypass, ever.** No cookies, no CAPTCHA solving, no paywall
  circumvention. A page demanding one stops the job with "Manual intervention
  required." Do not add one.
- **No resume.** A crawl dying at page 40 of 50 loses everything.

## Error contract

Timeout → one retry, then stop. Empty article/entry → skip, continue. 404/410 →
stop. Captcha or login wall → stop with a message. Video without captions →
skip (this reads captions, it does not transcribe). Private/internal URL →
rejected before any request.

Errors should name the real cause — several commits exist purely because a
message blamed the wrong thing (a probe's 403 instead of the bot check, a
missing `yt-dlp` reported as an interpreter mismatch).

## Config

Env vars, all optional: `JINA_API_KEY` (lifts Jina's ~20 req/min anonymous
limit), `GITHUB_TOKEN` (lifts 60 req/hr), `ALLOW_VIDEO_DOWNLOAD=0`. Tunables
live at the top of `app.py` (`MAX_PAGES_DEFAULT` 25, `MAX_PAGES_HARD_CAP` 100,
`WORDS_PER_CHUNK` 3000, `MAX_UPLOAD_SIZE_MB` 25) and `sources.py`
(`MIN_ARTICLE_WORDS`, the yt-dlp clients, `MAX_VIDEO_MB` 500).

`playwright` is left out of `requirements.txt` deliberately — the Dockerfile
skips Chromium to save RAM, and Jina covers the same ground.

## Commit style

Lowercase `type: what changed`, phrased as the user-visible symptom, not the
code — "fix: index discovery narrated fifteen unrelated novels".
