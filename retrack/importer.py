"""Catalog import: turn whatever spreadsheet a label already has into Retrack's CSV.

    python -m retrack import messy_file.xlsx --out catalog.csv                  # offline rules (default)
    python -m retrack import messy_file.xlsx --out catalog.csv --smart-import   # Claude reads the columns
    python -m retrack import export.csv --out catalog.csv --yes                 # scripts: no questions

How it works, in two steps:
  (a) Map the columns. A "column mapper" looks at the header row and up to
      SAMPLE_ROWS sample rows and says which column is the title, artist,
      ISRC and writers, with a confidence for each. The user sees the
      mapping and confirms or corrects it before anything is converted.
  (b) Convert. Plain Python reads every row using that mapping and applies
      the cleanup rules below. The AI never sees or rewrites the rest of the
      data: it only ever gets the header row and the sample rows.

There are two mappers with the same method, map_columns(headers, samples):
  HeaderRuleMapper   - offline rules based on header names. The default, like
                       every AI feature in Retrack being opt-in. Also the
                       fallback if the Claude API doesn't answer.
  ClaudeColumnMapper - asks the Claude API (opt-in with --smart-import; needs an
                       Anthropic API key). --mock keeps it offline.

Cleanup rules (all plain Python, all in this file):
  - ISRCs: dashes, spaces and dots removed, upper-cased. Anything that isn't
    12 characters shaped country(2 letters) + registrant(3) + year(2 digits)
    + number(5 digits) is flagged and the row isn't imported.
  - Writers: split on ";", "/", "&", " and " and new lines. Commas only when
    every piece is clearly a full name ("John Smith, Ana Lopez"). Anything
    like "Smith, John" is left as it is and flagged.
  - Duplicates: a row that's exactly the same as an earlier one (after
    mapping) is dropped and noted.
  - Rows with no ISRC are flagged and not imported; rows with no writers are
    imported and flagged. Nothing is dropped silently.
"""

import argparse
import csv
import datetime
import io
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .checker import normalize_isrc
from .name_matcher import MODEL

FIELDS = ("title", "artist", "isrc", "writers")
REQUIRED = ("title", "isrc")  # without these, there's no catalog to check
SAMPLE_ROWS = 5  # the most data rows the AI ever sees
HEADER_SEARCH_ROWS = 30  # look for the header row in the first 30 rows
LOW_CONFIDENCE = 0.6  # below this, the mapping line says "check this"

ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{2}[0-9]{5}$")


class CatalogImportError(Exception):
    """Something stops the import. Nothing has been written."""


# ---- Reading the file ------------------------------------------------------------

