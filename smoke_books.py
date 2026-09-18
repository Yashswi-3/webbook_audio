"""
Real-book smoke test — the one the offline suite cannot be.

Every failure this project has shipped looked identical from the outside: a
job that finished, reported success, and handed back one chapter. The unit
tests passed through all of them, because each bug lived in how a real
catalog is written, not in logic a fixture could describe.

So this runs actual books end to end with every direct fetch refused, which
is how the deployed server sees the web, and fails loudly if a book that
should have chapters comes back with one.

    python smoke_books.py            # all books
    python smoke_books.py webnovel   # only matching names

Needs the network and a minute. Run it before deploying a change to
sources.py; it would have caught all three of 2026-09-18's faults in 30
seconds each.
"""

import sys
import time

import sources

CHAPTERS_WANTED = 4

BOOKS = [
    ("webnovel mobile",          "https://m.webnovel.com/book/attack-on-titan-the-titan-of-fate_34614898400796905/chapter-1-child_92942474883954069"),
    ("webnovel brackets in slug", "https://m.webnovel.com/book/classroom-of-the-elite-i-have-the-ability-to-read-minds!-(cote)_28776837400218905/childhood-friend!_77247248521555336"),
    ("webnovel desktop",         "https://www.webnovel.com/book/slime-evolution_35006015000821605/01---world-without-hope_94025290363228627"),
    ("royalroad chain",          "https://www.royalroad.com/fiction/61686/that-time-an-american-was-reincarnated-into-another/chapter/2921937/chapter-300-six-long-years"),
]


def check(name, url):
    """Return (ok, detail). A book that reads fewer than 2 chapters failed."""
    started = time.time()
    try:
        result = sources.fetch_crawl(url, CHAPTERS_WANTED,
                                     lambda message: None, lambda page: None)
    except Exception as e:                       # noqa: BLE001 - report anything
        return False, f"raised {type(e).__name__}: {e}"

    chapters = result["chapters"]
    elapsed = time.time() - started
    if len(chapters) < 2:
        return False, (f"{len(chapters)} chapter(s) in {elapsed:.1f}s - "
                       f"warning: {result.get('warning')}")

    numbers = [c.get("chapter_num") for c in chapters]
    known = [n for n in numbers if n is not None]
    if known != sorted(known):
        return False, f"out of reading order: {numbers}"

    return True, f"{len(chapters)} chapters, order {numbers}, {elapsed:.1f}s"


def main(patterns):
    # Refuse every direct fetch, so this exercises the path a datacenter-hosted
    # instance actually takes. Locally the direct fetch would succeed and hide
    # exactly the failures this is here to catch.
    sources.fetch_with_retry = lambda url: (None, "direct fetch refused (smoke test)")

    books = [(n, u) for n, u in BOOKS
             if not patterns or any(p.lower() in n.lower() for p in patterns)]
    if not books:
        print("no books matched", patterns)
        return 1

    failures = 0
    for name, url in books:
        ok, detail = check(name, url)
        print(f"{'ok  ' if ok else 'FAIL'} {name:26} {detail}")
        failures += 0 if ok else 1

    print()
    print("all books read" if not failures else f"{failures} book(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
