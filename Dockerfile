FROM python:3.11-slim

# ffmpeg is required by pydub for audio merging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skip Playwright/Chromium on the free tier to save RAM + build time.
# The app already falls back to plain requests-based crawling if
# Playwright isn't installed (per its own README).

COPY . .

ENV PORT=10000
# YouTube blocks datacenter IPs, so both video routes can only fail here,
# and a public MP4 endpoint is bandwidth plus an abuse target. Off by
# default in any container; local runs (python app.py) still have it on.
ENV ALLOW_VIDEO_DOWNLOAD=0
EXPOSE 10000

# shell form so $PORT expands - Render assigns the port at runtime
CMD gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 600 app:app
