# Streaming Audio Generation Design

Date: 2026-08-23
Status: approved direction; implementation pending written-spec review

## Goal

Reduce total audiobook generation time by starting text-to-speech work while
chapters are still being collected. Preserve the existing narration order,
audio quality, source behaviour, cancellation contract, and single final MP3.

The expected improvement comes from overlapping chapter collection with TTS.
The final ffmpeg stream-copy merge already takes about 0.3 seconds, so it will
remain a single operation after all numbered audio parts are ready.

## Current flow

1. A source handler collects every readable chapter and returns a list.
2. `app.py` joins all chapter text.
3. The full text is split into roughly 1,500-word, sentence-aware chunks.
4. Up to ten Edge TTS requests run concurrently.
5. ffmpeg concatenates the numbered parts into `book.mp3`.

Collection and TTS are individually parallel where safe, but they never overlap.

## Approaches considered

### 1. Stream chunks to a bounded TTS executor, then merge once

Recommended. Each accepted chapter is emitted to `app.py` in final reading
order. A rolling word buffer produces complete chunks as soon as it can, and a
standard-library executor immediately starts their TTS requests. Numbered part
paths preserve order even if requests finish out of order. The final remainder
is submitted after collection ends, then ffmpeg performs one concat.

This changes only the source-to-app hand-off and the audio orchestration. It
reuses the existing chunk size, retry logic, Edge TTS call, ffmpeg merge, pydub
fallback, output format, and concurrency limit.

### 2. Convert every source handler into an async generator

Rejected for this version. It would express streaming directly, but it would
rewrite all source interfaces and mix synchronous requests, Playwright,
feedparser, yt-dlp subprocesses, and thread-pooled index fetching into one async
model. That is much more code and risk for the same overlap.

### 3. Merge each audio part into a growing MP3 immediately

Rejected. TTS parts complete out of order, repeated merges create avoidable
I/O, and byte-appending independent MP3 files is not a reliable finalisation
strategy. It would optimise the roughly 0.3-second step while risking ordering
and file integrity.

## Architecture

### Source emission

The source handler contract gains an optional `on_chapter` callback:

```text
handler(url, max_pages, on_status, on_page, on_chapter=None)
```

Every handler keeps building and returning its existing `chapters` list. After
a readable chapter is accepted into that list, it calls `on_chapter(chapter)`.
The callback remains optional so direct callers and focused tests continue to
work without constructing an audio pipeline.

For an index-discovered book, chapter fetches may finish out of order, but
`read_chapter_urls()` already consumes their futures in listing order. Emission
will happen at that same ordered consumption point. Chain crawling, YouTube,
RSS, GitHub, and uploaded text already accept chapters sequentially.

### Rolling chunker

`app.py` will keep a rolling list of words. Adding a chapter appends its words
to that buffer. While at least `WORDS_PER_CHUNK` words are available, the
chunker:

1. looks backward through the existing final-15-percent sentence window;
2. cuts at the latest sentence-ending word when one exists;
3. otherwise cuts at exactly 1,500 words; and
4. leaves unused words in the buffer for the next chapter.

When collection ends, the remaining words form one final smaller chunk. Joining
all emitted chunks must reproduce the exact input word sequence with no loss,
duplication, or reordering. Chapter boundaries do not force smaller chunks.

### Streaming TTS builder

A small stateful builder in `app.py` owns the rolling buffer, a
`ThreadPoolExecutor` capped at `MAX_CONCURRENT_TTS`, numbered part paths,
submitted futures, completion counters, and cleanup.

Each complete chunk receives its final index before submission. A worker runs
the existing async `_tts_chunk_to_file()` through its own event loop, retaining
the current one-retry policy. The executor is deliberately standard library;
Edge TTS requests are network-bound and each `Communicate` call already owns
its own connection state.

Completion order affects only the progress counter. Merge order always comes
from submission indices: `part1.mp3`, `part2.mp3`, and so on.

### Finalisation

After the source handler returns:

1. determine the final title, filename, warnings, and chapter range;
2. write `book.txt` exactly as today;
3. flush the rolling buffer as the final TTS chunk;
4. wait for any TTS requests that are still running;
5. fail if any numbered part failed;
6. run the existing single ffmpeg concat, with the existing pydub fallback;
7. remove temporary part files; and
8. publish `book.mp3` and mark the job done.

No MP3 is downloadable until the final merge succeeds.

## Progress and UI behaviour

No template rewrite is required.

During collection, source messages remain authoritative while
`audio_chunks_done` and `audio_chunks_total` update dynamically in job state.
The progress bar continues to represent collection because the final number of
audio chunks is not yet known. After collection, the status changes to
`generating_audio`; its progress reflects already-completed versus total chunks
and may therefore begin above zero. The existing merge and done states remain.

## Cancellation and errors

The feature preserves all-or-nothing MP3 publication:

- Source failure before collection completes: stop accepting text, cancel
  queued TTS work, wait for already-running workers to finish safely, delete
  temporary parts, and publish no MP3.
- TTS failure: record the first failure, stop further submission at the next
  chapter boundary, finish safe worker shutdown, delete temporary parts, and
  report the error.
- User cancellation during collection: use the existing cancellation signal,
  cancel queued work, wait only for already-running requests, clean parts, and
  retain the current stopped state.
- Cancellation or failure after `book.txt` is written: preserve the ready TXT,
  matching current behaviour, but publish no incomplete MP3.
- Merge failure: retain the existing pydub fallback. If both paths fail, remove
  any partial `book.mp3` before reporting the error.

Cleanup must finish before a new job can reuse the shared `output/` directory,
preventing workers from an old job from writing into a new one.

## Files in scope

- `app.py`: rolling chunker, streaming TTS builder, orchestration, progress,
  finalisation, cancellation, and cleanup.
- `sources.py`: optional ordered `on_chapter` emission across every handler and
  the known-index helper.
- `test_sources.py`: focused streaming, ordering, overlap, failure, and cleanup
  checks, while retaining the existing plain-function test style.
- `CLAUDE.md` and `README.md`: describe the new pipeline and its constraints.

No new dependency, database, queue service, endpoint, worker process, or UI
framework is in scope. Chunk size and concurrency remain 1,500 words and ten.

## Verification

The implementation is complete only when all of these hold:

1. Existing offline tests still pass.
2. Streaming across arbitrary chapter boundaries preserves every input word.
3. A full chunk starts TTS before the source handler returns.
4. At most ten TTS workers run concurrently.
5. Out-of-order TTS completion still produces ordered merge inputs.
6. The final partial chunk is included exactly once.
7. Source failure, TTS failure, and cancellation publish no partial MP3 and
   leave no stale part files.
8. A deterministic slow-source/fake-TTS check demonstrates real overlap rather
   than relying on a fragile wall-clock assertion.
9. A representative before/after benchmark records total collection, TTS wait,
   merge, and end-to-end time separately.

## Expected outcome

Fast index-backed jobs should usually save a few seconds. Slow sequential or
reader-backed crawls should save more because TTS can consume early chapters
while later pages are still being fetched. Short jobs that never fill a chunk
before collection ends will see little or no improvement. Final merge time will
remain effectively unchanged.
