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

Defaults live at the top of `app.py` and `sources.py`:

```python
MAX_PAGES_DEFAULT = 25         # app.py
MAX_PAGES_HARD_CAP = 100
WORDS_PER_CHUNK = 3000
MIN_ARTICLE_WORDS = 120        # sources.py - below this, Jina rescue fires
YTDLP_PLAYER_CLIENTS = "web_safari,mweb,web_embedded"
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

Offline — no network, no ffmpeg, no keys. Covers URL security, the router,
VTT caption dedupe, and Markdown-to-speech stripping.

## Known limits

- **One job at a time.** State is in memory and `output/` is a single shared
  folder; starting a job clears the previous one's files. Gunicorn runs
  `-w 1` for exactly this reason — a second worker would poll a job it can't
  see. Multi-user means real job storage first.
- **No resume.** A crawl that dies at page 40 of 50 loses everything.
- **No cancel.** Once started, a job runs to completion or error.
