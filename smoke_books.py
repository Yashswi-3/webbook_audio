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

CHAPTERS_WANTED = 10

# (name, minimum chapters expected, url). Every book a real failure was
# reported against lives here. The minimum is 10 except where the book itself
# ends sooner - three links point within a handful of chapters of the newest
# one, and returning more than exists would be a worse bug than returning
# fewer. Each run also proves no chapter came from a different book: a job
# that collected six unrelated novels passed every other check, because
# nothing it returned carried a chapter number to compare.
BOOKS = [
    # Pasted at chapter 331 of a fiction that has published 336, so six is
    # the whole remainder. Same for the next two: these links point near the
    # newest chapter, and a run that returned more would be inventing it.
    ("royalroad chapter", 6,
     "https://www.royalroad.com/fiction/61686/that-time-an-american-was-reincarnated-into-another/chapter/3789241/chapter-331-drastic-measures"),
    ("royalroad chapter 2", 5,
     "https://www.royalroad.com/fiction/50137/the-young-master-in-the-shadows/chapter/3820222/chapter-543-let-the-tournament-begin"),
    ("fanfiction near the end", 3,
     "https://m.fanfiction.net/s/14387311/44/"),
    ("webnovel mobile numeric", 10,
     "https://m.webnovel.com/book/36163297308457405/98448090515689749"),
    ("webnovel mobile numeric 2", 10,
     "https://m.webnovel.com/book/35895681908097305/98383742778794446"),
    # Pasted at chapter 306 of 312. Before the forward-only rule this returned
    # ten by appending three chapters from elsewhere in the catalogue - the
    # count looked better and the audiobook was wrong.
    ("webnovel mobile numeric 3", 7,
     "https://m.webnovel.com/book/35069975308849905/98825275483160418"),
    ("webnovel book page, no slug", 10,
     "https://m.webnovel.com/book/22611582906655205"),
    ("webnovel book page, slug", 10,
     "https://m.webnovel.com/book/naruto-breaking-every-limit_36785133108809405"),
    ("webnovel mobile slug", 10,
     "https://m.webnovel.com/book/my-pet-slime-gives-me-10x-rewards_36208943200327705/unstable-rift_98719683577958774"),
    ("webnovel brackets in slug", 10,
     "https://m.webnovel.com/book/classroom-of-the-elite-i-have-the-ability-to-read-minds!-(cote)_28776837400218905/childhood-friend!_77247248521555336"),
    ("freewebnovel chapter", 10,
     "https://freewebnovel.com/novel/vampires-slice-of-life/chapter-960"),
    ("freewebnovel book page", 10,
     "https://freewebnovel.com/novel/im-the-evil-lord-of-an-intergalactic-empire"),
]


def check(name, url, wanted):
    """Return (ok, detail). Fewer chapters than the book should give is a fail."""
    started = time.time()
    try:
        result = sources.fetch_crawl(url, CHAPTERS_WANTED,
                                     lambda message: None, lambda page: None)
    except Exception as e:                       # noqa: BLE001 - report anything
        return False, f"raised {type(e).__name__}: {e}"

    chapters = result["chapters"]
    elapsed = time.time() - started
    if len(chapters) < wanted:
        return False, (f"{len(chapters)}/{wanted} chapters in {elapsed:.1f}s - "
                       f"warning: {result.get('warning')}")

    numbers = [c.get("chapter_num") for c in chapters]
    known = [n for n in numbers if n is not None]
    if known != sorted(known):
        return False, f"out of reading order: {numbers}"

    # Every chapter must belong to the book that was asked for. Ordering alone
    # cannot see this: a run that collected six unrelated novels had no chapter
    # numbers at all, so the order check passed on an empty list while the
    # audiobook jumped between six different stories.
    # Compare on one host: a mobile link is answered with desktop chapter URLs
    # on purpose, and a check that misses that flags every correct chapter.
    def canonical(u):
        return sources.desktop_equivalent(u) or u

    ids, prefix = sources.work_identity(canonical(url))
    strays = [c["url"] for c in chapters
              if not sources._belongs_to_work(canonical(c["url"]), ids,
                                              canonical(url), prefix)]
    if strays:
        return False, (f"{len(strays)} chapter(s) from another book, "
                       f"first: {strays[0][:70]}")

    titled = len({(c.get("title") or "")[:40] for c in chapters})
    return True, (f"{len(chapters)} chapters, {titled} distinct titles, "
                  f"order {numbers}, {elapsed:.1f}s")


def main(patterns):
    # Refuse every direct fetch, so this exercises the path a datacenter-hosted
    # instance actually takes. Locally the direct fetch would succeed and hide
    # exactly the failures this is here to catch.
    sources.fetch_with_retry = lambda url: (None, "direct fetch refused (smoke test)")

    books = [b for b in BOOKS
             if not patterns or any(p.lower() in b[0].lower() for p in patterns)]
    if not books:
        print("no books matched", patterns)
        return 1

    failures = 0
    for name, wanted, url in books:
        ok, detail = check(name, url, wanted)
        print(f"{'ok  ' if ok else 'FAIL'} {name:28} {detail}")
        failures += 0 if ok else 1

    print()
    print("all books read" if not failures else f"{failures} book(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