def _cell_text(value):
    """A spreadsheet value -> clean text. 2024.0 -> '2024', dates -> '2024-03-01'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.date().isoformat() if isinstance(value, datetime.datetime) else value.isoformat()
    return str(value).strip()


def _decode(data):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _pick_delimiter(text):
    """Comma, semicolon or tab: whichever splits the first lines most consistently.

    (Excel in many European countries saves "CSV" with semicolons.)
    """
    lines = [l for l in text.splitlines()[:30] if l.strip()]
    best, best_score = ",", -1.0
    for delim in (",", ";", "\t"):
        counts = [len(row) for row in csv.reader(lines, delimiter=delim)]
        if not counts:
            continue
        mode, freq = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        score = freq / len(counts) + mode / 1000  # consistency first, then more columns
        if score > best_score:
            best, best_score = delim, score
    return best


def read_csv_rows(path):
    text = _decode(Path(path).read_bytes())
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=_pick_delimiter(text))]


def read_xlsx_rows(path):
    """All rows of the busiest sheet, with merged cells filled in.

    A merged cell only stores its value in the top-left corner. Copying that
    value into every cell of the merge is what a person reading the sheet sees
    (e.g. an artist name merged down over three songs).
    """
    try:
        from openpyxl import load_workbook
    except ModuleNotFoundError:
        raise CatalogImportError("Reading .xlsx files needs openpyxl: pip install openpyxl")
    try:
        wb = load_workbook(path, data_only=True)  # data_only: formulas -> their values
    except Exception as e:
        raise CatalogImportError(f"Couldn't open {Path(path).name} as an Excel file ({type(e).__name__}).")

    def filled(ws):
        return sum(1 for row in ws.iter_rows(values_only=True) for v in row if v not in (None, ""))

    ws = max(wb.worksheets, key=filled)
    grid = [[_cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
    for merged in ws.merged_cells.ranges:
        value = _cell_text(ws.cell(row=merged.min_row, column=merged.min_col).value)
        for r in range(merged.min_row, merged.max_row + 1):
            for c in range(merged.min_col, merged.max_col + 1):
                grid[r - 1][c - 1] = value
    return grid


def read_rows(path):
    path = Path(path)
    if not path.is_file():
        raise CatalogImportError(f"Can't find {path}.")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows = read_csv_rows(path)
    elif suffix == ".xlsx":
        rows = read_xlsx_rows(path)
    else:
        raise CatalogImportError(f"{path.name}: Retrack can import .csv and .xlsx files. "
                           "Save other formats (.xls, .numbers, Google Sheets) as one of those first.")
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


# ---- Finding the header row ---------------------------------------------------------

HEADER_WORDS = ("title", "song", "track", "artist", "performer", "isrc", "writer", "composer",
                "author", "lyricist", "album", "release", "upc", "label", "name")


def _is_blank(row):
    return not any(c.strip() for c in row)


def find_header_row(rows):
    """The index of the header row: the row in the first 30 with the most text
    cells, with a bonus for header-like words. Ties go to the earlier row.

    Title rows ("Sunbeam Records catalog") lose because they have one cell;
    data rows lose because they don't say "title" or "ISRC".
    """
    best, best_score = None, 0
    for i, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        cells = [c.lower() for c in row if c.strip()]
        if len(set(cells)) < 2:
            continue
        text_cells = sum(1 for c in set(cells) if re.search(r"[a-z]", c) and not ISRC_RE.match(c.upper()))
        keywords = sum(1 for c in set(cells) if any(w in c for w in HEADER_WORDS))
        score = text_cells + 2 * keywords
        if score > best_score:
            best, best_score = i, score
    if best is None:
        raise CatalogImportError("Couldn't find a header row (a row naming the columns) in the first "
                           f"{HEADER_SEARCH_ROWS} rows.")
    return best


def column_letter(index):
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA' (spreadsheet column names)."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def column_index(letter):
    n = 0
    for ch in letter.strip().upper():
        if not "A" <= ch <= "Z":
            return None
        n = n * 26 + ord(ch) - 64
    return n - 1 if n else None


@dataclass
class Table:
    headers: list       # header text per column ("" if the header cell is empty)
    rows: list          # (spreadsheet row number, cells) for each row below the header
    header_row: int     # spreadsheet row number of the header (1-based)


def read_table(path):
    rows = read_rows(path)
    h = find_header_row(rows)
    data = [(i + 1, r) for i, r in enumerate(rows) if i > h]
    return Table(headers=rows[h], rows=data, header_row=h + 1)


def sample_rows(table, n=SAMPLE_ROWS):
    """The first n non-blank rows under the header: the only data the AI may see."""
    return [cells for _, cells in table.rows if not _is_blank(cells)][:n]


# ---- The column mapping -----------------------------------------------------------

@dataclass
class FieldMap:
    columns: list = field(default_factory=list)  # 0-based column numbers; empty = no column
    confidence: float = 0.0


def empty_mapping():
    return {f: FieldMap() for f in FIELDS}


def clean_mapping(mapping, n_columns):
    """Drop impossible answers: unknown fields, columns that don't exist, more than
    one column for anything but writers, a column used twice. Clamp confidence."""
    out, used = empty_mapping(), set()
    for f in FIELDS:
        fm = mapping.get(f) or FieldMap()
        cols = []
        for c in fm.columns:
            if isinstance(c, int) and 0 <= c < n_columns and c not in used and c not in cols:
                cols.append(c)
        if f != "writers":
            cols = cols[:1]
        used.update(cols)
        out[f] = FieldMap(cols, min(max(float(fm.confidence), 0.0), 1.0) if cols else 0.0)
    return out


def _norm_header(h):
    return " ".join(re.sub(r"[^a-z0-9]", " ", h.lower()).split())


class HeaderRuleMapper:
    """Offline mapper: matches header names against known words, and spots an ISRC
    column from its sample values if no header says "ISRC"."""

    # field -> (words that must all start a word in the header, confidence). Best score wins.
    RULES = {
        "isrc": [(("isrc",), 0.95)],
        "title": [(("track", "title"), 0.95), (("song", "title"), 0.95), (("song", "name"), 0.9),
                  (("track", "name"), 0.9), (("title",), 0.85), (("song",), 0.7), (("track",), 0.6)],
        "artist": [(("primary", "artist"), 0.95), (("artist",), 0.9), (("performer",), 0.8)],
        "writers": [(("songwriter",), 0.95), (("writer",), 0.9), (("composer",), 0.9),
                    (("author",), 0.8), (("lyricist",), 0.8)],
    }
    # A header with one of these words is never that field: "Release Title" isn't
    # the song's title, "Songwriter Royalties Withheld" isn't a writer.
    NOT = {
        "title": ("album", "release", "version", "id", "code", "type"),
        "artist": ("featured", "feat", "label", "id"),
        "writers": ("royalt", "withheld", "share", "ipi", "id", "publisher"),
        "isrc": (),
    }

    def score(self, f, header):
        words = _norm_header(header).split()
        if any(w.startswith(bad) for w in words for bad in self.NOT[f]):
            return 0.0
        return max((conf for needed, conf in self.RULES[f]
                    if all(any(w.startswith(n) for w in words) for n in needed)), default=0.0)

    def map_columns(self, headers, samples):
        mapping = empty_mapping()
        for f in FIELDS:
            scores = [self.score(f, h) for h in headers]
            best = max(scores, default=0.0)
            if best == 0:
                continue
            if f == "writers":  # "Writer 1", "Writer 2"...: every column as good as the best
                mapping[f] = FieldMap([c for c, sc in enumerate(scores) if sc == best], best)
            else:
                mapping[f] = FieldMap([scores.index(best)], best)
        if not mapping["isrc"].columns:  # no "ISRC" header: look at the values instead
            for col in range(len(headers)):
                values = [s[col] for s in samples if col < len(s) and s[col].strip()]
                if values and all(ISRC_RE.match(normalize_isrc(v)) for v in values):
                    mapping["isrc"] = FieldMap([col], 0.7)
                    break
        return clean_mapping(mapping, len(headers))


# ---- Claude version ---------------------------------------------------------------

MAPPING_PROMPT = """You map the columns of a music catalog spreadsheet to four fields:
- title: the song or track title (not the album or release title)
- artist: the main recording artist (not featured artists, not the label)
- isrc: the recording's ISRC code
- writers: the songwriters or composers. This can be several columns (Writer 1, Writer 2...)

