"""The fix pack: files that help the user fix the songs they choose (the paid part).

The scan is free. Fixing costs a flat FIX_PRICE_USD per song, the same for every
verdict. (No payment is taken yet; that comes with the web app.)

What each kind of song gets:
  REGISTER ("Not found anywhere")
      Rows for The MLC's Bulk Work Registration template. The user downloads
      the official template from their own MLC Portal and passes it in with
      --template. We fill a COPY of it and never recreate it. Without a
      template we write a clearly labelled CSV worksheet instead.
  LINK ("Registered but not linked")
      match_suggestions.csv: ISRC <-> MLC song code with titles, writers and
      confidence, for the user to review. Plus BulkMatchSuggestions_YYYYMMDD.csv
      in The MLC's exact Bulk Match Submission format (ISRC,MLC Song Code).
  CLAIM (yellow: partial shares, nobody collecting, missing writers)
      claim_checklist.md: what to do in the Portal, song by song.

Ownership rule: we never guess publisher shares, IPIs, role codes or publisher
names. We fill in only what we know from the catalog and The MLC, and leave the
rest blank and marked "you must fill this in".

Compliance text: on our own files, the README and ATTESTATION.txt. Files whose
format The MLC dictates (the template copy, the bulk match CSV) get nothing
extra: The MLC's template is left exactly as it was, apart from the filled rows.

Attestation rule: build_fix_pack() refuses to write anything unless it's given
an Attestation (the user confirmed they reviewed the data and control the
shares). No confirmation, no files.

Retrack never logs into The MLC Portal, never asks for a Portal password and
never uploads anything. This file makes no network calls at all.
"""

import csv
import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from .checker import NOT_FOUND, NOT_LINKED, RED, YELLOW
from .compliance import ATTESTATION_TEXT, COMPLIANCE_TEXT, NEVER_TEXT
from .mlc_client import split_name

# Placeholder price, in US dollars, per fixed song. The same for every verdict:
# don't add per-verdict prices (tests/test_fixes.py checks this).
FIX_PRICE_USD = 5.00

REGISTER, LINK, CLAIM = "register", "link", "claim"

FIXES_DIR = "fix_pack"

MUST_FILL = "YOU MUST FILL THIS IN"
MAX_TEMPLATE_ROWS = 300  # The MLC's limit per bulk upload (rows, not works)

FILES = {
    "xlsx": "bulk_work_registration.xlsx",
    "worksheet": "registration_worksheet.csv",
    "matches": "match_suggestions.csv",
    "bulk_match": "BulkMatchSuggestions_{date}.csv",  # The MLC's naming, minus the member name
    "checklist": "claim_checklist.md",
    "attestation": "ATTESTATION.txt",
    "readme": "README.md",
}

TEMPLATE_HELP = ("Get the official template from your own MLC Portal: log in, go to Works "
                 "Registration, then Bulk Registration Tool, and download the template. Then run "
                 "Retrack again with --template path/to/template.xlsx.")

MLC_HELP_LINKS = [
    ("How to complete the bulk work document",
     "https://help.themlc.com/en/support/how-do-i-complete-the-bulk-work-document"),
    ("Bulk Match Submission Process", "https://pages.themlc.com/bulk-match-submission-process"),
    ("What is the Matching Tool?", "https://help.themlc.com/en/support/what-is-the-matching-tool"),
]


class FixPackError(Exception):
    """Something stops us building the fix pack. Nothing has been written."""


# ---- Which songs, and what they cost ---------------------------------------------

@dataclass
class FixPlan:
    register: list = field(default_factory=list)
    link: list = field(default_factory=list)
    claim: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # (result, reason) pairs

    @property
    def songs(self):
        return self.register + self.link + self.claim


def fix_kind(result):
    """Which fix a song gets, or None if there's nothing we can make for it."""
    if result.status == RED and result.red_case == NOT_FOUND:
        return REGISTER
    if result.status == RED and result.red_case == NOT_LINKED:
        return LINK
    if result.status == YELLOW:
        return CLAIM
    return None


