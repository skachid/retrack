"""Tests for the homepage (python -m retrack.web): upload a CSV, get results on the same page."""

import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from retrack import web
from retrack.checker import parse_catalog
from retrack.mlc_client import MLCError, MockClient
from retrack.name_matcher import MockNameMatcher

ROOT = Path(__file__).parent.parent
SAMPLE = (ROOT / "sample_catalog.csv").read_bytes()


def multipart(filename, data, field="catalog"):
    """Build a file upload body the way a browser does. Returns (content_type, body)."""
    boundary = "retrack-test-boundary"
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


# ---- The homepage itself ---------------------------------------------------------------

def test_homepage_has_upload_form_and_steps():
    page = web.render_home()
    assert 'enctype="multipart/form-data"' in page
    assert 'name="catalog" type="file"' in page
    assert page.count("How it works") == 2      # the top link and the section heading
    assert 'href="#how"' in page and 'id="how"' in page
    assert page.count('<li><h3>') == 4         # the four steps


def test_homepage_shows_no_test_data():
    page = web.render_home(mock=True)
    for song in ("Ghost Signal", "Paper Crowns", "Late Night Drive", "Nova Reyes"):
        assert song not in page
    assert "Demo mode." in page              # but it does say it's in demo mode
    assert "Demo mode." not in web.render_home(mock=False)


def test_homepage_loads_nothing_from_the_internet():
    page = web.render_home().lower()
    assert "<link" not in page
    assert "<script src" not in page
    assert "@import" not in page


# ---- Reading uploads -------------------------------------------------------------------

def test_read_upload_finds_the_file():
    content_type, body = multipart("my catalog.csv", SAMPLE)
    assert web.read_upload(content_type, body) == ("my catalog.csv", SAMPLE)


def test_read_upload_without_a_file_explains():
    content_type, body = multipart("x.csv", b"")
    with pytest.raises(web.UploadError, match="Choose a CSV file"):
        web.read_upload(content_type, body)


def test_excel_file_gets_a_helpful_message():
    with pytest.raises(web.UploadError, match="Excel workbook"):
        web.decode_csv(b"PK\x03\x04 rest of a zip file")


def test_windows_excel_csv_is_read():
    text = "title,artist,isrc,writers\nCafé,Zoë,QZ1,José\n".encode("cp1252")
    assert "Café" in web.decode_csv(text)


def test_parse_catalog_from_text_with_excel_bom():
    rows = parse_catalog("﻿title,artist,isrc,writers\nSong,Artist,qz-abc-24-00001,A; B\n")
    assert rows[0].isrc == "QZABC2400001"
    assert rows[0].writers == ["A", "B"]


def test_missing_column_is_explained_in_plain_words():
    with pytest.raises(web.UploadError, match="no writers column"):
        web.check_upload("x.csv", b"title,artist,isrc\nSong,Artist,QZ1\n", MockClient)


def test_too_many_songs_is_refused(monkeypatch):
    monkeypatch.setattr(web, "MAX_SONGS", 2)
    with pytest.raises(web.UploadError, match="up to 2"):
        web.check_upload("x.csv", SAMPLE, MockClient)


# ---- The server, end to end ------------------------------------------------------------

class FailingClient:
    def search_recordings(self, isrc):
        raise MLCError("timeout")

    def get_works(self, codes):
        return []


@pytest.fixture
def server():
    srv = web.start_server(MockClient, MockNameMatcher(), mock=True, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def post(srv, data=SAMPLE, filename="mine.csv", headers=None):
    content_type, body = multipart(filename, data)
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/check", data=body,
                                 headers={"Content-Type": content_type, **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_server_listens_on_this_computer_only(server):
    assert server.server_address[0] == "127.0.0.1"


def test_upload_shows_results_on_the_same_page(server):
    status, page = post(server)
    assert status == 200
    assert "Find the royalties your songs are missing." in page  # still the homepage
    assert "Results for mine.csv" in page
    assert page.index("Ghost Signal") < page.index("Late Night Drive")  # red before green


def test_results_on_the_page_start_collapsed_with_filters(server):
    _, fragment = post(server, headers={"X-Retrack-Fragment": "1"})
    assert fragment.count('<details class="song') == 5
    assert " open>" not in fragment
    assert 'data-filter="red"' in fragment and 'data-filter="green"' in fragment
    assert '<div class="filters" role="group" aria-label="Show songs" hidden>' in fragment  # off until JS


def test_script_gets_just_the_results(server):
    status, fragment = post(server, headers={"X-Retrack-Fragment": "1"})
    assert status == 200
    assert "Results for mine.csv" in fragment
    assert "<html" not in fragment


def test_bad_file_shows_an_error_not_a_crash(server):
    status, fragment = post(server, data=b"name,code\nx,y\n", headers={"X-Retrack-Fragment": "1"})
    assert status == 400
    assert 'role="alert"' in fragment


def test_mlc_outage_says_the_file_is_fine():
    srv = web.start_server(FailingClient, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, page = post(srv)
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 502
    assert "Nothing is wrong with your file" in page


def test_uploads_from_other_websites_are_refused(server):
    status, _ = post(server, headers={"Origin": "https://some-other-site.example"})
    assert status == 403
    status, _ = post(server, headers={"Origin": f"http://localhost:{server.server_port}"})
    assert status == 200


def test_template_download_has_just_the_headers(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/template.csv") as resp:
        assert resp.read().decode() == "title,artist,isrc,writers\r\n"
        assert "attachment" in resp.headers["Content-Disposition"]


def test_other_files_are_not_served(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/.env")
    assert err.value.code == 404


def test_without_mlc_login_it_explains_and_exits(monkeypatch, capsys):
    monkeypatch.delenv("MLC_USERNAME", raising=False)
    monkeypatch.delenv("MLC_PASSWORD", raising=False)
    assert web.main([], open_browser=None) == 1
    assert "--mock" in capsys.readouterr().err