You get each column's letter, its header and a few sample values. Answer with the \
column letters. If no column fits a field, use "" (or [] for writers); never force a \
match. confidence is how sure you are, from 0 to 1. Spreadsheets are messy: headers \
can be vague, misspelled or in other languages, so use the sample values too."""

_ONE = {
    "type": "object",
    "properties": {"column": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["column", "confidence"],
    "additionalProperties": False,
}
MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": _ONE, "artist": _ONE, "isrc": _ONE,
        "writers": {
            "type": "object",
            "properties": {"columns": {"type": "array", "items": {"type": "string"}},
                           "confidence": {"type": "number"}},
            "required": ["columns", "confidence"],
            "additionalProperties": False,
        },
    },
    "required": list(FIELDS),
    "additionalProperties": False,
}


def mapping_request_text(headers, samples):
    """Exactly what Claude is sent: header row + sample rows, column by column."""
    columns = [{"letter": column_letter(i), "header": h,
                "samples": [s[i] for s in samples[:SAMPLE_ROWS] if i < len(s)]}
               for i, h in enumerate(headers)]
    return json.dumps({"columns": columns}, ensure_ascii=False)


class ClaudeColumnMapper:
    def __init__(self, client=None, model=MODEL):
        if client is None:
            # Imported here so the rest of the tool (and --mock) works without
            # the anthropic package installed.
            import anthropic

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.client = client
        self.model = model
        self._cache = {}  # (headers, samples) -> mapping, so the same file costs one call

    def map_columns(self, headers, samples):
        """The mapping, or None if the AI couldn't answer (the caller falls back to rules)."""
        samples = [list(s) for s in samples[:SAMPLE_ROWS]]  # never more than SAMPLE_ROWS rows
        key = (tuple(headers), tuple(tuple(s) for s in samples))
        if key not in self._cache:
            self._cache[key] = self._ask(headers, samples)
        return self._cache[key]

    def _ask(self, headers, samples):
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                output_config={
                    "effort": "low",  # a short, well-defined question
                    "format": {"type": "json_schema", "schema": MAPPING_SCHEMA},
                },
                # If the main model declines, let the API retry on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=MAPPING_PROMPT,
                messages=[{"role": "user", "content": mapping_request_text(headers, samples)}],
            )
        except Exception:  # network down, bad key, rate limit...
            return None
        if response.stop_reason != "end_turn":  # refused or cut off
            return None
        try:
            data = json.loads(next(b.text for b in response.content if b.type == "text"))
            mapping = {}
            for f in ("title", "artist", "isrc"):
                col = column_index(data[f]["column"]) if data[f]["column"] else None
                mapping[f] = FieldMap([col] if col is not None else [], data[f]["confidence"])
            cols = [column_index(c) for c in data["writers"]["columns"] if c]
            mapping["writers"] = FieldMap([c for c in cols if c is not None], data["writers"]["confidence"])
        except (StopIteration, ValueError, KeyError, TypeError):
            return None
        return clean_mapping(mapping, len(headers))


