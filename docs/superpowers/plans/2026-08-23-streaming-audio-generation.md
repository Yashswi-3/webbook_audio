# Streaming Audio Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start Edge TTS work as soon as complete text chunks arrive while later chapters are still being collected, then publish one correctly ordered MP3 after a single final merge.

**Architecture:** Source handlers keep returning their existing chapter lists but additionally emit each accepted chapter through an optional callback. `app.py` feeds emitted text into a rolling sentence-aware chunker and a ten-worker standard-library TTS executor; numbered part files preserve order and the existing ffmpeg merge runs once after collection and outstanding TTS complete.

**Tech Stack:** Python 3.13, Flask, `concurrent.futures.ThreadPoolExecutor`, asyncio, Edge TTS, ffmpeg, plain `test_*` functions in `test_sources.py`.

**Spec:** `docs/superpowers/specs/2026-08-23-streaming-audio-generation-design.md`

## Global Constraints

- Preserve the current 1,500-word target, final-15-percent sentence lookback, ten-request TTS concurrency cap, and one retry per TTS chunk.
- Preserve chapter order even when chapter fetches or TTS requests finish out of order.
- Keep one final ffmpeg stream-copy merge and the existing pydub fallback.
- Publish no partial MP3 after source failure, TTS failure, or cancellation.
- Keep already-written `book.txt` available when audio fails after collection.
- Finish worker shutdown and temporary-part cleanup before `job_state["running"]` becomes false.
- Add no dependency, service, endpoint, worker process, database, or UI framework.
- Keep source callbacks optional so existing direct callers remain valid.

## File structure

- Modify `app.py`: rolling chunker, bounded streaming TTS builder, finalisation, orchestration, progress, and cleanup.
- Modify `sources.py`: one shared ordered chapter-emission helper and optional `on_chapter` parameters for every handler.
- Modify `test_sources.py`: deterministic chunking, early-start, concurrency, ordering, emission, integration, error, and cleanup checks.
- Modify `README.md`: user-facing description of overlapped collection and audio generation.
- Modify `CLAUDE.md`: maintainer-facing architecture, cancellation, performance, and deployment constraints.

---

### Task 1: Rolling chunker and bounded streaming TTS builder

**Files:**
- Modify: `test_sources.py:494-526`
- Modify: `app.py:23-30`
- Modify: `app.py:225-398`

**Interfaces:**
- Produces: `StreamingWordChunker(words_per_chunk=WORDS_PER_CHUNK)` with `add_text(text) -> list[str]` and `flush() -> list[str]`.
- Produces: `StreamingAudioBuilder(out_folder, voice, rate, progress_cb=None, max_concurrent=MAX_CONCURRENT_TTS, words_per_chunk=WORDS_PER_CHUNK, tts_fn=None)`.
- Produces: `StreamingAudioBuilder.add_text(text)`, `finish(on_wait_start=None) -> list[str]`, `abort()`, and `cleanup()`.
- Reuses: `_tts_chunk_to_file(text, out_path, voice, rate)` and ordered `part{n}.mp3` paths.

- [ ] **Step 1: Write failing rolling-chunker tests**

Append focused plain-function tests using arbitrary chapter boundaries:

```python
def test_streaming_chunker_crosses_chapter_boundaries_without_losing_words():
    import app as webapp

    chunker = webapp.StreamingWordChunker(words_per_chunk=20)
    source = [
        " ".join(f"a{i}" for i in range(13)),
        " ".join([*(f"b{i}" for i in range(5)), "sentence.",
                  *(f"b{i}" for i in range(6, 19))]),
        " ".join(f"c{i}" for i in range(17)),
    ]
    chunks = []
    for chapter in source:
        chunks.extend(chunker.add_text(chapter))
    chunks.extend(chunker.flush())

    assert " ".join(chunks).split() == " ".join(source).split()
    assert all(len(chunk.split()) <= 20 for chunk in chunks)
    assert chunker.flush() == []
```