def skip_reason(result):
    if result.status == RED:
        # We don't know if it's registered under another title. Registering it
        # anyway could create a duplicate, which delays payment.
        return ("run with --ai-search first, so Retrack knows whether the song is already "
                "registered (registering it again would create a duplicate)")
    return "nothing to fix"


def plan_fixes(results, titles=None):
    """Sort the chosen songs by kind of fix. titles=None means every fixable song."""
    if titles is not None:
        wanted = {t.strip().lower() for t in titles if t.strip()}
        known = {r.row.title.lower() for r in results}
        unknown = sorted(wanted - known)
        if unknown:
            raise FixPackError(f"These titles aren't in the catalog: {', '.join(unknown)}.")
        results = [r for r in results if r.row.title.lower() in wanted]

    plan = FixPlan()
    for r in results:
        kind = fix_kind(r)
        if kind is None:
            if titles is not None or r.status == RED:  # say why, but don't list every green song
                plan.skipped.append((r, skip_reason(r)))
        else:
            getattr(plan, kind).append(r)
    return plan


def price_of(result):
    """What fixing one song costs. Deliberately ignores the verdict."""
    return FIX_PRICE_USD


def total_price(songs):
    return sum(price_of(r) for r in songs)


def price_line(songs):
    n = len(songs)
    return (f"{n} {'song' if n == 1 else 'songs'} × ${FIX_PRICE_USD:,.2f} = "
            f"${total_price(songs):,.2f} (same price for every verdict; no payment is taken yet)")


# ---- Attestation -----------------------------------------------------------------

@dataclass(frozen=True)
class Attestation:
    statement: str
    confirmed_at: str  # UTC, ISO 8601
    method: str        # "typed yes at the prompt" or "--i-confirm flag"