# ---- Showing and confirming the mapping ----------------------------------------------

def _col_name(headers, col):
    h = headers[col].strip()
    return f"{h} ({column_letter(col)})" if h else f"column {column_letter(col)}"


def describe_mapping(mapping, headers):
    """'Song Name (B) → title (95%)', one line per field."""
    lines = []
    for f in FIELDS:
        fm = mapping[f]
        if not fm.columns:
            lines.append(f"(no column) → {f}" + ("   ← needed" if f in REQUIRED else ""))
            continue
        source = " + ".join(_col_name(headers, c) for c in fm.columns)
        line = f"{source} → {f} ({fm.confidence:.0%})"
        if fm.confidence < LOW_CONFIDENCE:
            line += "   ← not sure, please check"
        lines.append(line)
    return lines


def parse_column_answer(answer, headers):
    """'B' or 'B,C' or 'Song Name' -> column numbers. None if it can't be understood."""
    cols = []
    for part in [p.strip() for p in answer.split(",") if p.strip()]:
        if part.lower() in (h.strip().lower() for h in headers if h.strip()):
            cols.append(next(i for i, h in enumerate(headers) if h.strip().lower() == part.lower()))
            continue
        col = column_index(part) if re.fullmatch(r"[A-Za-z]{1,3}", part) else None
        if col is None or col >= len(headers):
            return None
        cols.append(col)
    return cols


def unclear_fields(mapping):
    """Fields the mapper found no column for, or isn't sure about."""
    return [f for f in FIELDS if not mapping[f].columns or mapping[f].confidence < LOW_CONFIDENCE]


def smart_import_hint(mapping):
    """For the offline rules only: point at --smart-import when they're unsure."""
    unclear = unclear_fields(mapping)
    if not unclear:
        return ""
    parts = [f"{f} ({'no column' if not mapping[f].columns else f'{mapping[f].confidence:.0%}'})"
             for f in unclear]
    return (f"The offline rules weren't sure about: {', '.join(parts)}. You can pick the columns "
            "yourself, or rerun with --smart-import to let Claude read the header row and the "
            f"first {SAMPLE_ROWS} rows.")


def confirm_mapping(mapping, headers, input_fn=input, print_fn=print, hint=""):
    """Show the mapping; let the user accept, correct or cancel. Returns the
    final mapping, or None if they cancelled."""
    while True:
        print_fn("\nRetrack's reading of your columns:")
        for line in describe_mapping(mapping, headers):
            print_fn(f"  {line}")
        if hint:
            print_fn(hint)
            hint = ""  # once is enough
        try:
            answer = input_fn("Use this? [y]es / [e]dit / [n]o: ").strip().lower()
        except EOFError:
            return None
        if answer in ("y", "yes"):
            return mapping
        if answer in ("n", "no"):
            return None
        if answer not in ("e", "edit"):
            continue
        print_fn("For each field, type a column letter or header (writers: several, comma-separated),")
        print_fn("'-' for none, or press Enter to keep it.")
        for f in FIELDS:
            current = ", ".join(column_letter(c) for c in mapping[f].columns) or "-"
            try:
                reply = input_fn(f"  {f} [{current}]: ").strip()
            except EOFError:
                return None
            if not reply:
                continue
            if reply == "-":
                mapping[f] = FieldMap()
                continue
            cols = parse_column_answer(reply, headers)
            if cols is None:
                print_fn(f"    Couldn't find column '{reply}', kept {current}.")
                continue
            mapping[f] = FieldMap(cols, 1.0)  # chosen by the user: fully sure
        mapping = clean_mapping(mapping, len(headers))