- [ ] **Step 2: Run the focused test and verify the missing class failure**

Run:

```powershell
& 'C:\Users\yashswi shukla\AppData\Local\Microsoft\WinGet\Links\uv.exe' run --isolated --managed-python --python 3.13 --with-requirements requirements.txt --with pytest -m pytest test_sources.py -q -k streaming_chunker
```

Expected: FAIL because `app.StreamingWordChunker` does not exist.

- [ ] **Step 3: Implement the minimal rolling chunker**

Add a concrete class beside `split_into_word_chunks()`:

```python
class StreamingWordChunker:
    def __init__(self, words_per_chunk=WORDS_PER_CHUNK):
        self.words_per_chunk = words_per_chunk
        self.words = []

    def add_text(self, text):
        self.words.extend((text or "").split())
        chunks = []
        while len(self.words) >= self.words_per_chunk:
            end = _sentence_aware_end(self.words, self.words_per_chunk)
            chunks.append(" ".join(self.words[:end]))
            del self.words[:end]
        return chunks

    def flush(self):
        if not self.words:
            return []
        chunk = " ".join(self.words)
        self.words.clear()
        return [chunk]
```

Extract the existing sentence lookback into `_sentence_aware_end(words, target)` and make `split_into_word_chunks()` use it too, so batch and streaming paths share one boundary rule.

- [ ] **Step 4: Run chunker tests and the two existing splitter tests**

Run the test command with `-k "streaming_chunker or chunk_split"`.

Expected: PASS with exact word preservation and existing batch behaviour unchanged.

- [ ] **Step 5: Write failing early-start, ordering, concurrency, and cleanup tests**

Use `tempfile.mkdtemp()`, `threading.Event`, and a synchronous injected `tts_fn` so tests use no network:

```python
def test_streaming_audio_starts_before_finish_and_returns_parts_in_text_order():
    import os
    import shutil
    import tempfile
    import threading
    import app as webapp

    folder = tempfile.mkdtemp(prefix="wba_stream_")
    started = threading.Event()
    release = threading.Event()

    def fake_tts(text, path, voice, rate):
        started.set()
        assert release.wait(2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    try:
        builder = webapp.StreamingAudioBuilder(
            folder, "voice", "+0%", words_per_chunk=10,
            max_concurrent=2, tts_fn=fake_tts)
        builder.add_text(" ".join(f"w{i}" for i in range(10)))
        assert started.wait(1), "TTS did not start before collection finished"
        builder.add_text(" ".join(f"x{i}" for i in range(15)))
        release.set()
        paths = builder.finish()
        assert [os.path.basename(p) for p in paths] == [
            "part1.mp3", "part2.mp3", "part3.mp3"]
        assert " ".join(open(p, encoding="utf-8").read() for p in paths).split() \
            == [*(f"w{i}" for i in range(10)), *(f"x{i}" for i in range(15))]
    finally:
        shutil.rmtree(folder, ignore_errors=True)
```

Add a second fake that records active workers under a lock, deliberately sleeps
different durations, asserts `max_active <= max_concurrent`, calls `abort()`,
and verifies `part*.mp3` no longer exist after abort returns.

- [ ] **Step 6: Run focused builder tests and verify the missing class failure**

Run with `-k "streaming_audio"`.

Expected: FAIL because `app.StreamingAudioBuilder` does not exist.

- [ ] **Step 7: Implement the bounded builder**

Import `ThreadPoolExecutor` and add the builder. Use a synchronous default
wrapper around the existing async TTS call:

```python
def _tts_chunk_sync(text, out_path, voice, rate):
    asyncio.run(_tts_chunk_to_file(text, out_path, voice, rate))
```

The builder must assign the part index before `executor.submit()`, store futures
in submission order, use `add_done_callback()` only for counters/first-error
recording, and make `finish()` perform this exact sequence:

```python
for chunk in self.chunker.flush():
    self._submit(chunk)
if on_wait_start:
    on_wait_start(self.completed, len(self.futures))
self.executor.shutdown(wait=True, cancel_futures=False)
for future in self.futures:
    future.result()
return list(self.part_paths)
```

