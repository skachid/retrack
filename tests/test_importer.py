"""Tests for the catalog importer (python -m retrack import).

No test needs an API key: Claude is faked. The fixtures in fixtures/import/ are
made-up data shaped like a DistroKid export, a TuneCore export and a messy
hand-made label spreadsheet.
"""

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from retrack import importer
from retrack.__main__ import main as retrack_main
from retrack.checker import RED, check_catalog, load_catalog
from retrack.importer import (SAMPLE_ROWS, CatalogImportError, ClaudeColumnMapper, FieldMap,
                              HeaderRuleMapper, Table, clean_isrc, clean_mapping, column_index,
                              column_letter, confirm_mapping, convert, describe_mapping, read_rows,
                              read_table, sample_rows, split_writers)
from retrack.mlc_client import MockClient

ROOT = Path(__file__).parent.parent
FIX = ROOT / "fixtures" / "import"
DISTROKID = FIX / "distrokid_export.csv"
TUNECORE = FIX / "tunecore_export.xlsx"
MESSY = FIX / "label_catalog_messy.xlsx"


def mapped(path):
    table = read_table(path)
    return table, HeaderRuleMapper().map_columns(table.headers, sample_rows(table))


def cols(mapping):
    return {f: m.columns for f, m in mapping.items()}


def run_import(tmp_path, src, *extra, **kw):
    out = tmp_path / "catalog.csv"
    code = importer.main([str(src), "--out", str(out), *extra], print_fn=lambda *a: None, **kw)
    return code, out


# ---- Reading files ---------------------------------------------------------------

def test_header_row_is_found_below_title_rows():
    table = read_table(MESSY)
    assert table.header_row == 4
    assert table.headers[:5] == ["#", "Song Name", "Performer", "ISRC code", "Composer(s)"]


def test_busiest_sheet_is_used_not_the_notes_tab():
    assert read_table(MESSY).headers[1] == "Song Name"


def test_merged_cells_are_filled_with_their_value():
    rows = {n: r for n, r in read_table(MESSY).rows}
    assert rows[7][2] == "Nova Reyes"  # C6:C7 merged, value only stored in C6


def test_semicolon_csv_from_european_excel(tmp_path):
    f = tmp_path / "eu.csv"
    f.write_text("Titel;Artist;ISRC;Writer\nLied;Band;QZABC2400001;A B\n", encoding="utf-8")
    assert read_rows(f)[1] == ["Lied", "Band", "QZABC2400001", "A B"]


def test_comma_csv_with_semicolons_in_writers_stays_comma(tmp_path):
    f = tmp_path / "c.csv"
    f.write_text("title,artist,isrc,writers\nS,A,QZABC2400001,A B; C D\nT,A,QZABC2400002,E F\n")
    assert read_rows(f)[1] == ["S", "A", "QZABC2400001", "A B; C D"]


def test_windows_encoded_csv(tmp_path):
    f = tmp_path / "w.csv"
    f.write_bytes("title,artist,isrc,writers\nCafé,Zoë,QZABC2400001,José Núñez\n".encode("cp1252"))
    assert read_rows(f)[1][0] == "Café"


def test_unsupported_file_type(tmp_path):
    f = tmp_path / "x.numbers"
    f.write_text("")
    with pytest.raises(CatalogImportError, match=".csv and .xlsx"):
        read_rows(f)


@pytest.mark.parametrize("i,letter", [(0, "A"), (25, "Z"), (26, "AA"), (51, "AZ")])
def test_column_letters(i, letter):
    assert column_letter(i) == letter and column_index(letter) == i


# ---- Mapping: offline header rules -----------------------------------------------

def test_maps_distrokid_export():
    table, m = mapped(DISTROKID)
    h = table.headers
    assert cols(m) == {"title": [h.index("Title")], "artist": [h.index("Artist")],
                       "isrc": [h.index("ISRC")], "writers": []}  # "Songwriter Royalties Withheld" isn't a writer


