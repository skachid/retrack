"""Tests for the fix pack (fixes.py) and the compliance rules.

The most important ones:
  - no files without attestation
  - ownership fields (shares, IPIs, role codes, publishers) are never guessed
  - the same price for every verdict
"""

import hashlib
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from retrack import fixes
from retrack.__main__ import main
from retrack.checker import (GREEN, NOT_FOUND, NOT_LINKED, RED, YELLOW, CatalogRow, Result,
                             check_catalog, load_catalog)
from retrack.compliance import ATTESTATION_TEXT, COMPLIANCE_TEXT
from retrack.fixes import (CLAIM, FILES, LINK, MUST, MUST_FILL, REGISTER, Attestation, FixPackError,
                           build_fix_pack, classify_header, confirm, fix_kind, plan_fixes, price_of,
                           price_line, registration_rows)
from retrack.mlc_client import MockClient
from retrack.name_matcher import MockNameMatcher
from retrack.report import render_html
from retrack.work_search import MockWorkSearcher

ROOT = Path(__file__).parent.parent
CATALOG = str(ROOT / "sample_catalog.csv")


@pytest.fixture
def results():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    return check_catalog(rows, MockClient(), MockNameMatcher(), MockWorkSearcher())


@pytest.fixture
def plan(results):
    return plan_fixes(results)


def make_template(path, song_code_column=True):
    """A FAKE template for tests only. It is not The MLC's template and isn't shipped:
    Retrack itself never creates one. The column order loosely follows The MLC's help
    page (title in A, writers from G, collection share in Q, recording title/artist in R/S)."""
    wb = Workbook()
    defs = wb.active
    defs.title = "Field Definitions"  # a first tab without a header row, to check we skip it
    defs.append(["Field", "Definition"])
    defs.append(["Work Title", "The title of the work"])
    ws = wb.create_sheet("Works")
    headers = ["Work Title*", "MLC Song Code" if song_code_column else "Work Language", "ISWC", "Notes",
               "AKA Title", "AKA Title Type Code", "Writer Last Name*", "Writer First Name*",
               "Writer Middle Name", "Writer Role Code*", "Writer IPI Number", "Publisher Name*",
               "Publisher IPI Number*", "Publisher Role Code", "Administrator Name",
               "Administrator IPI Number", "Collection Share*", "Recording Title", "Recording Artist", "ISRC"]
    ws.append(headers)
    wb.save(path)
    return headers


def col(headers, name):
    return headers.index(name) + 1


OWNERSHIP_HEADERS = ["Writer Role Code*", "Writer IPI Number", "Publisher Name*",
                     "Publisher IPI Number*", "Publisher Role Code", "Collection Share*"]


# ---- Rule: no files without attestation ----------------------------------------

def test_no_attestation_means_no_files(plan, tmp_path):
    out = tmp_path / "fix_pack"
    with pytest.raises(FixPackError, match="No confirmation, no files"):
        build_fix_pack(plan, out, attestation=None)
    assert not out.exists()


def test_a_made_up_attestation_is_refused(plan, tmp_path):
    out = tmp_path / "fix_pack"
    with pytest.raises(FixPackError):
        build_fix_pack(plan, out, Attestation("whatever", "2026-01-01T00:00:00+00:00", "x"))
    with pytest.raises(FixPackError):
        build_fix_pack(plan, out, "yes")
    assert not out.exists()


def test_cli_without_terminal_or_flag_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main([CATALOG, "--mock", "--ai-search", "--fix"], stdin_is_tty=False) == 1
    assert not (tmp_path / "fix_pack").exists()
    assert "No confirmation, so no files were created" in capsys.readouterr().err


def test_cli_prompt_answered_no_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([CATALOG, "--mock", "--ai-search", "--fix"], input_fn=lambda _: "no", stdin_is_tty=True) == 1
    assert not (tmp_path / "fix_pack").exists()


def test_cli_prompt_answered_yes_writes_and_records_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([CATALOG, "--mock", "--ai-search", "--fix"], input_fn=lambda _: " YES ", stdin_is_tty=True) == 0
    record = (tmp_path / "fix_pack" / "ATTESTATION.txt").read_text()
    assert ATTESTATION_TEXT in record
    assert "typed yes at the prompt" in record
    assert "Confirmed at (UTC): 20" in record  # a timestamp


def test_i_confirm_flag_is_recorded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([CATALOG, "--mock", "--ai-search", "--fix", "--i-confirm"], stdin_is_tty=False) == 0
    assert "--i-confirm flag" in (tmp_path / "fix_pack" / "ATTESTATION.txt").read_text()