def confirm(method, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return Attestation(ATTESTATION_TEXT, now.isoformat(timespec="seconds"), method)


def ask_for_attestation(input_fn=input, print_fn=print):
    """Ask in the terminal. Returns an Attestation, or None if the user didn't type yes.

    input_fn and print_fn are passed in so tests can fake the keyboard.
    """
    print_fn("\nBefore Retrack writes your fix pack, please confirm:")
    print_fn(f"  {ATTESTATION_TEXT}")
    try:
        answer = input_fn('Type "yes" to confirm: ')
    except EOFError:  # no keyboard (e.g. run from a script)
        return None
    return confirm("typed yes at the prompt") if answer.strip().lower() == "yes" else None


# ---- Reading the user's MLC template -----------------------------------------------
#
# We don't hard-code column letters: we read the header row of the user's own
# template and work out each column from its name. If The MLC changes the
# template, the user's newer copy still works.

MUST = "MUST"  # an ownership column we never fill in


def classify_header(value):
    """A template column header -> what we put there: a field name, MUST, or None (leave alone)."""
    if not isinstance(value, str):
        return None
    h = " ".join(re.sub(r"[^a-z0-9]", " ", value.lower()).split())
    words = h.split()
    if not h or "aka" in words or "admin" in words or "administrator" in h:
        return None  # optional columns: the user's call, so we leave them unmarked
    if "song code" in h:
        return "song_code"
    if "isrc" in words:
        return "isrc"
    if "iswc" in words:
        return "iswc"
    if "recording" in words or "artist" in words:
        if "title" in words:
            return "recording_title"
        return "recording_artist" if "artist" in words else None
    if any(k in h for k in ("share", "publisher", "ipi", "role")):
        return MUST  # ownership: never guessed
    if "writer" in h:
        if "first" in words:
            return "writer_first"
        if "last" in words or "surname" in words:
            return "writer_last"
        if "middle" in words:
            return None
        return "writer_name" if "name" in words else None
    if "title" in words:
        return "work_title"
    return None


@dataclass
class TemplateLayout:
    sheet_name: str
    header_row: int
    columns: dict  # field name -> column number
    must_fill: list  # column numbers for ownership fields


def read_layout(workbook):
    """Find the sheet and row holding the column headers."""
    for ws in workbook.worksheets:
        for row in ws.iter_rows(min_row=1, max_row=min(20, ws.max_row)):
            columns, must_fill = {}, []
            for cell in row:
                kind = classify_header(cell.value)
                if kind == MUST:
                    must_fill.append(cell.column)
                elif kind and kind not in columns:
                    columns[kind] = cell.column
            if "work_title" in columns and ({"writer_last", "writer_name"} & columns.keys()):
                return TemplateLayout(ws.title, row[0].row, columns, must_fill)
    raise FixPackError("Retrack couldn't find the column headers (work title, writer names) in "
                       "that template. Make sure it's the Bulk Work Registration template "
                       "from your MLC Portal, unchanged.")


def open_template(path):
    """Open the user's template (read only: we save the copy elsewhere)."""
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise FixPackError(f"{path.name} isn't an .xlsx file. The MLC's template is an Excel .xlsx file.")
    if not path.is_file():
        raise FixPackError(f"Can't find the template at {path}.")
    try:
        from openpyxl import load_workbook  # imported here so the scan works without openpyxl
    except ImportError:
        raise FixPackError("Filling the template needs openpyxl: pip install openpyxl")
    try:
        workbook = load_workbook(path)
    except Exception as e:
        raise FixPackError(f"Couldn't open {path.name} as an Excel file ({type(e).__name__}).")
    return workbook, read_layout(workbook)


def check_template(path):
    """Fail early (before the user confirms anything) if the template won't work."""
    open_template(path)


# ---- What goes in a registration row ------------------------------------------------

def first_song_code(result):
    codes = [c.strip() for c in result.song_code.split(",") if c.strip()]
    return codes[0] if codes else ""


def registration_rows(result):
    """One dict per writer, as The MLC wants it: the work's details on the first
    row only, then one row per extra writer.

    Only facts from the catalog and The MLC. Ownership fields (shares, IPIs,
    role codes, publishers) are never in here: they're left for the user.
    """
    row = result.row
    writers = row.writers or [""]
    out = []
    for i, name in enumerate(writers):
        first, last = split_name(name)
        values = {"writer_first": first, "writer_last": last, "writer_name": name}
        if i == 0:
            values.update(work_title=row.title, song_code=first_song_code(result), iswc=result.iswc,
                          recording_title=row.title, recording_artist=row.artist, isrc=row.isrc)
        out.append(values)
    return out


# ---- Writing the files ---------------------------------------------------------------

def _preamble(*extra):
    return [COMPLIANCE_TEXT, NEVER_TEXT, *extra]


def write_template_copy(template_path, songs, out_path):
    """Fill a copy of the user's own template. The original file is never changed."""
    from openpyxl.comments import Comment
    from openpyxl.styles import PatternFill

    workbook, layout = open_template(template_path)
    ws = workbook[layout.sheet_name]
    highlight = PatternFill(fill_type="solid", start_color="FFF2CC", end_color="FFF2CC")

    # Start below the headers, after anything already in the sheet.
    r = layout.header_row + 1
    while any(c.value not in (None, "") for c in ws[r]):
        r += 1

    rows_written = 0
    for result in songs:
        for values in registration_rows(result):
            for name, col in layout.columns.items():
                if values.get(name):
                    ws.cell(row=r, column=col, value=values[name])
            must = list(layout.must_fill)
            if not values["writer_name"]:  # no writer in the catalog: the user must add one
                must += [layout.columns[k] for k in ("writer_first", "writer_last", "writer_name")
                         if k in layout.columns]
            for col in must:
                cell = ws.cell(row=r, column=col)
                cell.value = None
                cell.fill = highlight
                cell.comment = Comment(f"{MUST_FILL}. Retrack never guesses ownership data.", "Retrack")
            r += 1
            rows_written += 1

    # Nothing else is added: no extra tabs, no notes, no changed properties. The MLC
    # dictates this file's format, so the compliance text lives in README.md and
    # ATTESTATION.txt instead.
    workbook.save(out_path)
    return rows_written


WORKSHEET_COLUMNS = [
    # (heading, field name, or MUST for ownership fields we never fill in)
    ("Work Title", "work_title"), ("MLC Song Code", "song_code"), ("ISWC", "iswc"),
    ("Writer First Name", "writer_first"), ("Writer Last Name", "writer_last"),
    ("Writer IPI", MUST), ("Writer Role Code", MUST), ("Publisher Name", MUST),
    ("Publisher IPI", MUST), ("Collection Share", MUST),
    ("Recording Title", "recording_title"), ("Recording Artist", "recording_artist"), ("ISRC", "isrc"),
]


def write_worksheet(songs, out_path):
    """No template given: a CSV worksheet to copy from. It is NOT The MLC's template."""
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for line in _preamble("THIS IS NOT THE MLC'S TEMPLATE. It's a worksheet to copy from. " + TEMPLATE_HELP):
            w.writerow([f"# {line}"])
        w.writerow([heading for heading, _ in WORKSHEET_COLUMNS])
        rows = 0
        for result in songs:
            for values in registration_rows(result):
                out = []
                for _, name in WORKSHEET_COLUMNS:
                    if name == MUST or (name in ("writer_first", "writer_last") and not values["writer_name"]):
                        out.append(MUST_FILL)
                    else:
                        out.append(values.get(name, ""))
                w.writerow(out)
                rows += 1
    return rows


def write_match_suggestions(songs, out_path):
    """ISRC <-> MLC song code suggestions for the user to confirm, one row per song."""
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for line in _preamble("These are SUGGESTIONS. Please confirm each one before submitting it "
                              "(write yes in the Confirmed column). See README.md for the steps."):
            w.writerow([f"# {line}"])
        w.writerow(["ISRC", "MLC Song Code", "Your Title", "MLC Title", "Your Writers", "MLC Writers",
                    "Confidence", "Confirmed (yes/no)", "Notes"])
        for r in songs:
            c = r.suggested_match
            notes = ""
            if r.other_matches:
                notes = "Also possible: " + ", ".join(o.song_code for o in r.other_matches)
            w.writerow([r.row.isrc, c.song_code, r.row.title, c.title, "; ".join(r.row.writers),
                        "; ".join(c.writers), f"{c.confidence:.0%}", "", notes])


def bulk_match_name(today=None):
    today = today or datetime.date.today()
    return FILES["bulk_match"].format(date=f"{today:%Y%m%d}")


def write_bulk_match(songs, out_path):
    """The MLC's Bulk Match Submission file, in exactly their format: UTF-8 CSV,
    header "ISRC,MLC Song Code", no blank rows, no duplicate ISRCs. Nothing else
    goes in it (no notes, no compliance text): The MLC dictates this format."""
    seen = set()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["ISRC", "MLC Song Code"])
        for r in songs:
            isrc, code = r.row.isrc, r.suggested_match.song_code
            if isrc and code and isrc not in seen:
                seen.add(isrc)
                w.writerow([isrc, code])