def test_maps_tunecore_export_with_two_writer_columns():
    table, m = mapped(TUNECORE)
    h = table.headers
    assert m["title"].columns == [h.index("Song Title")]  # not "Release Title"
    assert m["isrc"].columns == [h.index("Optional ISRC")]
    assert m["writers"].columns == [h.index("Songwriter 1"), h.index("Songwriter 2")]


def test_maps_messy_label_sheet():
    table, m = mapped(MESSY)
    assert cols(m) == {"title": [1], "artist": [2], "isrc": [3], "writers": [4]}


def test_isrc_column_found_from_values_when_header_is_vague():
    headers = ["Song", "Code", "Who"]
    samples = [["A", "QZABC2400001", "X"], ["B", "QZ-ABC-24-00002", "Y"]]
    assert HeaderRuleMapper().map_columns(headers, samples)["isrc"].columns == [1]


def test_clean_mapping_drops_impossible_answers():
    m = clean_mapping({"title": FieldMap([0, 1], 3.0), "artist": FieldMap([0], 0.9),
                       "isrc": FieldMap([99], 0.9), "writers": FieldMap([2, 2], 0.5)}, 3)
    assert cols(m) == {"title": [0], "artist": [], "isrc": [], "writers": [2]}
    assert m["title"].confidence == 1.0


# ---- Mapping: Claude (faked) -------------------------------------------------------

class FakeAnthropic:
    def __init__(self, answer=None, stop_reason="end_turn", error=None):
        self.calls, self.sent = 0, []
        self.answer, self.stop_reason, self.error = answer, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.sent.append(kwargs)
        if self.error:
            raise self.error
        text = SimpleNamespace(type="text", text=json.dumps(self.answer))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])


ANSWER = {"title": {"column": "B", "confidence": 0.95}, "artist": {"column": "C", "confidence": 0.8},
          "isrc": {"column": "D", "confidence": 0.99},
          "writers": {"columns": ["E"], "confidence": 0.88}}


def test_only_headers_and_five_sample_rows_go_to_claude(tmp_path):
    f = tmp_path / "big.csv"
    lines = ["Num,Song Name,Performer,ISRC code,Composer(s)"]
    lines += [f"{i},SECRET-TITLE-{i},Band {i},QZABC24{i:05d},Writer {i}" for i in range(1, 41)]
    f.write_text("\n".join(lines) + "\n")
    fake = FakeAnthropic(ANSWER)
    code, out = run_import(tmp_path, f, "--yes", mapper=ClaudeColumnMapper(client=fake))
    assert code == 0 and fake.calls == 1

    request = fake.sent[0]
    sent_text = request["system"] + json.dumps(request["messages"], ensure_ascii=False)
    # rows 1-5 may be sent, rows 6-40 must not be
    for i in range(1, SAMPLE_ROWS + 1):
        assert f"SECRET-TITLE-{i}" in sent_text
    for i in range(SAMPLE_ROWS + 1, 41):
        assert f"SECRET-TITLE-{i}," not in sent_text and f"SECRET-TITLE-{i}\"" not in sent_text
    # and the message is exactly the header row plus those sample rows, nothing else
    payload = json.loads(request["messages"][0]["content"])
    assert [c["header"] for c in payload["columns"]] == lines[0].split(",")
    sent_rows = list(zip(*[c["samples"] for c in payload["columns"]]))
    assert [list(r) for r in sent_rows] == [l.split(",") for l in lines[1:SAMPLE_ROWS + 1]]
    assert len(request["messages"]) == 1
    # but every row was still converted, by plain Python
    assert len(list(csv.DictReader(out.open()))) == 40


def test_claude_mapping_is_read_from_column_letters():
    m = ClaudeColumnMapper(client=FakeAnthropic(ANSWER)).map_columns(["#", "a", "b", "c", "d"], [])
    assert cols(m) == {"title": [1], "artist": [2], "isrc": [3], "writers": [4]}
    assert m["writers"].confidence == 0.88


