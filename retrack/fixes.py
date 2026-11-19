"""The fix pack: files that help the user fix the songs they choose (the paid part).

The scan is free. Fixing costs a flat FIX_PRICE_USD per song, the same for every
verdict. The web UI also sells two optional add-ons (AI search, Smart names) at a flat
ADDON_PRICE_USD each per order, never per song; they never change the per-song price.
The web UI takes payment through Stripe (payments.py); the CLI takes none.

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

from .checker import NOT_FOUND, NOT_LINKED, RED, YELLOW, needs_ai_after_checkout
from .compliance import ATTESTATION_TEXT, COMPLIANCE_TEXT, NEVER_TEXT
from .mlc_client import split_name

# Placeholder price, in US dollars, per fixed song. The same for every verdict:
# don't add per-verdict prices (tests/test_fixes.py checks this).
FIX_PRICE_USD = 5.00

# Web UI only: the optional add-ons. Each is a flat price per order (not per song),
# opt-in, and never changes the per-song price. They run only after checkout, on the
# paid songs they apply to (checker.needs_ai_after_checkout).
ADDON_PRICE_USD = 2.00
AI_SEARCH, SMART_NAMES = "ai_search", "smart_names"
ADDONS = (AI_SEARCH, SMART_NAMES)  # in the order they're listed and charged
ADDON_LABELS = {AI_SEARCH: "AI search", SMART_NAMES: "Smart names"}

REGISTER, LINK, CLAIM = "register", "link", "claim"
# Web UI only. UNRESOLVED: a red song not placed yet (Register or Link?). It can be paid
# for; after checkout AI search works it out, and if it can't, the user checks by hand.
# ALL_SET: a paid song that a closer look (smart names) found nothing wrong with.
UNRESOLVED, ALL_SET = "unresolved", "all_set"

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
    unresolved: list = field(default_factory=list)  # web UI only, see UNRESOLVED
    all_set: list = field(default_factory=list)     # web UI only, see ALL_SET

    @property
    def songs(self):
        return self.register + self.link + self.claim + self.unresolved + self.all_set

    def by_kind(self):
        """(kind, songs) for every kind, in the order the pages show them."""
        return [(REGISTER, self.register), (LINK, self.link), (CLAIM, self.claim),
                (UNRESOLVED, self.unresolved), (ALL_SET, self.all_set)]


def fix_kind(result):
    """Which fix a song gets, or None if there's nothing we can make for it."""
    if result.status == RED and result.red_case == NOT_FOUND:
        return REGISTER
    if result.status == RED and result.red_case == NOT_LINKED:
        return LINK
    if result.status == YELLOW:
        return CLAIM
    return None


def sold_kind(result):
    """What the web UI sells for a song: its fix, or UNRESOLVED for a red song that
    isn't placed yet (worked out after checkout). None: nothing to sell."""
    kind = fix_kind(result)
    if kind is None and result.status == RED:
        return UNRESOLVED
    return kind


def skip_reason(result):
    if result.status == RED:
        # We don't know if it's registered under another title. Registering it
        # anyway could create a duplicate, which delays payment.
        return ("run with --ai-search first, so Retrack knows whether the song is already "
                "registered (registering it again would create a duplicate)")
    return "nothing to fix"


def plan_fixes(results, titles=None, sell_red=False):
    """Sort the chosen songs by kind of fix. titles=None means every fixable song.

    sell_red=True is the web UI: a red song that isn't placed yet goes in
    plan.unresolved instead of being skipped, and a song with nothing to fix in
    plan.all_set (it was paid for, so it's still shown). The command line leaves
    it False: there, red songs without --ai-search are skipped, never registered.
    """
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
        if kind is None and sell_red:
            (plan.unresolved if r.status == RED else plan.all_set).append(r)
        elif kind is None:
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


