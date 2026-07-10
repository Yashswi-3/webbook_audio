# WebBook Audio Reader

Turns a chain of web pages (docs, tutorials, manuals, or an online book you're
authorized to read) into one merged `book.txt` and one narrated `book.mp3`.

Built for content you have the right to access and convert for personal use.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

# Optional but recommended - lets the crawler handle JS-rendered pages.
# If skipped, the app still works via plain requests for static pages.
playwright install chromium
```

You'll also need **ffmpeg** installed and on your PATH (required by `pydub`
to merge the mp3 chunks):

- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: download from ffmpeg.org and add the `bin` folder to PATH

## Run

```bash
python app.py
```

Open http://127.0.0.1:5000

## How it works

1. Enter the starting URL, pick max pages (10/20/25/50), a voice, and a speed.
2. Click **Start**. The app fetches the page, extracts the readable article
   text (trafilatura, falling back to a BeautifulSoup-based cleaner), and
   looks for a "next page" link (`rel="next"`, or link text like "Next",
   "Continue", "»", "›").
3. It repeats this until it hits the max page count or can't find a next link.
4. All collected pages are merged into `output/book.txt`, each prefixed with
   a `PAGE n` / URL header.
5. The merged text (without the page headers) is split into ~3000-word
   chunks, narrated with `edge-tts`, and stitched into `output/book.mp3`
   with `pydub`. Temporary per-chunk files are deleted afterward.
6. Download buttons unlock once each file is ready.

## Behavior on errors

- **Timeout** → retried once, then the job stops with an error.
- **Empty article** → that page is skipped, crawl continues.
- **404** → job stops.
- **Captcha / login wall / verification page detected** → job stops and
  shows "Manual intervention required."

## Configuration

Defaults live at the top of `app.py`:

```python
MAX_PAGES_DEFAULT = 25
MAX_PAGES_HARD_CAP = 100
VOICE_DEFAULT = "en-IN-NeerjaNeural"
RATE_DEFAULT = "+0%"
OUTPUT_FOLDER = "output"
WORDS_PER_CHUNK = 3000
```

## Notes / limitations

- "Next page" detection is heuristic (rel=next, or common link text). Sites
  that paginate purely via JS button clicks with no real link/href won't be
  followed — that's an inherent limit of a lightweight crawler, not a bug to
  route around with anything that mimics human/browser verification.
- This tool does not attempt to bypass logins, paywalls, or CAPTCHAs — it
  stops and asks for manual intervention instead, by design.
- Single-job-at-a-time: starting a new job clears the previous output files.

## Future improvement ideas (not implemented yet)

- Resume from `last_url.txt` after an interrupted run
- PDF export of the merged text
- Estimated reading/listening time display before starting