def test_claude_mapping_ignores_columns_that_dont_exist():
    answer = dict(ANSWER, isrc={"column": "ZZ", "confidence": 0.9})
    m = ClaudeColumnMapper(client=FakeAnthropic(answer)).map_columns(["#", "a", "b", "c", "d"], [])
    assert m["isrc"].columns == []


def test_claude_mapping_is_cached():
    fake = FakeAnthropic(ANSWER)
    mapper = ClaudeColumnMapper(client=fake)
    mapper.map_columns(["#", "a", "b", "c", "d"], [["1", "2", "3", "4", "5"]])
    mapper.map_columns(["#", "a", "b", "c", "d"], [["1", "2", "3", "4", "5"]])
    assert fake.calls == 1


@pytest.mark.parametrize("fake", [FakeAnthropic(error=ConnectionError("offline")),
                                  FakeAnthropic(ANSWER, stop_reason="refusal"),
                                  FakeAnthropic({"nonsense": True})])
def test_claude_failure_gives_none(fake):
    assert ClaudeColumnMapper(client=fake).map_columns(["a", "b"], []) is None


def test_claude_failure_falls_back_to_header_rules(tmp_path):
    mapper = ClaudeColumnMapper(client=FakeAnthropic(error=ConnectionError("offline")))
    code, out = run_import(tmp_path, MESSY, "--yes", mapper=mapper)
    assert code == 0
    assert len(list(csv.DictReader(out.open()))) == 6


def test_mock_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, out = run_import(tmp_path, MESSY, "--mock", "--yes")
    assert code == 0 and out.exists()


# ---- Confirming the mapping ---------------------------------------------------------

def test_mapping_is_shown_as_header_arrow_field_percent():
    table, m = mapped(MESSY)
    lines = describe_mapping(m, table.headers)
    assert lines[0] == "Song Name (B) → title (90%)"
    assert lines[3] == "Composer(s) (E) → writers (90%)"


def test_low_confidence_and_missing_fields_are_pointed_out():
    m = {"title": FieldMap([0], 0.4), "artist": FieldMap(), "isrc": FieldMap(), "writers": FieldMap()}
    lines = describe_mapping(m, ["Thing"])
    assert "not sure, please check" in lines[0]
    assert lines[2] == "(no column) → isrc   ← needed"


def answers(*replies):
    it = iter(replies)
    return lambda _prompt: next(it)


def test_confirm_yes_keeps_the_mapping():
    table, m = mapped(MESSY)
    assert cols(confirm_mapping(m, table.headers, answers("y"), lambda *a: None)) == cols(m)


def test_confirm_no_cancels():
    table, m = mapped(MESSY)
    assert confirm_mapping(m, table.headers, answers("n"), lambda *a: None) is None


def test_user_can_correct_a_column():
    table, m = mapped(MESSY)
    # edit: title -> F (by letter), artist kept, isrc kept, writers -> none; then accept
    fixed = confirm_mapping(m, table.headers, answers("e", "F", "", "", "-", "y"), lambda *a: None)
    assert fixed["title"].columns == [5] and fixed["title"].confidence == 1.0
    assert fixed["writers"].columns == []


def test_user_can_pick_a_column_by_header_name():
    table, m = mapped(MESSY)
    fixed = confirm_mapping(m, table.headers, answers("e", "notes", "", "", "", "y"), lambda *a: None)
    assert fixed["title"].columns == [5]


def test_cli_without_terminal_or_yes_converts_nothing(tmp_path):
    code, out = run_import(tmp_path, MESSY, "--mock", stdin_is_tty=False)
    assert code == 1 and not out.exists()


def test_cli_answering_no_converts_nothing(tmp_path):
    code, out = run_import(tmp_path, MESSY, "--mock", stdin_is_tty=True, input_fn=answers("n"))
    assert code == 1 and not out.exists()


def test_cli_answering_yes_converts(tmp_path):
    code, out = run_import(tmp_path, MESSY, "--mock", stdin_is_tty=True, input_fn=answers("y"))
    assert code == 0 and out.exists()


