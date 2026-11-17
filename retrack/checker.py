"""The actual audit logic: one catalog row in, one verdict out.

Verdicts:
  RED    - The MLC has no record of this recording, or it isn't linked to a song.
           Mechanical royalties for it are probably sitting unmatched.
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

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"

# How similar two names must be to count as "the same person" (0..1).
NAME_MATCH_THRESHOLD = 0.85

# When the basic match fails we ask a name matcher (see name_matcher.py).
# Its "yes" only counts if it is at least this sure.
SMART_MATCH_MIN_CONFIDENCE = 0.8


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
            return True, (f'"{writer}" matched to MLC writer "{m}" '
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


def check_row(row, client, name_matcher=None):
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

    if not work.get("publishers"):
        result.problems.append("No publisher or administrator is collecting on this song.")
        result.fixes.append("Claim your share in The MLC Portal (self-published writers can claim directly).")
    elif result.claimed_share < 100:
        result.problems.append(f"Only {result.claimed_share:g}% of the song is claimed.")
        result.fixes.append("Find which co-writer's share is unclaimed and get it claimed in The MLC Portal.")
    elif result.claimed_share > 100:
        result.problems.append(f"Shares add up to {result.claimed_share:g}%, which means there's a dispute or overclaim.")
        result.fixes.append("Check The MLC Portal for a share conflict on this song.")

    missing = []
    for w in row.writers:
        found, note = find_writer(w, result.mlc_writers, name_matcher)
        if not found:
            missing.append(w)
        if note:
            result.notes.append(note)
    if missing:
        result.problems.append(f"Writers not found on the MLC record: {', '.join(missing)}.")
        result.fixes.append(
            "Check whether these writers are registered under a different name, or add them to the work."
        )

    if result.problems:
        result.status = YELLOW
    return result


def check_catalog(rows, client, name_matcher=None):
    return [check_row(r, client, name_matcher) for r in rows]