`abort()` must call `shutdown(wait=True, cancel_futures=True)` before deleting
part paths. `cleanup()` must be idempotent. `add_text()` and `finish()` must
raise the recorded first TTS failure rather than accepting further work.

- [ ] **Step 8: Run all audio-focused tests**

Run with `-k "streaming or chunk_split"`.

Expected: PASS.

- [ ] **Step 9: Commit the independently tested audio primitives**

```powershell
git add app.py test_sources.py
git commit -m "feat: start audio chunks before collection finishes"
```

---

### Task 2: Ordered chapter emission from every source

**Files:**
- Modify: `test_sources.py:132-170`
- Modify: `sources.py:19-24`
- Modify: `sources.py:953-1040`
- Modify: `sources.py:1062-1110`
- Modify: `sources.py:1154-1227`
- Modify: `sources.py:1548-1602`
- Modify: `sources.py:1618-1863`
- Modify: `sources.py:1870-1878`

**Interfaces:**
- Produces: `_add_chapter(chapters, chapter, on_chapter=None) -> None`.
- Changes: every `HANDLERS` function accepts optional `on_chapter=None` after `on_page`.
- Changes: `read_chapter_urls(..., on_page, on_chapter=None)` emits fetched chapters in listing order.
- Changes: `fetch_uploaded_text(..., on_page, on_chapter=None)` uses the same emission helper.

- [ ] **Step 1: Write failing source-emission tests**

```python
def test_add_chapter_appends_before_emitting_the_same_object():
    chapters, seen = [], []
    chapter = {"page": 1, "text": "hello"}
    sources._add_chapter(chapters, chapter,
                         lambda emitted: seen.append((len(chapters), emitted)))
    assert chapters == [chapter]
    assert seen == [(1, chapter)]


def test_uploaded_text_emits_its_chapter():
    seen = []
    result = sources.fetch_uploaded_text(
        "one two three", "sample.txt", lambda page: None, seen.append)
    assert seen == result["chapters"]
```

Add a known-index test that temporarily replaces `_read_one_chapter` with a
fake whose later URL finishes first, calls `read_chapter_urls(...,
on_chapter=seen.append)`, restores the original function, and asserts `seen`
matches the returned list order.

- [ ] **Step 2: Run emission tests and verify signature/helper failures**

Run with `-k "add_chapter or emits_its_chapter or emits_known_index"`.

Expected: FAIL because `_add_chapter` and callback parameters do not exist.

- [ ] **Step 3: Implement the shared emission helper and optional parameters**

```python
def _add_chapter(chapters, chapter, on_chapter=None):
    chapters.append(chapter)
    if on_chapter:
        on_chapter(chapter)
```

Replace every direct `chapters.append(...)` in a source handler with
`_add_chapter(...)`. Thread `on_chapter` through `fetch_crawl()` into
`read_chapter_urls()`. Emit only readable chapters, never skipped page-log
entries. Keep callback invocation at the existing ordered consumption point.

- [ ] **Step 4: Verify no handler bypasses the helper**

Run:

```powershell
rg -n "chapters\.append" sources.py
```

Expected: only the single append inside `_add_chapter`.

- [ ] **Step 5: Run emission tests and the full offline suite**

Run the complete `test_sources.py` command.

Expected: all existing and new tests pass.

- [ ] **Step 6: Commit ordered source emission**

```powershell
git add sources.py test_sources.py
git commit -m "feat: emit chapters to audio as they arrive"
```

---

### Task 3: Integrate streaming generation with job lifecycle

**Files:**
- Modify: `test_sources.py:494-end`
- Modify: `app.py:91-188`
- Modify: `app.py:353-519`