# ---- Converting (plain Python, every row) -------------------------------------------

@dataclass
class Problem:
    row: int        # spreadsheet row number, as the user sees it
    field: str
    value: str
    problem: str
    outcome: str    # "not imported", "imported, please check", "dropped"


@dataclass
class ImportResult:
    rows: list = field(default_factory=list)      # dicts with title, artist, isrc, writers
    problems: list = field(default_factory=list)
    rows_in: int = 0
    blank_rows: int = 0
    duplicates: int = 0


def clean_isrc(raw):
    """Returns (isrc, problem). 'qz-abc-24-00001' -> ('QZABC2400001', '')."""
    isrc = normalize_isrc(raw)
    if isrc.startswith("ISRC") and len(isrc) == 16:  # "ISRC: QZ..." typed into the cell
        isrc = isrc[4:]
    if not isrc:
        return "", "no ISRC"
    if not ISRC_RE.match(isrc):
        return isrc, ("invalid ISRC: needs 12 characters, country (2 letters) + registrant (3) "
                      "+ year (2 digits) + number (5 digits)")
    return isrc, ""


_SPLIT = re.compile(r"\s*(?:;|/|&|\n|\band\b)\s*", re.IGNORECASE)


def split_writers(cells):
    """Writer cells -> (list of names, list of unsure pieces).

    Splits on ; / & 'and' and new lines. A comma splits only if every piece has
    at least two words ('John Smith, Ana Lopez'). 'Smith, John' could be one
    person written last-name-first, or two one-word names, so it's left as it
    is and reported as unsure.
    """
    names, unsure = [], []
    for cell in cells:
        for part in _SPLIT.split(cell):
            part = " ".join(part.split()).strip(" ,")
            if not part:
                continue
            if "," in part:
                pieces = [p.strip() for p in part.split(",") if p.strip()]
                if len(pieces) > 1 and all(len(p.split()) >= 2 for p in pieces):
                    names.extend(pieces)
                    continue
                unsure.append(part)
            names.append(part)
    seen, unique = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            unique.append(n)
    return unique, unsure


def convert(table, mapping):
    """Apply the mapping to every row. No AI here."""
    result = ImportResult()
    seen_rows = {}   # converted row -> first row number, for duplicates
    seen_isrcs = {}  # isrc -> first row number
    header_cells = [h.strip().lower() for h in table.headers]

    def get(cells, f):
        return " ".join(cells[mapping[f].columns[0]].split()) if mapping[f].columns else ""

    for number, cells in table.rows:
        if _is_blank(cells):
            result.blank_rows += 1
            continue
        if [c.strip().lower() for c in cells] == header_cells:  # header repeated mid-sheet
            result.blank_rows += 1
            continue
        result.rows_in += 1
        title, artist = get(cells, "title"), get(cells, "artist")
        writers, unsure = split_writers([cells[c] for c in mapping["writers"].columns])
        isrc, isrc_problem = clean_isrc(get(cells, "isrc"))

        if isrc_problem:
            result.problems.append(Problem(number, "isrc", get(cells, "isrc"), isrc_problem, "not imported"))
            continue
        key = (title, artist, isrc, tuple(writers))
        if key in seen_rows:
            result.duplicates += 1
            result.problems.append(Problem(number, "row", title, f"duplicate of row {seen_rows[key]}", "dropped"))
            continue
        seen_rows[key] = number
        if isrc in seen_isrcs:
            result.problems.append(Problem(number, "isrc", isrc, f"same ISRC as row {seen_isrcs[isrc]}, "
                                           "but different details", "imported, please check"))
        seen_isrcs.setdefault(isrc, number)
        if not title:
            result.problems.append(Problem(number, "title", "", "no title", "imported, please check"))
        if not writers:
            result.problems.append(Problem(number, "writers", "", "no writers (the writer check "
                                           "will be skipped for this song)", "imported, please check"))
        for piece in unsure:
            result.problems.append(Problem(number, "writers", piece, "not sure how to split this "
                                           "(one person written 'Last, First', or two people?); "
                                           "left as it is", "imported, please check"))
        result.rows.append({"title": title, "artist": artist, "isrc": isrc, "writers": "; ".join(writers)})
    return result


