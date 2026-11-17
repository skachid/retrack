"""Tests for the HTML report (--html) and the local preview server (--serve)."""

import datetime
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from retrack import preview
from retrack.__main__ import main
from retrack.checker import CatalogRow, Result, RED, check_catalog, load_catalog
from retrack.mlc_client import MockClient
from retrack.report import render_html

ROOT = Path(__file__).parent.parent


@pytest.fixture
def html():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    results = check_catalog(rows, MockClient())
    return render_html(results, catalog_name="sample_catalog.csv", mock=True,
                       checked_on=datetime.date(2026, 11, 16))


# ---- What the report says ------------------------------------------------------

def test_summary_shows_the_three_counts(html):
    # sample catalog: 2 red, 2 yellow, 1 green
    assert '<dt>Red</dt><dd><span class="figure">2</span>' in html
    assert '<dt>Yellow</dt><dd><span class="figure">2</span>' in html
    assert '<dt>Green</dt><dd><span class="figure">1</span>' in html
    assert "2 songs need action, 2 need a closer look and 1 is all set." in html


def test_red_songs_come_first_then_yellow_then_green(html):
    red = html.index("Ghost Signal")
    yellow = html.index("Paper Crowns")
    green = html.index("Late Night Drive")
    assert red < yellow < green


def test_each_card_shows_its_problems_and_fix_steps(html):
    assert "The MLC has no recording with this ISRC." in html
    assert "Register the song (composition) with The MLC, listing this ISRC." in html
    assert html.count('<details class="song') == 5


def test_saved_report_opens_every_row(html):
    # paper and email can't be clicked, so every song's details start open
    assert html.count('<details class="song') == html.count(" open>")


def test_collapsed_row_shows_the_main_problem():
    from retrack.report import groups_html
    row = CatalogRow(title="Song", artist="Artist", isrc="QZ1", writers=[])
    out = groups_html([Result(row, RED, problems=["First thing.", "Second thing."], fixes=["Fix."])],
                      expanded=False)
    assert " open>" not in out
    assert "First thing." in out.split("</summary>")[0]   # visible without opening
    assert "and 1 more" in out


def test_green_song_says_nothing_to_fix(html):
    green = html[html.index("Late Night Drive"):]
    body = green.split("</summary>")[1]
    assert "Nothing to fix." in body  # in the details too, since printed rows hide the summary line


def test_sample_data_is_labelled_as_sample(html):
    assert "Sample data." in html
    real = render_html([], mock=False)
    assert "Sample data." not in real


def test_empty_catalog_still_renders():
    out = render_html([])
    assert "No songs to show." in out
    assert '<span class="figure">0</span>' in out


def test_catalog_text_is_escaped():
    row = CatalogRow(title="<script>alert(1)</script>", artist="A & B", isrc="QZ1", writers=[])
    out = render_html([Result(row, RED, problems=["x"], fixes=["y"])])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "A &amp; B" in out


# ---- It must work offline, in email and on paper -----------------------------------

def test_file_is_self_contained(html):
    lowered = html.lower()
    assert "<script" not in lowered         # no JavaScript at all
    assert "<link" not in lowered           # no external stylesheets or fonts
    assert "@import" not in lowered
    assert "url(" not in lowered            # no fonts or images fetched by CSS
    assert "<style>" in lowered             # the CSS is inside the file


def test_report_has_print_styles(html):
    assert "@media print" in html
    assert "break-inside: avoid" in html


def test_cli_html_flag_writes_the_file(tmp_path):
    out = tmp_path / "report.html"
    code = main([str(ROOT / "sample_catalog.csv"), "--mock",
                 "--out", str(tmp_path / "report.csv"), "--html", str(out)])
    assert code == 0
    assert "Your catalog at The MLC" in out.read_text(encoding="utf-8")


# ---- The preview server -----------------------------------------------------------

@pytest.fixture
def running_server():
    server = preview.start_server("<p>hello report</p>", port=0)  # port 0 = any free port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_server_only_listens_on_this_computer(running_server):
    assert running_server.server_address[0] == "127.0.0.1"


def test_server_serves_the_report(running_server):
    port = running_server.server_port
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]
        assert resp.read().decode() == "<p>hello report</p>"


def test_server_does_not_share_other_files(running_server):
    port = running_server.server_port
    for path in ("/.env", "/retrack/report.py", "/../.env"):
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")
        assert err.value.code == 404


def test_serve_opens_the_browser_and_stops_on_ctrl_c(monkeypatch):
    def press_ctrl_c(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(preview.ThreadingHTTPServer, "serve_forever", press_ctrl_c)
    opened = []
    assert preview.serve("<p>hi</p>", port=0, open_browser=opened.append) == 0
    assert opened and opened[0].startswith("http://localhost:")


def test_serve_explains_a_busy_port(capsys):
    busy = preview.start_server("<p>first</p>", port=0)
    try:
        code = preview.serve("<p>second</p>", port=busy.server_port, open_browser=None)
    finally:
        busy.server_close()
    assert code == 1
    assert "already in use" in capsys.readouterr().out