def _claim_section(r, already_registered=False):
    lines = [f"## {r.row.title} ({r.row.artist})", ""]
    lines.append(f"- ISRC: `{r.row.isrc}`")
    if r.song_code:
        lines.append(f"- MLC song code: `{r.song_code}`")
    if already_registered:
        lines += ["", f"Already registered at The MLC as song code `{first_song_code(r)}`. "
                      "Don't register it again (that creates a duplicate). Claim it instead.", ""]
    else:
        lines.append(f"- Share claimed at The MLC now: {r.claimed_share:g}%")
        if r.mlc_writers:
            lines.append(f"- Writers at The MLC: {', '.join(r.mlc_writers)}")
        if r.missing_writers:
            lines.append(f"- Your writers missing from that record: {', '.join(r.missing_writers)}")
        lines += ["", "What we found:"] + [f"- {p}" for p in r.problems] + [""]
    lines.append("Steps:")
    lines.append("- [ ] Log in to The MLC Portal yourself (Retrack never logs in for you).")
    if r.song_code:
        lines.append(f"- [ ] Open song code {first_song_code(r)} and check the title and writers.")
    for fix in r.fixes:
        lines.append(f"- [ ] {fix}")
    lines.append(f"- [ ] Your share to claim: ______ % ({MUST_FILL}: Retrack can't know your share)")
    for w in r.missing_writers:
        lines.append(f"- [ ] IPI for {w}: ______ ({MUST_FILL})")
    return "\n".join(lines) + "\n"