def test_prompt_with_no_keyboard_is_not_a_yes():
    def no_keyboard(_):
        raise EOFError
    assert fixes.ask_for_attestation(no_keyboard, print_fn=lambda *a: None) is None


# ---- Rule: ownership fields are never guessed -------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("Collection Share*", MUST), ("Writer IPI Number", MUST), ("Publisher Name*", MUST),
    ("Publisher IPI Number*", MUST), ("Writer Role Code*", MUST),
    ("Administrator Name", None), ("Administrator IPI Number", None), ("AKA Title", None),
    ("Work Title*", "work_title"), ("MLC Song Code", "song_code"), ("Writer Last Name*", "writer_last"),
    ("Writer First Name*", "writer_first"), ("Writer Middle Name", None),
    ("Recording Title", "recording_title"), ("Recording Artist", "recording_artist"), ("ISRC", "isrc"),
])
def test_classify_header(header, expected):
    assert classify_header(header) == expected


def test_registration_rows_never_contain_ownership_fields(results):
    allowed = {"work_title", "song_code", "iswc", "writer_first", "writer_last", "writer_name",
               "recording_title", "recording_artist", "isrc"}
    for r in results:
        for values in registration_rows(r):
            assert set(values) <= allowed


def test_worksheet_leaves_every_ownership_cell_for_the_user(plan, tmp_path):
    build_fix_pack(plan, tmp_path, confirm("test"))
    lines = (tmp_path / FILES["worksheet"]).read_text().splitlines()
    header = next(l for l in lines if l.startswith("Work Title")).split(",")
    data = [l.split(",") for l in lines[lines.index(",".join(header)) + 1:]]
    assert data
    for row in data:
        for name in ("Writer IPI", "Writer Role Code", "Publisher Name", "Publisher IPI", "Collection Share"):
            assert row[header.index(name)] == MUST_FILL


def test_template_ownership_cells_are_blank_highlighted_and_commented(plan, tmp_path):
    template = tmp_path / "mlc_template.xlsx"
    headers = make_template(template)
    build_fix_pack(plan, tmp_path / "out", confirm("test"), template)
    ws = load_workbook(tmp_path / "out" / FILES["xlsx"])["Works"]
    assert ws.max_row >= 2
    for row in range(2, ws.max_row + 1):
        for name in OWNERSHIP_HEADERS:
            cell = ws.cell(row=row, column=col(headers, name))
            assert cell.value is None
            assert cell.fill.start_color.rgb.endswith("FFF2CC")
            assert MUST_FILL in cell.comment.text
        # optional columns are left alone, not marked
        assert ws.cell(row=row, column=col(headers, "Administrator Name")).comment is None


def test_song_without_writers_gets_writer_cells_marked(tmp_path):
    r = Result(CatalogRow("No Writers", "X", "QZ0000000001", []), RED, red_case=NOT_FOUND)
    template = tmp_path / "t.xlsx"
    headers = make_template(template)
    build_fix_pack(plan_fixes([r]), tmp_path / "out", confirm("test"), template)
    ws = load_workbook(tmp_path / "out" / FILES["xlsx"])["Works"]
    assert ws.cell(row=2, column=col(headers, "Writer Last Name*")).comment is not None


# ---- Rule: the same price for every verdict ------------------------------------------

def test_same_price_for_every_verdict():
    row = CatalogRow("S", "A", "QZ1", ["W X"])
    prices = {price_of(Result(row, status, red_case=case))
              for status, case in [(RED, NOT_FOUND), (RED, NOT_LINKED), (YELLOW, ""), (RED, ""), (GREEN, "")]}
    assert prices == {fixes.FIX_PRICE_USD}


def test_summary_shows_songs_times_price(plan):
    assert len(plan.songs) == 4
    total = 4 * fixes.FIX_PRICE_USD
    assert price_line(plan.songs).startswith(f"4 songs × ${fixes.FIX_PRICE_USD:,.2f} = ${total:,.2f}")