def write_catalog(rows, path):
    """Retrack's own catalog format: title, artist, isrc, writers (split by ';')."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(FIELDS))
        w.writeheader()
        w.writerows(rows)


def write_problems(problems, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row", "field", "value", "problem", "what happened"])
        for p in problems:
            w.writerow([p.row, p.field, p.value, p.problem, p.outcome])


def _n(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def print_summary(result, out, problems_path, print_fn=print, limit=20):
    print_fn(f"\nRows in: {result.rows_in}. Rows out: {len(result.rows)} (saved to {out}).")
    if result.blank_rows:
        print_fn(f"  Skipped {_n(result.blank_rows, 'blank or repeated header row')}.")
    if result.duplicates:
        print_fn(f"  Dropped {_n(result.duplicates, 'duplicate row')}.")
    if result.problems:
        print_fn(f"  {_n(len(result.problems), 'problem')} (saved to {problems_path}):")
        for p in result.problems[:limit]:
            value = f' "{p.value}"' if p.value else ""
            print_fn(f"    row {p.row}: {p.field}{value}: {p.problem} ({p.outcome})")
        if len(result.problems) > limit:
            print_fn(f"    ... and {len(result.problems) - limit} more in {problems_path}")
    else:
        print_fn("  No problems found.")


# ---- Command line ------------------------------------------------------------------

def main(argv=None, input_fn=input, stdin_is_tty=None, mapper=None, print_fn=print):
    parser = argparse.ArgumentParser(
        prog="python -m retrack import",
        description="Turn your own spreadsheet (.csv or .xlsx, any columns) into Retrack's catalog CSV.")
    parser.add_argument("file", help="your spreadsheet: .csv or .xlsx")
    parser.add_argument("--out", default="catalog.csv", help="where to save the catalog (default: catalog.csv)")
    parser.add_argument("--smart-import", action="store_true",
                        help="let the Claude API work out which column is which. It's sent only the "
                             f"header row and the first {SAMPLE_ROWS} rows; needs ANTHROPIC_API_KEY. "
                             "Without it, offline header-name rules are used.")
    parser.add_argument("--mock", action="store_true",
                        help="stay offline: with --smart-import, use the header-name rules instead of Claude")
    parser.add_argument("--yes", action="store_true", help="for scripts: use the column mapping without asking")
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.resolve() == Path(args.file).resolve():
        print("Error: --out would overwrite your original file. Pick another name.", file=sys.stderr)
        return 1
    problems_path = out.with_name("import_problems.csv")

    try:
        table = read_table(args.file)
    except CatalogImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    samples = sample_rows(table)

    if mapper is None:
        if not args.smart_import or args.mock:
            mapper = HeaderRuleMapper()  # the default: offline, nothing leaves this computer
        else:
            try:
                mapper = ClaudeColumnMapper()
            except ImportError:
                print("Error: --smart-import needs the anthropic package: pip install anthropic "
                      "(or leave out --smart-import to use the offline rules).", file=sys.stderr)
                return 1
    mapping = mapper.map_columns(table.headers, samples)
    hint = ""
    if mapping is None:  # fail safe: the AI didn't answer, use the offline rules
        print_fn("The AI column mapping didn't answer, so Retrack used its offline header rules instead.")
        mapping = HeaderRuleMapper().map_columns(table.headers, samples)
    elif isinstance(mapper, HeaderRuleMapper):
        hint = smart_import_hint(mapping)

    print_fn(f"Header row: row {table.header_row} of {Path(args.file).name}")
    if args.yes:
        for line in describe_mapping(mapping, table.headers):
            print_fn(f"  {line}")
        if hint:
            print_fn(hint)
    else:
        if stdin_is_tty is None:
            stdin_is_tty = sys.stdin.isatty()
        if not stdin_is_tty:
            print("Error: Retrack needs you to confirm the column mapping. Run it in a terminal, "
                  "or pass --yes.", file=sys.stderr)
            return 1
        mapping = confirm_mapping(mapping, table.headers, input_fn, print_fn, hint)
        if mapping is None:
            print_fn("Cancelled. Nothing was converted.")
            return 1

    missing = [f for f in REQUIRED if not mapping[f].columns]
    if missing:
        print(f"Error: no column chosen for {' and '.join(missing)}, so there's nothing to check. "
              "Run again without --yes and pick the columns, or with --smart-import.", file=sys.stderr)
        return 1

    result = convert(table, mapping)
    write_catalog(result.rows, out)
    write_problems(result.problems, problems_path)
    print_summary(result, out, problems_path, print_fn)
    print_fn(f"\nNext: python -m retrack {out}")
    return 0