def addon_applies(addon, result):
    """Does this add-on do anything for this song? AI search: an unplaced red song.
    Smart names: a song with writers missing from its MLC record."""
    return needs_ai_after_checkout(result, smart_names=addon == SMART_NAMES, ai_search=addon == AI_SEARCH)


def addons_offered(songs):
    """The add-ons worth buying for these songs: each one only if a song needs it."""
    return [a for a in ADDONS if any(addon_applies(a, r) for r in songs)]


def order_total(songs, addons=()):
    """Songs at the flat per-song price, plus each add-on bought at its flat price.

    `addons` lists add-on purchases: one entry per order that bought it (an order never
    lists the same add-on twice, so within one order each is charged once)."""
    return total_price(songs) + ADDON_PRICE_USD * len(addons)


def price_parts(songs, addons=()):
    """The price, as its parts, one per line on a page:
    ["4 songs × $5.00 = $20.00", "AI search $2.00", "Smart names $2.00", "Total $24.00"].
    With no add-ons it's just the songs part, which is already the total."""
    n = len(songs)
    parts = [f"{n} {'song' if n == 1 else 'songs'} × ${FIX_PRICE_USD:,.2f} = ${total_price(songs):,.2f}"]
    for a in ADDONS:
        k = list(addons).count(a)
        if k == 1:
            parts.append(f"{ADDON_LABELS[a]} ${ADDON_PRICE_USD:,.2f}")
        elif k > 1:  # bought with more than one order this visit (fix page: what was paid)
            parts.append(f"{ADDON_LABELS[a]}, {k} orders × ${ADDON_PRICE_USD:,.2f} = ${k * ADDON_PRICE_USD:,.2f}")
    if addons:
        parts.append(f"Total ${order_total(songs, addons):,.2f}")
    return parts


def price_line(songs, addons=()):
    """The same, as one line of text, for the command line and the files:
    "4 songs × $5.00 = $20.00, AI search $2.00, Smart names $2.00, Total $24.00"."""
    return ", ".join(price_parts(songs, addons))


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


def split_for_template(register, layout):
    """(songs to put in the template, songs that must be claimed instead).

    A song with a known MLC song code already has a work. If the template has no
    song code column it can't say so, and registering it would create a duplicate.
    """
    if "song_code" in layout.columns:
        return list(register), []
    return ([r for r in register if not first_song_code(r)],
            [r for r in register if first_song_code(r)])


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
        # The header comes first, with no note lines above it, so spreadsheet apps and
        # scripts read it cleanly. What the file is (and isn't) is explained in README.md.
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
        w.writerow(LINK_HEADERS)  # header first; "please confirm each row" is in README.md
        for r in songs:
            w.writerow(link_row(r))


LINK_HEADERS = ["ISRC", "MLC Song Code", "Your Title", "MLC Title", "Your Writers", "MLC Writers",
                "Confidence", "Confirmed (yes/no)", "Notes"]


def link_row(r):
    c = r.suggested_match
    notes = "Also possible: " + ", ".join(o.song_code for o in r.other_matches) if r.other_matches else ""
    return [r.row.isrc, c.song_code, r.row.title, c.title, "; ".join(r.row.writers),
            "; ".join(c.writers), f"{c.confidence:.0%}", "", notes]


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


# ---- The web fix pack: one workbook -------------------------------------------------------

CLAIM_HEADERS = ["Song", "Artist", "ISRC", "MLC Song Code", "Share claimed now", "Writers at The MLC",
                 "Your writers missing there", "What to do", "Your share to claim (%)",
                 "IPIs of the missing writers"]
CLAIM_MUST = ("Your share to claim (%)", "IPIs of the missing writers")


def fixes_workbook_name(today=None):
    today = today or datetime.date.today()
    return f"Retrack_Fixes_{today:%Y%m%d}.xlsx"


def claim_row(r):
    return {"Song": r.row.title, "Artist": r.row.artist, "ISRC": r.row.isrc, "MLC Song Code": r.song_code,
            "Share claimed now": f"{r.claimed_share:g}%", "Writers at The MLC": ", ".join(r.mlc_writers),
            "Your writers missing there": ", ".join(r.missing_writers), "What to do": " ".join(r.fixes)}