def test_cli_prints_the_price(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main([CATALOG, "--mock", "--ai-search", "--fix-songs", "Blue Hour,ghost signal", "--i-confirm"])
    assert f"2 songs × ${fixes.FIX_PRICE_USD:,.2f}" in capsys.readouterr().out


# ---- Choosing songs ----------------------------------------------------------------

def test_each_verdict_gets_its_fix(results):
    kinds = {r.row.title: fix_kind(r) for r in results}
    assert kinds == {"Ghost Signal": REGISTER, "Blue Hour": LINK, "Paper Crowns": CLAIM,
                     "Concrete Garden": CLAIM, "Late Night Drive": None}


def test_red_without_ai_search_is_skipped_not_registered():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    plan = plan_fixes(check_catalog(rows, MockClient()))
    assert plan.register == [] and plan.link == []
    assert all("--ai-search" in why for r, why in plan.skipped if r.status == RED)


def test_fix_songs_picks_titles_case_insensitively(results):
    plan = plan_fixes(results, ["blue hour", " Late Night Drive "])
    assert [r.row.title for r in plan.songs] == ["Blue Hour"]
    assert [r.row.title for r, _ in plan.skipped] == ["Late Night Drive"]


def test_fix_songs_with_unknown_title_is_an_error(results):
    with pytest.raises(FixPackError, match="nope"):
        plan_fixes(results, ["Blue Hour", "Nope"])


# ---- The template copy ---------------------------------------------------------------

def test_original_template_is_never_changed(plan, tmp_path):
    template = tmp_path / "mlc_template.xlsx"
    make_template(template)
    before = hashlib.sha256(template.read_bytes()).hexdigest()
    build_fix_pack(plan, tmp_path / "out", confirm("test"), template)
    assert hashlib.sha256(template.read_bytes()).hexdigest() == before


def test_template_copy_gets_known_facts_one_row_per_writer(tmp_path):
    r = Result(CatalogRow("Two Writers", "The Band", "QZ0000000009", ["Ana Lopez", "Ben Cho"]),
               RED, red_case=NOT_FOUND)
    template = tmp_path / "t.xlsx"
    headers = make_template(template)
    build_fix_pack(plan_fixes([r]), tmp_path / "out", confirm("test"), template)
    ws = load_workbook(tmp_path / "out" / FILES["xlsx"])["Works"]
    get = lambda row, name: ws.cell(row=row, column=col(headers, name)).value
    assert get(2, "Work Title*") == "Two Writers"
    assert (get(2, "Writer First Name*"), get(2, "Writer Last Name*")) == ("Ana", "Lopez")
    assert (get(2, "Recording Title"), get(2, "Recording Artist"), get(2, "ISRC")) == \
        ("Two Writers", "The Band", "QZ0000000009")
    # second writer: own row, work title left blank (a title may appear once per document)
    assert get(3, "Work Title*") is None
    assert (get(3, "Writer First Name*"), get(3, "Writer Last Name*")) == ("Ben", "Cho")


def test_song_code_is_included_when_a_work_exists(tmp_path):
    r = Result(CatalogRow("Has Code", "A", "QZ0000000010", ["Ana Lopez"]), RED, red_case=NOT_FOUND,
               song_code="HC1234")
    template = tmp_path / "t.xlsx"
    headers = make_template(template)
    build_fix_pack(plan_fixes([r]), tmp_path / "out", confirm("test"), template)
    ws = load_workbook(tmp_path / "out" / FILES["xlsx"])["Works"]
    assert ws.cell(row=2, column=col(headers, "MLC Song Code")).value == "HC1234"


def test_existing_work_without_song_code_column_goes_to_checklist_not_template(tmp_path):
    r = Result(CatalogRow("Has Code", "A", "QZ0000000010", ["Ana Lopez"]), RED, red_case=NOT_FOUND,
               song_code="HC1234")
    template = tmp_path / "t.xlsx"
    make_template(template, song_code_column=False)
    written = build_fix_pack(plan_fixes([r]), tmp_path / "out", confirm("test"), template)
    assert FILES["xlsx"] not in written  # registering it would create a duplicate
    checklist = (tmp_path / "out" / FILES["checklist"]).read_text()
    assert "HC1234" in checklist and "Don't register it again" in checklist


def test_template_must_be_xlsx_with_recognisable_headers(plan, tmp_path):
    with pytest.raises(FixPackError, match=r"\.xlsx"):
        fixes.check_template(tmp_path / "template.csv")
    blank = tmp_path / "blank.xlsx"
    Workbook().save(blank)
    with pytest.raises(FixPackError, match="column headers"):
        build_fix_pack(plan, tmp_path / "out", confirm("test"), blank)
    assert not (tmp_path / "out").exists()


def test_cli_bad_template_fails_before_asking_to_confirm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asked = []
    code = main([CATALOG, "--mock", "--ai-search", "--fix", "--template", "missing.xlsx"],
                input_fn=lambda q: asked.append(q) or "yes", stdin_is_tty=True)
    assert code == 1 and asked == []


# ---- Other files -----------------------------------------------------------------------

def test_match_suggestions_file(plan, tmp_path):
    build_fix_pack(plan, tmp_path, confirm("test"))
    text = (tmp_path / FILES["matches"]).read_text()
    assert "ISRC,MLC Song Code,Your Title,MLC Title,Your Writers,MLC Writers,Confidence" in text
    assert "QZABC2400005,BH3456,Blue Hour,THE BLUE HOUR,Jide Okafor,Jyde Okafor,90%" in text
    assert "Please confirm" in text


def test_fix_pack_readme_explains_each_file_and_the_upload_steps(plan, tmp_path):
    written = build_fix_pack(plan, tmp_path, confirm("test"))
    readme = (tmp_path / "README.md").read_text()
    for name in written:
        if name != "README.md":
            assert name in readme
    assert "Bulk Registration Tool" in readme
    assert "bulk.matching@themlc.com" in readme and "ISRC,MLC Song Code" in readme
    assert "Matching Tool" in readme
    assert price_line(plan.songs) in readme


# ---- Compliance text, everywhere -------------------------------------------------------

def mlc_format(name):
    """Files whose format The MLC dictates: nothing of ours may be added to them."""
    return name.endswith(".xlsx") or name.startswith("BulkMatchSuggestions_")


def test_compliance_text_is_in_our_files_and_not_in_mlc_format_files(plan, tmp_path):
    template = tmp_path / "t.xlsx"
    make_template(template)
    out = tmp_path / "out"
    written = build_fix_pack(plan, out, confirm("test"), template)
    assert {"README.md", "ATTESTATION.txt"} <= set(written)
    for name in written:
        if name.endswith(".xlsx"):
            continue  # checked in test_template_copy_is_untouched_apart_from_filled_rows
        text = (out / name).read_text(encoding="utf-8")
        assert (COMPLIANCE_TEXT in text) is not mlc_format(name), name


def test_template_copy_is_untouched_apart_from_filled_rows(plan, tmp_path):
    template = tmp_path / "t.xlsx"
    make_template(template)
    build_fix_pack(plan, tmp_path / "out", confirm("test"), template)
    original, copy = load_workbook(template), load_workbook(tmp_path / "out" / FILES["xlsx"])
    assert copy.sheetnames == original.sheetnames  # no extra tabs
    assert copy.properties.description == original.properties.description
    assert [c.value for c in copy["Works"][1]] == [c.value for c in original["Works"][1]]
    for ws_name in original.sheetnames:
        if ws_name != "Works":  # other tabs exactly as they were
            assert [[c.value for c in row] for row in copy[ws_name].iter_rows()] == \
                [[c.value for c in row] for row in original[ws_name].iter_rows()]
    assert not any(COMPLIANCE_TEXT == c.value for ws in copy.worksheets for row in ws.iter_rows() for c in row)


def test_bulk_match_file_is_exactly_the_mlc_format(plan, tmp_path):
    written = build_fix_pack(plan, tmp_path, confirm("test"))
    name = next(n for n in written if n.startswith("BulkMatchSuggestions_"))
    assert len(name) == len("BulkMatchSuggestions_YYYYMMDD.csv") and name.endswith(".csv")
    assert (tmp_path / name).read_bytes() == b"ISRC,MLC Song Code\nQZABC2400005,BH3456\n"


def test_bulk_match_file_has_no_duplicate_isrcs(tmp_path):
    from retrack.work_search import Candidate
    def linked(title, code):
        return Result(CatalogRow(title, "A", "QZ0000000001", ["W X"]), RED, red_case=NOT_LINKED,
                      suggested_match=Candidate(code, title.upper(), "", ["W X"], True, 0.9))
    fixes.write_bulk_match([linked("One", "AA1"), linked("Two", "BB2")], tmp_path / "b.csv")
    assert (tmp_path / "b.csv").read_text().splitlines() == ["ISRC,MLC Song Code", "QZ0000000001,AA1"]


def test_html_report_and_homepage_have_the_compliance_text(results):
    from retrack.web import render_home
    assert COMPLIANCE_TEXT in render_html(results)
    assert COMPLIANCE_TEXT in render_home()


def test_no_logos_or_images_anywhere(results):
    from retrack.web import render_home
    for page in (render_html(results), render_home()):
        lower = page.lower()
        assert "<img" not in lower and "<svg" not in lower and "logo" not in lower


def test_fix_pack_code_never_talks_to_the_network():
    source = Path(fixes.__file__).read_text()
    for word in ("import requests", "urllib", "http.client", "socket", "getpass"):
        assert word not in source


def test_retrack_never_prompts_for_a_password():
    for path in (ROOT / "retrack").glob("*.py"):
        assert "getpass" not in path.read_text(), path.name