def test_no_title_or_isrc_column_stops_the_import(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("Foo,Bar\nhello,world\nmore,stuff\n")
    code, out = run_import(tmp_path, f, "--mock", "--yes")
    assert code == 1 and not out.exists()


def test_out_cannot_overwrite_the_input(tmp_path):
    f = tmp_path / "catalog.csv"
    f.write_text("title,artist,isrc,writers\nS,A,QZABC2400001,W X\n")
    assert importer.main([str(f), "--out", str(f), "--mock", "--yes"]) == 1


# ---- Cleanup rule: ISRCs --------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("QZABC2400001", "QZABC2400001"),
    ("qz-abc-24-00001", "QZABC2400001"),
    (" QZ ABC 24 00001 ", "QZABC2400001"),
    ("QZ.ABC.24.00001", "QZABC2400001"),
    ("ISRC: QZ-ABC-24-00001", "QZABC2400001"),
    ("US1A22400001", "US1A22400001"),  # registrant can contain digits
])
def test_isrc_is_normalized(raw, expected):
    assert clean_isrc(raw) == (expected, "")


@pytest.mark.parametrize("raw", [
    "QZABC240001",     # 11 characters
    "QZABC24000011",   # 13 characters
    "1ZABC2400001",    # country must be 2 letters
    "QZABCX400001",    # year must be digits
    "QZABC24A0001",    # number must be digits
])
def test_invalid_isrc_is_flagged(raw):
    isrc, problem = clean_isrc(raw)
    assert problem.startswith("invalid ISRC")


def test_empty_isrc():
    assert clean_isrc("  ") == ("", "no ISRC")


# ---- Cleanup rule: splitting writers ---------------------------------------------------

@pytest.mark.parametrize("cell,expected", [
    ("Ana Lopez; Ben Cho", ["Ana Lopez", "Ben Cho"]),
    ("Ana Lopez / Ben Cho", ["Ana Lopez", "Ben Cho"]),
    ("Ana Lopez & Ben Cho", ["Ana Lopez", "Ben Cho"]),
    ("Ana Lopez and Ben Cho", ["Ana Lopez", "Ben Cho"]),
    ("Ana Lopez AND Ben Cho", ["Ana Lopez", "Ben Cho"]),
    ("Ana Lopez\nBen Cho", ["Ana Lopez", "Ben Cho"]),
    ("John Smith, Ana Lopez", ["John Smith", "Ana Lopez"]),   # every piece is a full name: split
    ("Ana Lopez, Ben Cho and Sam Lee", ["Ana Lopez", "Ben Cho", "Sam Lee"]),
    ("Sandra Anderson", ["Sandra Anderson"]),                 # "and" inside a name isn't a separator
    ("Ana Lopez;  ; Ana Lopez", ["Ana Lopez"]),               # blanks and repeats removed
])
def test_writers_are_split(cell, expected):
    assert split_writers([cell]) == (expected, [])


@pytest.mark.parametrize("cell", ["Smith, John", "Prince, Madonna", "Smith, John Paul"])
def test_unsure_comma_is_left_as_is_and_flagged(cell):
    names, unsure = split_writers([cell])
    assert names == [cell] and unsure == [cell]


def test_writers_from_several_columns():
    assert split_writers(["Ana Lopez", "", "Ben Cho / Sam Lee"]) == (["Ana Lopez", "Ben Cho", "Sam Lee"], [])


# ---- Cleanup rules on whole files: duplicates, missing data, row numbers ----------------

@pytest.fixture
def messy():
    table, m = mapped(MESSY)
    return convert(table, m)


def problems_by_row(result):
    out = {}
    for p in result.problems:
        out.setdefault(p.row, []).append(p)
    return out


def test_exact_duplicate_row_is_dropped_and_reported(messy):
    p = problems_by_row(messy)[12][0]
    assert (p.problem, p.outcome) == ("duplicate of row 5", "dropped")
    assert [r["title"] for r in messy.rows].count("Late Night Drive") == 1
    assert messy.duplicates == 1