def write_claim_checklist(songs, out_path, already_registered=()):
    parts = ["# Claim checklist", ""]
    parts += [f"> {line}" for line in _preamble()]
    parts.append("")
    for r in songs:
        parts.append(_claim_section(r))
    for r in already_registered:
        parts.append(_claim_section(r, already_registered=True))
    Path(out_path).write_text("\n".join(parts), encoding="utf-8")


def write_attestation(attestation, songs, out_path):
    lines = [
        "Retrack fix pack: confirmation",
        "",
        f"Confirmed at (UTC): {attestation.confirmed_at}",
        f"How: {attestation.method}",
        f"Statement: {attestation.statement}",
        "",
        "Songs in this fix pack:",
        *[f"  - {r.row.title} ({r.row.isrc})" for r in songs],
        "",
        COMPLIANCE_TEXT,
        NEVER_TEXT,
    ]
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_readme(plan, written, attestation, out_path, rows=0, used_template=False, already_registered=()):
    lines = ["# Your Retrack fix pack", ""]
    lines += [f"> {line}" for line in _preamble()]
    lines += ["", f"Songs: {price_line(plan.songs)}.", "",
              f"Confirmed {attestation.confirmed_at} UTC ({attestation.method}). See `ATTESTATION.txt`.", ""]

    if FILES["xlsx"] in written:
        lines += [f"## `{FILES['xlsx']}`: register songs that aren't at The MLC", "",
                  "A filled-in copy of the Bulk Work Registration template you downloaded from your "
                  "MLC Portal. Your original template is unchanged.", "",
                  f"1. Open it and fill in every **yellow cell** ({MUST_FILL}): writer IPIs, role codes, "
                  "publisher names and IPIs, and collection shares. Retrack never guesses these.",
                  "2. Enter publishing information only for shares you control.",
                  "3. Don't move or delete any columns. Delete any example rows the template came with.",
                  "4. Log in to your MLC Portal, go to Works Registration, then Bulk Registration Tool, "
                  "and upload the file (it must stay an .xlsx file).", "",
                  "Retrack only added rows: the template's tabs, columns and settings are exactly as "
                  "The MLC made them.", "",
                  "Self-administered songwriters without a publisher IPI can't use bulk upload: register "
                  "those songs one at a time in the Portal instead.", ""]
        if rows > MAX_TEMPLATE_ROWS:
            lines += [f"**This file has {rows} rows; The MLC accepts up to {MAX_TEMPLATE_ROWS} per upload.** "
                      "Split it into several files.", ""]
    if FILES["worksheet"] in written:
        lines += [f"## `{FILES['worksheet']}`: register songs that aren't at The MLC", "",
                  "**This is not The MLC's template.** It's a worksheet listing what Retrack knows, "
                  "to copy into the official template. " + TEMPLATE_HELP, "",
                  f"Every \"{MUST_FILL}\" cell is ownership data Retrack never guesses: writer IPIs, "
                  "role codes, publishers and collection shares.", ""]
    if FILES["matches"] in written:
        bulk = next(n for n in written if n.startswith("BulkMatchSuggestions_"))
        lines += [f"## `{FILES['matches']}` and `{bulk}`: link recordings to songs already at The MLC", "",
                  f"`{FILES['matches']}` is for you to review. Each row suggests that your recording "
                  "(ISRC) belongs to a song that's already registered (MLC song code), with both "
                  "titles, both writer lists and how sure Retrack is. These are suggestions: "
                  "**please confirm each one**.", "",
                  "A few songs: use the **Matching Tool** in your MLC Portal. Search for the "
                  "recording and propose the match to the song code.", "",
                  f"Many songs: `{bulk}` is ready for The MLC's **Bulk Match Submission Process** "
                  "(exactly their format: `ISRC,MLC Song Code`).",
                  "1. Delete every row you didn't confirm. Don't leave blank rows.",
                  f"2. Add your member name to the front of the file name: "
                  f"`YourMemberName_{bulk}`.",
                  "3. Email it to bulk.matching@themlc.com. The MLC says it takes 30 to 90 days, and "
                  "sends monthly files with each suggestion's status.", ""]
    if FILES["checklist"] in written:
        lines += [f"## `{FILES['checklist']}`: claim shares and fix writers", "",
                  "One section per song with what's wrong at The MLC and a checklist of steps to do "
                  "in your MLC Portal.", ""]
        if already_registered:
            lines += ["Some songs you chose to register already have an MLC song code, and your "
                      "template has no song code column, so they're in the checklist (claim them) "
                      "instead of the template (which would create duplicates).", ""]
    if plan.skipped:
        lines += ["## Not included", ""] + [f"- {r.row.title}: {why}" for r, why in plan.skipped] + [""]
    lines += ["## The MLC's own instructions", ""] + [f"- [{t}]({u})" for t, u in MLC_HELP_LINKS] + [""]
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


