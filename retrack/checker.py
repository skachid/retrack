"""The actual audit logic: one catalog row in, one verdict out.

Verdicts:
  RED    - The MLC has no record of this recording, or it isn't linked to a song.
           Mechanical royalties for it are probably sitting unmatched.
           With --ai-search, RED is split into two cases (see red_case):
             NOT_LINKED - the song IS registered (found by title/writers), just
                          not linked to this ISRC. Fix: claim it, attach the ISRC.
             NOT_FOUND  - searched by title and writers too, found nothing.
                          Fix: register it from scratch.
  YELLOW - The song is there, but something's off: publisher shares don't add up
           to 100%, nobody's collecting, or your writers aren't listed (name mismatch).
  GREEN  - Recording is linked to a work, shares are fully claimed, writers match.
"""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .work_search import search_for_work

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"

# How similar two names must be to count as "the same person" (0..1).
NAME_MATCH_THRESHOLD = 0.85

# When the basic match fails we ask a name matcher (see name_matcher.py).
# Its "yes" only counts if it is at least this sure.
SMART_MATCH_MIN_CONFIDENCE = 0.8

# The two kinds of RED (only known when --ai-search ran).
NOT_LINKED, NOT_FOUND = "NOT_LINKED", "NOT_FOUND"
RED_CASES = {
    NOT_LINKED: "Registered but not linked to your recording",
    NOT_FOUND: "Not found anywhere",
}

# Short codes for the problems on yellow songs, recorded as each rule fires, so a
# summary can count them (report.findings_summary) without repeating the rules.
MULTIPLE_WORKS, NO_PUBLISHER, PARTIAL_SHARE, OVERCLAIM, MISSING_WRITERS, SECOND_REGISTRATION = (
    "MULTIPLE_WORKS", "NO_PUBLISHER", "PARTIAL_SHARE", "OVERCLAIM", "MISSING_WRITERS", "SECOND_REGISTRATION")

# A work-search suggestion (see work_search.py) only counts if the searcher says
# "same song" and is at least this sure. Set higher than for names: a wrong match
# here could send someone to claim a song that isn't theirs.
WORK_MATCH_MIN_CONFIDENCE = 0.85


@dataclass
class CatalogRow:
    title: str
    artist: str
    isrc: str
    writers: list  # e.g. ["John Smith", "Ana Lopez"]


@dataclass
class Result:
    row: CatalogRow
    status: str
    song_code: str = ""
    iswc: str = ""
    claimed_share: float = 0.0
    mlc_writers: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # FYI only, doesn't change the status
    missing_writers: list = field(default_factory=list)
    findings: list = field(default_factory=list)  # codes like PARTIAL_SHARE, one per problem found
    red_case: str = ""  # NOT_LINKED, NOT_FOUND, or "" (not searched / search incomplete)
    suggested_match: object = None  # a work_search.Candidate the user must confirm
    other_matches: list = field(default_factory=list)  # more Candidates that also passed the bar