**Interfaces:**
- Consumes: `StreamingAudioBuilder.add_text()`, `finish()`, `abort()`, and `cleanup()` from Task 1.
- Consumes: optional `on_chapter` handler callback from Task 2.
- Changes: `_write_and_narrate(chapters, book_title, voice, rate, audio_builder=None)`.
- Changes: `run_pipeline()` and `run_pipeline_from_text()` create the builder before content collection and pass chapter text into it.
- Preserves: `generate_audio()` as the batch-compatible wrapper for direct callers.

- [ ] **Step 1: Write a failing integration test proving overlap**

Install a fake handler temporarily in `sources.HANDLERS`, inject a fake TTS
function through a replaceable `STREAMING_TTS_FACTORY`, and use events:

```python
def test_run_pipeline_starts_tts_before_source_returns():
    import threading
    import app as webapp

    tts_started = threading.Event()
    source_saw_tts = []

    def fake_handler(url, max_pages, on_status, on_page, on_chapter=None):
        chapter = {"page": 1, "url": url, "title": "Chapter 1",
                   "text": " ".join(["word"] * 20), "chapter_num": 1}
        on_chapter(chapter)
        source_saw_tts.append(tts_started.wait(1))
        return {"chapters": [chapter], "book_title": "Book"}

    # The test factory returns a builder using words_per_chunk=10 and a fake
    # tts_fn that sets tts_started before writing its part file.
    # Patch merge functions to write a sentinel final file, then restore every
    # global in finally.
    # Assert source_saw_tts == [True] and final state is done/mp3_ready.
```

Use a narrow module-level `STREAMING_TTS_FACTORY = StreamingAudioBuilder` seam
instead of adding production configuration or a dependency-injection framework.

- [ ] **Step 2: Run the overlap integration test and verify failure**

Expected: FAIL because `run_pipeline()` still calls the four-argument handler
and starts TTS only after it returns.

- [ ] **Step 3: Integrate the builder into both orchestration paths**

In `run_pipeline()`:

```python
builder = STREAMING_TTS_FACTORY(
    OUTPUT_FOLDER, voice, rate,
    progress_cb=lambda done, total: update_state(
        audio_chunks_done=done, audio_chunks_total=total))

def on_chapter(chapter):
    builder.add_text(chapter["text"])

result = handler(start_url, max_pages, on_status, append_page_log, on_chapter)
_write_and_narrate(result["chapters"], result["book_title"], voice, rate,
                    audio_builder=builder)
```

Create the builder before `fetch_uploaded_text()` in `run_pipeline_from_text()`
and pass the same callback. In every `except` path call `builder.abort()` before
setting `running=False`; make abort idempotent so the `finally` safety call is
harmless.

- [ ] **Step 4: Refactor finalisation without changing output contracts**

Make `_write_and_narrate()` write and publish TXT first, then either finish the
provided streaming builder or construct a batch-compatible builder for the full
text. Set `generating_audio` before waiting, use `on_wait_start(done, total)` to
seed accurate counters, merge once, and run cleanup in `finally`:

```python
part_paths = audio_builder.finish(on_wait_start=wait_start)
update_state(status="merging_audio",
             message=f"Merging {len(part_paths)} audio chunks into book.mp3...")
try:
    _merge_via_ffmpeg_concat(part_paths, final_path)
except Exception:
    _merge_via_pydub(part_paths, final_path)
finally:
    audio_builder.cleanup()
```

If both merge paths fail, remove a partial `book.mp3` before re-raising.

- [ ] **Step 5: Write and run cancellation/error cleanup tests**

Add tests using fake TTS workers and fake handlers for:

- a handler raising `SourceError` after emitting one full chunk;
- a TTS function raising after one successful part;
- `cancel_requested=True` while workers are queued.

Each test must wait for `run_pipeline()` to return and assert:

```python
assert not glob.glob(os.path.join(webapp.OUTPUT_FOLDER, "part*.mp3"))
assert not webapp.job_state["mp3_ready"]
assert not webapp.job_state["running"]
```

For the post-collection TTS-failure case, additionally assert
`job_state["txt_ready"]` remains true.

- [ ] **Step 6: Run all tests**