def build_fix_pack(plan, out_dir, attestation, template_path=None):
    """Write the fix pack. Returns the list of file names written.

    Refuses (and writes nothing) without an Attestation.
    """
    if not isinstance(attestation, Attestation) or attestation.statement != ATTESTATION_TEXT:
        raise FixPackError("No confirmation, no files: confirm you reviewed the data and control "
                           "the shares (answer the prompt, or pass --i-confirm).")
    if not plan.songs:
        raise FixPackError("None of the chosen songs has anything Retrack can make a fix for.")

    register, already_registered = list(plan.register), []
    if template_path is not None:
        workbook, layout = open_template(template_path)  # check it before writing anything
        if "song_code" not in layout.columns:
            # A work exists but the template can't say so: registering it would duplicate it.
            already_registered = [r for r in register if first_song_code(r)]
            register = [r for r in register if not first_song_code(r)]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written, rows = [], 0

    if register:
        if template_path is not None:
            rows = write_template_copy(template_path, register, out_dir / FILES["xlsx"])
            written.append(FILES["xlsx"])
        else:
            rows = write_worksheet(register, out_dir / FILES["worksheet"])
            written.append(FILES["worksheet"])
    if plan.link:
        write_match_suggestions(plan.link, out_dir / FILES["matches"])
        written.append(FILES["matches"])
        bulk = bulk_match_name()
        write_bulk_match(plan.link, out_dir / bulk)
        written.append(bulk)
    if plan.claim or already_registered:
        write_claim_checklist(plan.claim, out_dir / FILES["checklist"], already_registered)
        written.append(FILES["checklist"])

    write_attestation(attestation, plan.songs, out_dir / FILES["attestation"])
    written.append(FILES["attestation"])
    write_readme(plan, written, attestation, out_dir / FILES["readme"], rows,
                 template_path is not None, already_registered)
    written.append(FILES["readme"])
    return written