def write_fixes_workbook(plan, attestation, out_path, addons=()):
    """Everything for the paid songs in one .xlsx: Summary, Register, Link, Claim, Confirmation.

    Every data tab has its headers on row 1, with nothing above them. Ownership
    cells (shares, IPIs, role codes, publishers) are left blank, highlighted and
    marked "YOU MUST FILL THIS IN"; Retrack never fills them.
    """
    if not isinstance(attestation, Attestation) or attestation.statement != ATTESTATION_TEXT:
        raise FixPackError("No confirmation, no files.")
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Font, PatternFill

    highlight = PatternFill(fill_type="solid", start_color="FFF2CC", end_color="FFF2CC")
    bold = Font(bold=True)

    def sheet(title, headers, wb):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.font = bold
        ws.freeze_panes = "A2"
        for i, h in enumerate(headers, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(14, min(40, len(h) + 4))
        return ws

    def must_fill(cell):
        cell.value = None
        cell.fill = highlight
        cell.comment = Comment(f"{MUST_FILL}. Retrack never guesses ownership data.", "Retrack")

    wb = Workbook()
    wb.remove(wb.active)

    summary = sheet("Summary", ["Song", "Artist", "ISRC", "Fix", "What you get", "Price (USD)"], wb)
    what = {REGISTER: ("Register", "Rows for The MLC’s Bulk Work Registration (Register tab)"),
            LINK: ("Link", "A match to confirm and submit (Link tab)"),
            CLAIM: ("Claim", "Steps to claim in The MLC Portal (Claim tab)"),
            UNRESOLVED: ("Check by hand", "Search The MLC by title and writers yourself (steps on the fix page)"),
            ALL_SET: ("Nothing to fix", "A closer look found nothing wrong")}
    for kind, songs in plan.by_kind():
        for r in songs:
            summary.append([r.row.title, r.row.artist, r.row.isrc, *what[kind], price_of(r)])
    summary.append([])
    summary.append(["Total", price_line(plan.songs, addons)])  # addons: the paid add-ons (web UI)
    summary.append([])
    summary.append([COMPLIANCE_TEXT])
    summary.append([NEVER_TEXT])

    register = sheet("Register", [h for h, _ in WORKSHEET_COLUMNS], wb)
    for r in plan.register:
        for values in registration_rows(r):
            register.append([None if name == MUST else values.get(name, "") for _, name in WORKSHEET_COLUMNS])
            row = register.max_row
            for col, (_, name) in enumerate(WORKSHEET_COLUMNS, start=1):
                no_writer = name in ("writer_first", "writer_last") and not values["writer_name"]
                if name == MUST or no_writer:
                    must_fill(register.cell(row=row, column=col))

    link = sheet("Link", LINK_HEADERS, wb)
    for r in plan.link:
        link.append(link_row(r))

    claim = sheet("Claim", CLAIM_HEADERS, wb)
    for r in plan.claim:
        values = claim_row(r)
        claim.append([values.get(h) for h in CLAIM_HEADERS])
        row = claim.max_row
        for h in CLAIM_MUST:
            if h == "IPIs of the missing writers" and not r.missing_writers:
                continue
            must_fill(claim.cell(row=row, column=CLAIM_HEADERS.index(h) + 1))

    confirmation = sheet("Confirmation", ["Field", "Value"], wb)
    for field_name, value in [("Statement", attestation.statement),
                              ("Confirmed at (UTC)", attestation.confirmed_at),
                              ("How", attestation.method),
                              ("Songs", "; ".join(r.row.title for r in plan.songs)),
                              ("Disclaimer", COMPLIANCE_TEXT)]:
        confirmation.append([field_name, value])

    wb.save(out_path)


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
        register, already_registered = split_for_template(register, layout)

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