def load_catalog(path):
    """Read the catalog CSV from a file on disk."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return parse_catalog(f)


def parse_catalog(lines):
    """Read catalog rows from CSV text. Writers are separated by ';' in one column.

    `lines` can be an open file or a string (e.g. an uploaded file's contents).
    """
    if isinstance(lines, str):
        lines = io.StringIO(lines.lstrip("\ufeff"))  # drop Excel's invisible BOM marker
    rows = []
    reader = csv.DictReader(lines)
    needed = {"title", "artist", "isrc", "writers"}
    cols = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
    missing = needed - cols
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
    for raw in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if isinstance(v, str) or v is None}
        if not r["isrc"]:
            continue
        rows.append(
            CatalogRow(
                title=r["title"],
                artist=r["artist"],
                isrc=normalize_isrc(r["isrc"]),
                writers=[w.strip() for w in r["writers"].split(";") if w.strip()],
            )
        )
    return rows


def normalize_isrc(isrc):
    """'us-abc-24-00001' -> 'USABC2400001'"""
    return re.sub(r"[^A-Za-z0-9]", "", isrc).upper()


def normalize_name(name):
    """Lowercase, strip accents and punctuation: 'José  O'Neil' -> 'jose oneil'"""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z ]", "", name.lower())
    return " ".join(name.split())


def names_match(a, b):
    a, b = normalize_name(a), normalize_name(b)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= NAME_MATCH_THRESHOLD


def find_writer(writer, mlc_writers, name_matcher=None):
    """Is `writer` on the MLC record? Returns (found, note).

    Pass 1: the cheap local check (names_match). Free and instant.
    Pass 2: only if pass 1 found nothing, ask the name matcher about each MLC name.
    """
    if any(names_match(writer, m) for m in mlc_writers):
        return True, ""
    if name_matcher is None:
        return False, ""
    for m in mlc_writers:
        verdict = name_matcher.same_person(writer, m)
        if verdict.same_person and verdict.confidence >= SMART_MATCH_MIN_CONFIDENCE:
            return True, (f"“{writer}” matched to MLC writer “{m}” "
                          f"({verdict.confidence:.0%} sure): confirm it's the same person.")
    return False, ""


def total_claimed_share(work):
    """Sum of top-level publisher collection shares (MLC reports these as percents)."""
    return round(sum(float(p.get("collectionShare") or 0) for p in work.get("publishers") or []), 2)


def mlc_writer_names(work):
    names = []
    for w in work.get("writers") or []:
        full = f"{w.get('writerFirstName') or ''} {w.get('writerLastName') or ''}".strip()
        if full:
            names.append(full)
    return names


def check_row(row, client, name_matcher=None, work_searcher=None):
    """One catalog row in, one Result out.

    work_searcher is optional (--ai-search). It only runs for songs that need
    it: RED ones, and ones with writers missing from the MLC record.
    """
    result = check_by_isrc(row, client, name_matcher)
    if work_searcher is not None:
        if result.status == RED:
            apply_red_search(result, client, work_searcher)
        elif result.missing_writers:
            apply_missing_writer_search(result, client, work_searcher)
    return result


def check_by_isrc(row, client, name_matcher=None):
    """The basic check: follow the ISRC to the work and look at it."""
    recordings = client.search_recordings(row.isrc)
    song_codes = sorted({r.get("mlcsongCode") for r in recordings if r.get("mlcsongCode")})

    if not recordings:
        return Result(
            row,
            RED,
            problems=["The MLC has no recording with this ISRC."],
            fixes=[
                "Register the song (composition) with The MLC, listing this ISRC.",
                "Double-check the ISRC against your distributor's dashboard.",
            ],
        )
    if not song_codes:
        return Result(
            row,
            RED,
            problems=["The MLC knows the recording but it isn't linked to any song."],
            fixes=["Register the song with The MLC and attach this ISRC to it."],
        )

    works = client.get_works(song_codes)
    if not works:
        return Result(
            row,
            RED,
            song_code=", ".join(song_codes),
            problems=["Recording points to a song code, but The MLC returned no work for it."],
            fixes=["Look up the song code in The MLC's Public Work Search and claim it."],
        )

    work = works[0]  # one ISRC can map to several works (medleys); v1 checks the first
    result = Result(
        row,
        GREEN,
        song_code=work.get("mlcSongCode", ""),
        iswc=work.get("iswc") or "",
        claimed_share=total_claimed_share(work),
        mlc_writers=mlc_writer_names(work),
    )
    if len(works) > 1:
        result.problems.append(f"This ISRC is linked to {len(works)} works; only the first was checked.")
        result.findings.append(MULTIPLE_WORKS)

    if not work.get("publishers"):
        result.problems.append("No publisher or administrator is collecting on this song.")
        result.findings.append(NO_PUBLISHER)
        result.fixes.append("Claim your share in The MLC Portal (self-published writers can claim directly).")
    elif result.claimed_share < 100:
        result.problems.append(f"Only {result.claimed_share:g}% of the song is claimed.")
        result.findings.append(PARTIAL_SHARE)
        result.fixes.append("Find which co-writer's share is unclaimed and get it claimed in The MLC Portal.")
    elif result.claimed_share > 100:
        result.problems.append(f"Shares add up to {result.claimed_share:g}%, which means there's a dispute or overclaim.")
        result.findings.append(OVERCLAIM)
        result.fixes.append("Check The MLC Portal for a share conflict on this song.")

    missing = []
    for w in row.writers:
        found, note = find_writer(w, result.mlc_writers, name_matcher)
        if not found:
            missing.append(w)
        if note:
            result.notes.append(note)
    result.missing_writers = missing
    if missing:
        result.problems.append(f"Writers not found on the MLC record: {', '.join(missing)}.")
        result.findings.append(MISSING_WRITERS)
        result.fixes.append(
            "Check whether these writers are registered under a different name, or add them to the work."
        )

    if result.problems:
        result.status = YELLOW
    return result


# ---- Work search (--ai-search) ---------------------------------------------------
#
# work_search.py only *suggests* songs. The rules below decide what a suggestion
# means for the verdict, so all red/yellow/green logic stays in this file.

def confident_matches(outcome):
    """Suggestions that pass our bar, most confident first."""
    ok = [c for c in outcome.candidates if c.same_song and c.confidence >= WORK_MATCH_MIN_CONFIDENCE]
    return sorted(ok, key=lambda c: c.confidence, reverse=True)


def describe(c):
    writers = ", ".join(c.writers) or "no writers listed"
    return f"song code {c.song_code}, “{c.title}” by {writers}"


def apply_red_search(result, client, work_searcher):
    """Split a RED result into NOT_LINKED or NOT_FOUND (or leave it if the search failed)."""
    row = result.row
    outcome = search_for_work(row.title, row.writers, client, work_searcher)
    matches = confident_matches(outcome)

    if matches:
        best = matches[0]
        result.red_case = NOT_LINKED
        result.suggested_match = best
        result.other_matches = matches[1:]
        result.problems.insert(0, f"Looks registered at The MLC as “{best.title}” "
                                  f"(song code {best.song_code}), but not linked to your recording.")
        result.fixes = [
            f"Please confirm it's your song: open song code {best.song_code} in The MLC Portal "
            "and check the title and writers.",
            "If it is, claim your share of it there (if nobody has yet). Don't register the song "
            "again: a duplicate registration slows payment down.",
            f"Attach ISRC {row.isrc} to song code {best.song_code} (in the Portal, or ask your "
            "publisher or administrator), so plays of your recording are matched to it.",
            "If the title or writer names on that record are wrong, ask The MLC to correct them.",
        ]
        if result.other_matches:
            codes = ", ".join(c.song_code for c in result.other_matches)
            result.problems.append(f"Other registrations also look like this song ({codes}). "
                                   "There may be duplicates.")
        return

    if not outcome.complete:
        # Some searches failed, so we can't honestly say "not found anywhere".
        result.notes.append("The search by title and writers couldn't finish (The MLC or the AI "
                            "service didn't answer), so this song might still be registered "
                            "under another title. Try again later.")
        return

    result.red_case = NOT_FOUND
    n = len(outcome.queries)
    result.problems.insert(0, f"Not found anywhere at The MLC: we also searched by title and writer "
                              f"names ({n} {'search' if n == 1 else 'searches'}) and found no "
                              "registration of this song.")
    result.fixes = [
        "Register the song (composition) with The MLC from scratch, and list this ISRC on it.",
        "Double-check the ISRC against your distributor's dashboard.",
    ]


def apply_missing_writer_search(result, client, work_searcher):
    """Writers missing? Look for a second registration of the same song that lists them.

    This only adds a suggestion and a fix step; the song is already YELLOW.
    """
    row = result.row
    linked = [c.strip() for c in result.song_code.split(",") if c.strip()]
    outcome = search_for_work(row.title, row.writers, client, work_searcher, exclude_codes=linked)
    matches = confident_matches(outcome)
    if not matches:
        return
    best = matches[0]
    result.suggested_match = best
    result.other_matches = matches[1:]
    result.findings.append(SECOND_REGISTRATION)
    result.fixes.append(f"Please confirm whether {describe(best)} is a second registration of this "
                        "song. If it is, ask The MLC to merge the two so all writers are on one record.")


def needs_ai_after_checkout(result, smart_names, ai_search):
    """Web UI: does this paid song get the AI checks the user turned on?

    AI search places red songs nobody has placed yet (register or link?); smart names
    takes a second look at writers missing from the MLC record. Nothing else uses the AI.
    """
    unplaced_red = result.status == RED and not result.red_case
    return bool((ai_search and unplaced_red) or (smart_names and result.missing_writers))


def check_catalog(rows, client, name_matcher=None, work_searcher=None):
    return [check_row(r, client, name_matcher, work_searcher) for r in rows]