def test_repeated_rows_in_an_earnings_export_collapse_to_one_song():
    table, m = mapped(DISTROKID)
    result = convert(table, m)
    assert [r["title"] for r in result.rows] == ["Late Night Drive", "Paper Crowns", "Concrete Garden", "Blue Hour"]


def test_same_isrc_with_different_details_is_kept_and_flagged():
    t = Table(["title", "artist", "isrc", "writers"],
              [(2, ["Song", "A", "QZABC2400001", "W X"]), (3, ["Song (Remix)", "A", "QZABC2400001", "W X"])], 1)
    result = convert(t, HeaderRuleMapper().map_columns(t.headers, []))
    assert len(result.rows) == 2
    assert result.problems[0].row == 3 and "same ISRC as row 2" in result.problems[0].problem


def test_missing_isrc_is_flagged_not_silently_dropped(messy):
    p = problems_by_row(messy)[11][0]
    assert (p.field, p.problem, p.outcome) == ("isrc", "no ISRC", "not imported")


def test_invalid_isrc_row_is_flagged_and_not_imported(messy):
    p = problems_by_row(messy)[10][0]
    assert p.value == "QZABC240005" and p.outcome == "not imported"
    assert "Blue Hour" not in [r["title"] for r in messy.rows]


def test_missing_writers_is_imported_and_flagged(messy):
    p = problems_by_row(messy)[14][0]
    assert (p.field, p.outcome) == ("writers", "imported, please check")
    assert {"title": "Static Bloom", "artist": "The Lowlights", "isrc": "QZABC2400006", "writers": ""} in messy.rows


def test_unsure_writer_is_imported_as_is_and_flagged(messy):
    p = problems_by_row(messy)[9][0]
    assert p.value == "Lee, Sam" and p.outcome == "imported, please check"


def test_blank_rows_and_repeated_headers_are_skipped(messy):
    assert messy.blank_rows == 2          # row 8 blank, row 13 the header again
    assert messy.rows_in == 9             # rows 5-7, 9-12, 14-15
    assert len(messy.rows) == 6


def test_every_row_is_accounted_for(messy):
    """rows in = rows out + not imported + dropped: nothing disappears silently."""
    gone = sum(1 for p in messy.problems if p.outcome in ("not imported", "dropped"))
    assert messy.rows_in == len(messy.rows) + gone


# ---- Output -------------------------------------------------------------------------

def test_output_is_a_catalog_retrack_can_check(tmp_path):
    code, out = run_import(tmp_path, MESSY, "--mock", "--yes")
    assert code == 0
    rows = load_catalog(out)
    assert [r.isrc for r in rows][:3] == ["QZABC2400001", "QZABC2400002", "QZABC2400003"]
    assert rows[1].writers == ["Nova Reyes", "Marcus Bell"]
    results = check_catalog(rows, MockClient())
    assert len(results) == 6 and any(r.status == RED for r in results)


def test_problems_file_lists_row_numbers(tmp_path):
    run_import(tmp_path, MESSY, "--mock", "--yes")
    rows = list(csv.DictReader((tmp_path / "import_problems.csv").open()))
    assert [r["row"] for r in rows] == ["9", "10", "11", "12", "14"]
    assert rows[0].keys() == {"row", "field", "value", "problem", "what happened"}


def test_summary_shows_rows_in_and_out_and_problems(tmp_path):
    printed = []
    importer.main([str(MESSY), "--out", str(tmp_path / "c.csv"), "--mock", "--yes"], print_fn=printed.append)
    text = "\n".join(printed)
    assert "Rows in: 9. Rows out: 6" in text
    assert "5 problems" in text and "row 10:" in text
    assert "Song Name (B) → title (90%)" in text


def test_import_is_a_retrack_subcommand(tmp_path, capsys):
    out = tmp_path / "c.csv"
    assert retrack_main(["import", str(TUNECORE), "--out", str(out), "--mock", "--yes"]) == 0
    assert out.exists()
    assert "Rows in: 4. Rows out: 4" in capsys.readouterr().out
