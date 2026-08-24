# WebBook Audio Reader

Paste a link. Get one `book.txt` and one narrated `book.mp3`.

v2 stopped being a single-purpose crawler. It now recognises what you pasted
and reads it the right way:

| Paste this | It does this | Needs |
|---|---|---|
| An article / chapter URL | Follows the "next page" chain, extracts the text of each | nothing |
| A YouTube video or playlist | Pulls the existing captions (no video download) | `yt-dlp` |
| An RSS / Atom feed | One chapter per entry; summary-only feeds get the full article fetched | `feedparser` |
| A public GitHub repo | README, then `docs/*.md`, code blocks stripped | nothing |
| A `.txt` file (upload) | Straight to audio | nothing |

Plus one thing that isn't audio: a YouTube link can be downloaded as **MP4**
instead of narrated — see *Video download* below.

Built for content you have the right to access and convert for personal use.
Nothing here bypasses a login, paywall, or CAPTCHA — see *Boundaries* below.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

# Optional - renders JS pages locally. Without it, the Jina Reader
# rescue path below covers the same ground.
playwright install chromium
```

**ffmpeg** must be on your PATH — it does the fast audio merge:

- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: download from ffmpeg.org and add the `bin` folder to PATH

## Run

```bash
python app.py
```

Open http://127.0.0.1:5000

## How audio generation works

Chapters are still saved in reading order, but narration no longer waits for
the whole book to finish downloading. Accepted chapter text flows into a
sentence-aware 1500-word buffer. Whenever that buffer fills, its numbered MP3
part starts immediately, with at most 10 edge-tts requests running at once.
After the last chapter, the remaining text is narrated and ffmpeg joins all
numbered parts once, in order, into `book.mp3`.

This overlaps page fetching with narration. Long or slow-to-fetch books save
the most time; short books may see little difference because the final merge
was already fast. A failed or stopped job removes temporary audio parts, so a
partial MP3 is never presented as complete.

## How the web reader gets text

Three rungs, cheapest first. Each page stops at the first one that returns
real content:

1. **trafilatura** — fast, local, free, handles most static article pages.
2. **BeautifulSoup fallback** — strips nav/ads/comments by tag and by
   class/id, then keeps `<article>` / `<main>` prose.
3. **Jina Reader** (`r.jina.ai`) — only when the first two come back under
   ~120 words. Jina renders JavaScript on its own servers, so single-page
   reader sites and any deploy without Chromium installed still produce text.

Rung 3 is the single most useful thing carried over from the
[Agent Reach](https://github.com/Panniantong/Agent-Reach) skill (MIT). It
matters most in Docker: the image deliberately skips Chromium to save RAM, so
before v2 a JS-rendered page just came back empty there.

Jina is free and needs no account. It rate-limits anonymous callers to roughly
20 requests/minute — set `JINA_API_KEY` to lift that.

## Video download

Paste a YouTube URL and the **Download MP4** button lights up. One video, no
playlists, capped at 500 MB.

**It is 360p, and that is not a bug.** Format 18 — a single combined
640x360 h264/aac stream — is the only real format YouTube serves to an
anonymous caller. Every 720p+ adaptive stream sits behind the bot check, which
wants cookies. This project doesn't do cookies, so 360p is the ceiling.

Note this uses a *different* yt-dlp player client from the caption path
(`android` vs `web_safari,mweb,web_embedded`). The caption clients reach
subtitle tracks but report "only images are available" for formats; the
android client is the one that returns a real stream. Both are in `sources.py`
as separate constants — changing one doesn't fix the other.

### YouTube does not work on cloud hosting

Both YouTube paths — MP4 download *and* captions — fail on Render, Fly,
Railway, or any other cloud host, with "Sign in to confirm you're not a bot".

The cause isn't the code. YouTube blocks datacenter IP ranges by default,
because that's where scraping comes from. The identical request from a home
connection is served normally. Nothing in this project can change that:
getting past it requires account cookies, which this project doesn't use.

So treat YouTube as a **local-only** feature. The web, RSS, GitHub and Jina
readers are unaffected — they don't touch YouTube — so a deployed instance is
still fully useful for everything else.

### Before you deploy this publicly

Two things worth knowing:

- YouTube's Terms of Service prohibit downloading without a download button.
  Your own uploads, Creative Commons and public-domain videos are fine; most
  other content isn't.
- A public MP4 endpoint is bandwidth you pay for and an obvious target for
  abuse. It's the kind of thing that gets a Render account terminated.

So set `ALLOW_VIDEO_DOWNLOAD=0` on the deployed instance. That returns 403
from both `/start_video` and `/download/video`, and drops the button and the
download link from the page entirely. It defaults to on for local use.

## What came from Agent Reach, and what didn't

Agent Reach routes 15 platforms. Only the ones that need **no login** and
produce **narratable prose** were worth porting:

**Ported** — Jina Reader, RSS/feedparser, YouTube captions via `yt-dlp`,
GitHub public docs, and its `normalize_public_http_url` SSRF guard.

**Not ported** — Reddit, Twitter/X, Instagram, Facebook, LinkedIn and
XiaoHongShu all need browser cookies this app has nowhere to store. Exa search
needs an API key. V2EX threads, Xueqiu stock quotes and Bilibili are
zero-login but aren't things anyone wants read aloud.

## Configuration

Environment variables, all optional:

| Variable | Effect |
|---|---|
| `JINA_API_KEY` | Lifts Jina Reader's anonymous rate limit |
| `GITHUB_TOKEN` | Lifts GitHub's 60 req/hr unauthenticated API limit |
| `ALLOW_VIDEO_DOWNLOAD` | `0` disables MP4 download entirely. Default on. |

Defaults live at the top of `app.py` and `sources.py`:

```python
MAX_PAGES_DEFAULT = 25         # app.py
MAX_PAGES_HARD_CAP = 100
WORDS_PER_CHUNK = 1500
MAX_CONCURRENT_TTS = 10
MIN_ARTICLE_WORDS = 120        # sources.py - below this, Jina rescue fires
YTDLP_PLAYER_CLIENTS = "web_safari,mweb,web_embedded"   # captions
YTDLP_VIDEO_CLIENT = "android"                          # mp4 download
MAX_VIDEO_MB = 500
```

`YTDLP_PLAYER_CLIENTS` is the knob to turn if YouTube captions start failing.
YouTube's bot check moves; as of 2026-08-15 the `default` and `android_vr`
clients hit "Sign in to confirm you're not a bot" while those three don't.

## Behavior on errors

- **Timeout** → retried once, then the job stops with an error.
- **Empty article / entry** → skipped, the crawl continues.
- **404** → job stops.
- **Captcha / login wall / verification page** → job stops with "Manual
  intervention required."
- **Video with no captions** → skipped. This tool reads existing captions; it
  does not transcribe audio.
- **Private or internal URL** → rejected before any request is made.

## Boundaries

- **No login bypass.** No cookies, no CAPTCHA solving, no paywall
  circumvention, no pretending to be a human verifying a browser. When a page
  demands one of those, the job stops and says so.
- **"Next page" detection is heuristic** — `rel="next"` or common link text.
  Sites that paginate purely by JS button click with no real href won't be
  followed. That's an inherent limit of a lightweight crawler.
- **Only public targets.** URLs resolving to localhost, private ranges, or
  cloud metadata endpoints (`169.254.169.254`) are refused. Without that check
  a deployed instance would happily fetch its own host's internals and hand
  them back in `book.txt`.

## Tests

```bash
python test_sources.py
```

Offline — no network, no ffmpeg, no keys. Covers URL security, routing,
chapter order, streaming audio overlap and cleanup, VTT caption dedupe, and
Markdown-to-speech stripping.

## Known limits

- **One job at a time.** State is in memory and `output/` is a single shared
  folder; starting a job clears the previous one's files. Gunicorn runs
  `-w 1` for exactly this reason — a second worker would poll a job it can't
  see. Multi-user means real job storage first.
- **No resume.** A crawl that dies at page 40 of 50 loses everything.

## Reload and Stop

The job lives on the server, not in the page. Refreshing, backgrounding the
tab, or clearing the cache doesn't lose it — the page asks `/progress` on
load and reconnects to whatever is running. A job that finished while you were
away comes back with its download buttons already enabled.

**Stop Job** appears while a job runs. Cancellation is cooperative: the flag
is checked between pages and between audio chunks, and it kills yt-dlp
outright, so a stop lands within a few seconds rather than instantly. Whatever
already finished stays downloadable — stopping during narration still leaves
you `book.txt`.