Run the complete offline suite.

Expected: every existing and new test passes with no network or ffmpeg use in
the new tests.

- [ ] **Step 7: Commit integrated job behaviour**

```powershell
git add app.py test_sources.py
git commit -m "feat: overlap chapter collection with narration"
```

---

### Task 4: Documentation, verification, benchmark, and release

**Files:**
- Modify: `README.md:17-41`
- Modify: `README.md:133-177`
- Modify: `CLAUDE.md:45-73`
- Modify: `CLAUDE.md:106-119`

**Interfaces:**
- Documents: optional ordered chapter emission, rolling 1,500-word chunks,
  ten-worker TTS overlap, one final merge, progress semantics, and cleanup.
- Releases: fast-forward `main` to the verified feature branch and push
  `refs/heads/main` to `origin`.

- [ ] **Step 1: Update maintainer and user documentation**

In `CLAUDE.md`, replace the serial flow description with:

```text
run_pipeline() starts a streaming audio builder before collection. Each source
emits accepted chapters in reading order; complete 1500-word sentence-aware
chunks start Edge TTS immediately, up to 10 concurrently. After collection,
the final partial chunk is submitted, outstanding requests finish, and ffmpeg
performs one ordered stream-copy merge.
```

State explicitly that repeated incremental merging is not used, the source
callback is optional, and job shutdown waits for active workers before clearing
the single shared output directory.

In `README.md`, explain that audio now begins generating while later chapters
load and that short jobs may see little change.

- [ ] **Step 2: Run documentation and repository checks**

```powershell
git diff --check
rg -n "collects chapters.*split|collection.*TTS|streaming audio" README.md CLAUDE.md
```

Expected: no whitespace errors and no stale claim that all chapters must be
collected before TTS begins.

- [ ] **Step 3: Run the full offline suite twice**

Run both:

```powershell
& 'C:\Users\yashswi shukla\AppData\Local\Microsoft\WinGet\Links\uv.exe' run --isolated --managed-python --python 3.13 --with-requirements requirements.txt test_sources.py
& 'C:\Users\yashswi shukla\AppData\Local\Microsoft\WinGet\Links\uv.exe' run --isolated --managed-python --python 3.13 --with-requirements requirements.txt --with pytest -m pytest test_sources.py -q
```

Expected: the same complete test count passes through both supported runners.

- [ ] **Step 4: Run a deterministic overlap benchmark**

Use a one-off Python command that feeds 16 ten-word chunks over 3.6 seconds into
a builder whose fake TTS takes 2.2 seconds per chunk with concurrency ten.
Record:

- sequential control = source duration + batch TTS duration;
- streaming duration = source emission through final worker completion;
- observed seconds and percentage saved; and
- final part ordering.

Require streaming duration to be lower than the sequential control and report
the measured values; do not encode a fragile timing threshold into the suite.

- [ ] **Step 5: Commit documentation**

```powershell
git add README.md CLAUDE.md
git commit -m "docs: explain streaming audio generation"
```

- [ ] **Step 6: Verify branch state before integration**

```powershell
git status --short --branch
git log --oneline --decorate main..codex/streaming-audio-generation
git ls-remote origin refs/heads/main
```

Expected: feature worktree clean; remote main still at the feature branch's
base or can be fetched and reviewed before integration.

- [ ] **Step 7: Fast-forward desktop main and push**

From `C:\Users\yashswi shukla\Desktop\Project\webbook_audio`:

```powershell
git merge --ff-only codex/streaming-audio-generation
git push origin main
```

Expected: local `main`, `origin/main`, and GitHub `refs/heads/main` all resolve
to the same verified release commit. Do not force-push.

- [ ] **Step 8: Report the deploy boundary honestly**

Confirm the GitHub main hash with `git ls-remote`. If no authenticated Render
deployment status or public service URL is discoverable, report that the push
trigger was confirmed but the Render deployment itself could not be observed;
provide the commit hash the user should see before mobile testing.
