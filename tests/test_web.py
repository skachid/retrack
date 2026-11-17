"""Tests for the web UI (python -m retrack.web).

The flow: upload -> columns -> scan -> results -> fix pack .zip, plus the
rules around it: 127.0.0.1 only, files deleted (on stop, on start, on
request), no attestation no fix pack, plain-English errors, the sample-data
banner, and privacy text that matches exactly what a scan sends.

Nothing here needs an API key or the network: The MLC and Claude are faked.
"""

import io
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from retrack import web
from retrack import web_pages as pages
from retrack.checker import load_catalog
from retrack.compliance import ATTESTATION_TEXT, COMPLIANCE_TEXT
from retrack.fixes import price_line
from retrack.mlc_client import MLCAuthError, MLCError, MockClient
from retrack.name_matcher import ClaudeNameMatcher, MockNameMatcher
from retrack.privacy import ISRCS, SONG_CODES, TITLES, WRITER_NAMES, data_flows, privacy_items, privacy_text
from retrack.sessions import SessionStore
from retrack.work_search import ClaudeWorkSearcher, MockWorkSearcher

ROOT = Path(__file__).parent.parent
SAMPLE = (ROOT / "sample_catalog.csv").read_bytes()
MESSY = (ROOT / "fixtures" / "import" / "label_catalog_messy.xlsx").read_bytes()
BANNER = "Sample data, not real results."


# ---- Helpers: a server on a free port, and a browser that doesn't follow redirects ----------

def start(tmp_path, make_client=MockClient, mock=True, **kw):
    srv = web.start_server(make_client, mock=mock, port=0, sessions=SessionStore(tmp_path / "sessions"), **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def server(tmp_path):
    srv = start(tmp_path)
    yield srv
    srv.shutdown()
    srv.server_close()
    srv.sessions.delete_all()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def call(srv, path, data=None, content_type=None, headers=None):
    """Returns (status, headers, body bytes)."""
    h = dict(headers or {})
    if content_type:
        h["Content-Type"] = content_type
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}", data=data, headers=h)
    try:
        resp = _OPENER.open(req)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def text(srv, path, **kw):
    status, _, body = call(srv, path, **kw)
    return status, body.decode()


def multipart(fields):
    """fields: list of (name, value) where value is text or (filename, bytes)."""
    boundary = "retrack-test-boundary"
    out = b""
    for name, value in fields:
        if isinstance(value, tuple):
            filename, data = value
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
        else:
            out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def form(pairs):
    return urllib.parse.urlencode(pairs).encode(), "application/x-www-form-urlencoded"


def upload(srv, filename="catalog.csv", data=SAMPLE, headers=None):
    body, ctype = multipart([("catalog", (filename, data))])
    return call(srv, "/upload", body, ctype, headers)


def upload_ok(srv, filename="catalog.csv", data=SAMPLE):
    status, headers, _ = upload(srv, filename, data)
    assert status == 303, "upload should redirect to the columns page"
    return headers["Location"].split("/")[2]


SAMPLE_MAPPING = [("map_title", "0"), ("map_artist", "1"), ("map_isrc", "2"), ("map_writers", "3")]


def scan(srv, sid, mapping=SAMPLE_MAPPING, smart_names=False, ai_search=False):
    pairs = list(mapping) + ([("smart_names", "on")] if smart_names else []) + \
        ([("ai_search", "on")] if ai_search else [])
    return call(srv, f"/s/{sid}/scan", *form(pairs))


def wait_done(srv, sid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        _, _, body = call(srv, f"/s/{sid}/status")
        status = json.loads(body)
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("scan never finished")


def scanned(srv, **kw):
    sid = upload_ok(srv)
    status, headers, _ = scan(srv, sid, **kw)
    assert status == 303 and headers["Location"].endswith("/progress")
    assert wait_done(srv, sid)["state"] == "done"
    return sid


def fixable_ids(page):
    import re
    return re.findall(r'name="song" value="(\d+)"', page)


# ---- The whole flow --------------------------------------------------------------------------

def test_full_flow_upload_columns_scan_report_fix_pack_download(server):
    # 1. upload a messy .xlsx (title rows, merged cells, odd headers)
    sid = upload_ok(server, "label_catalog_messy.xlsx", MESSY)

    # 2. the columns page shows what the importer's rules found, for the user to confirm
    status, page = text(server, f"/s/{sid}/columns")
    assert status == 200
    assert '<option value="1" selected>B: Song Name</option>' in page
    assert '<option value="3" selected>D: ISRC code</option>' in page
    assert 'name="map_writers" value="4" checked' in page
    assert 'id="smart_names" name="smart_names" value="on">' in page  # off by default
    assert 'id="ai_search" name="ai_search" value="on">' in page

    # 3. scan with AI search on
    status, headers, _ = scan(server, sid, [("map_title", "1"), ("map_artist", "2"), ("map_isrc", "3"),
                                            ("map_writers", "4")], ai_search=True)
    assert status == 303
    assert wait_done(server, sid)["state"] == "done"

    # 4. the report, with import notes and the songs to tick
    status, page = text(server, f"/s/{sid}/results")
    assert status == 200
    assert "Results for label_catalog_messy.xlsx" in page
    assert "notes from reading your file" in page
    assert COMPLIANCE_TEXT in page
    ids = fixable_ids(page)
    assert ids

    # 5. tick songs -> the review page shows "N songs × $X" from price_line
    status, page = text(server, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                        content_type="application/x-www-form-urlencoded")
    assert status == 200
    assert f"{len(ids)} songs × $" in page
    assert ATTESTATION_TEXT in page and COMPLIANCE_TEXT in page

    # 6. confirm and download the .zip
    body, ctype = multipart([("song", i) for i in ids] + [("attest", "yes")])
    status, headers, data = call(server, f"/s/{sid}/fix/download", body, ctype)
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert "attachment" in headers["Content-Disposition"]
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "fix_pack/README.md" in names and "fix_pack/ATTESTATION.txt" in names
    attestation = zipfile.ZipFile(io.BytesIO(data)).read("fix_pack/ATTESTATION.txt").decode()
    assert "ticked the confirmation box on the Retrack page" in attestation


def test_corrected_mapping_is_what_gets_checked(server):
    sid = upload_ok(server)
    # point "title" at the artist column on purpose: the results must follow the user's choice
    scan(server, sid, [("map_title", "1"), ("map_artist", "1"), ("map_isrc", "2"), ("map_writers", "3")])
    wait_done(server, sid)
    s = server.sessions.get(sid)
    assert {r.row.title for r in s.results} == {"Nova Reyes", "The Lowlights"}


def test_web_checks_the_same_catalog_the_cli_would(server):
    sid = scanned(server)
    s = server.sessions.get(sid)
    assert [r.row for r in s.results] == load_catalog(ROOT / "sample_catalog.csv")


# ---- No attestation, no fix pack --------------------------------------------------------------

def test_download_without_attestation_is_refused_and_makes_no_files(server):
    sid = scanned(server, ai_search=True)
    _, page = text(server, f"/s/{sid}/results")
    ids = fixable_ids(page)
    folder = server.sessions.get(sid).folder
    before = sorted(p.name for p in folder.iterdir())

    for extra in ([], [("attest", "no")], [("attest", "")]):
        body, ctype = multipart([("song", i) for i in ids] + extra)
        status, headers, data = call(server, f"/s/{sid}/fix/download", body, ctype)
        assert status == 400
        assert headers["Content-Type"].startswith("text/html")
        assert b"No confirmation, no fix pack" in data
    assert sorted(p.name for p in folder.iterdir()) == before  # nothing was written


def test_no_songs_ticked_explains(server):
    sid = scanned(server)
    status, page = text(server, f"/s/{sid}/fix", data=b"", content_type="application/x-www-form-urlencoded")
    assert status == 400 and "Tick at least one song" in page


def test_price_on_the_page_comes_from_price_line(server):
    sid = scanned(server, ai_search=True)
    _, page = text(server, f"/s/{sid}/results")
    import html, re
    lines = json.loads(html.unescape(re.search(r'data-lines="([^"]*)"', page).group(1)))
    results = server.sessions.get(sid).results
    from retrack.fixes import fix_kind
    fixable = [r for r in results if fix_kind(r)]
    assert lines == [price_line(fixable[:n]) for n in range(len(fixable) + 1)]


# ---- Progress ---------------------------------------------------------------------------------

def test_progress_text_and_page_work_without_javascript(tmp_path):
    store = SessionStore(tmp_path / "s")
    s = store.create("c.csv", SAMPLE)
    s.rows, s.done, s.state = [object()] * 120, 36, "running"
    assert pages.progress_text(s) == "Checking 37 of 120…"
    page = pages.progress_page(s)
    assert "Checking 37 of 120…" in page
    assert '<meta http-equiv="refresh" content="2;url=/s/' in page  # refreshes itself, no JS needed
    assert '<progress id="progress-bar" max="120" value="36">' in page


def test_status_reports_progress_while_running(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SlowClient(MockClient):
        def search_recordings(self, isrc):
            started.set()
            release.wait(5)
            return super().search_recordings(isrc)

    srv = start(tmp_path, SlowClient)
    try:
        sid = upload_ok(srv)
        scan(srv, sid)
        started.wait(5)
        status = json.loads(call(srv, f"/s/{sid}/status")[2])
        assert status["state"] == "running" and status["total"] == 5
        assert status["text"] == "Checking 1 of 5…"
        _, page = text(srv, f"/s/{sid}/progress")
        assert "Checking 1 of 5…" in page
        assert text(srv, f"/s/{sid}/results")[0] == 303  # no results until it's done
        release.set()
        assert wait_done(srv, sid)["next"].endswith("/results")
    finally:
        release.set()
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


# ---- Errors, in plain English, with what to do next -------------------------------------------

@pytest.mark.parametrize("filename,data,expected", [
    ("catalog.pdf", b"%PDF-1.4", "Retrack needs the spreadsheet itself"),
    ("catalog.xls", b"\xd0\xcf\x11\xe0", "older Excel file"),
    ("catalog.numbers", b"xx", "In Numbers choose File, Export To"),
    ("catalog.xlsx", b"not really excel", "open catalog.xlsx as an Excel file"),
    ("catalog.csv", b"just one word\n", "find a header row"),
    ("catalog.csv", b"title,artist,isrc,writers\n", "headers but no songs"),
])
def test_bad_files_are_explained(server, filename, data, expected):
    status, _, body = upload(server, filename, data)
    assert status == 400
    page = body.decode()
    assert 'role="alert"' in page and expected in page
    assert list((server.sessions.root).iterdir()) == []  # a file we can't use isn't kept


def test_no_file_chosen(server):
    body, ctype = multipart([("catalog", ("", b""))])
    status, page = text(server, "/upload", data=body, content_type=ctype)
    assert status == 400 and "Choose a .csv or .xlsx file" in page


def test_too_big_is_refused(server, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD_BYTES", 1000)
    status, _, body = upload(server, data=b"x" * 5000)
    assert status == 400 and "too big" in body.decode()


def test_no_isrc_column_chosen(server):
    sid = upload_ok(server)
    status, _, body = scan(server, sid, [("map_title", "0"), ("map_isrc", "")])
    assert status == 400
    assert "Pick the missing columns" in body.decode() and "ISRC" in body.decode()


def test_no_valid_isrcs(server):
    sid = upload_ok(server)
    status, _, body = scan(server, sid, [("map_title", "0"), ("map_isrc", "1")])  # artist column as ISRC
    assert status == 400 and "No songs with a valid ISRC" in body.decode()


def test_too_many_songs(server, monkeypatch):
    monkeypatch.setattr(web, "MAX_SONGS", 2)
    sid = upload_ok(server)
    status, _, body = scan(server, sid)
    assert status == 400 and "Retrack checks up to 2 at a time" in body.decode()


class LoginRefused(MockClient):
    def search_recordings(self, isrc):
        raise MLCAuthError("Login failed (401)")


class MLCDown(MockClient):
    def search_recordings(self, isrc):
        raise MLCError("POST /search/recordings failed (503)")


@pytest.mark.parametrize("client,expected", [
    (LoginRefused, "Retrack couldn’t log in to The MLC"),
    (MLCDown, "The MLC didn’t answer"),
])
def test_mlc_problems_are_explained(tmp_path, client, expected):
    srv = start(tmp_path, client, mock=False, make_name_matcher=MockNameMatcher,
                make_work_searcher=MockWorkSearcher)
    try:
        sid = upload_ok(srv)
        scan(srv, sid)
        assert wait_done(srv, sid)["state"] == "failed"
        status, page = text(srv, f"/s/{sid}/progress")
        assert status == 502 and expected in page
        assert "Your file is fine" in page or "Nothing is wrong with your file" in page
        assert "Back to the columns" in page
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_claude_not_set_up_is_explained_before_scanning(tmp_path):
    def no_claude():
        raise RuntimeError("no ANTHROPIC_API_KEY")

    srv = start(tmp_path, mock=False, make_name_matcher=no_claude, make_work_searcher=no_claude)
    try:
        sid = upload_ok(srv)
        status, _, body = scan(srv, sid, smart_names=True)
        assert status == 503 and "Claude isn’t set up on this computer" in body.decode()
        assert scan(srv, sid)[0] == 303  # with both checks off it goes ahead
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_claude_unavailable_during_scan_is_reported(tmp_path):
    class Down:
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(ConnectionError())))

    srv = start(tmp_path, mock=False, make_name_matcher=lambda: ClaudeNameMatcher(client=Down()),
                make_work_searcher=lambda: ClaudeWorkSearcher(client=Down()))
    try:
        sid = upload_ok(srv)
        scan(srv, sid, smart_names=True, ai_search=True)
        assert wait_done(srv, sid)["state"] == "done"  # the check still finishes
        _, page = text(srv, f"/s/{sid}/results")
        assert "Claude didn’t answer for" in page and "Nothing is wrong with your file" in page
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_unknown_or_deleted_session(server):
    status, page = text(server, "/s/aaaaaaaaaaaaaaaaaaaaaa/results")
    assert status == 404 and "This check has ended" in page


# ---- Empty states ---------------------------------------------------------------------------

def test_nothing_uploaded_yet_shows_the_upload(server):
    status, page = text(server, "/")
    assert status == 200
    assert 'action="/upload"' in page and 'accept=".csv,.xlsx' in page
    for song in ("Ghost Signal", "Late Night Drive"):
        assert song not in page


def test_zero_problems_says_nothing_to_fix(tmp_path):
    store = SessionStore(tmp_path / "s")
    s = store.create("c.csv", SAMPLE)
    from retrack.checker import check_catalog
    rows = [r for r in load_catalog(ROOT / "sample_catalog.csv") if r.title == "Late Night Drive"]
    s.rows, s.results, s.state = rows, check_catalog(rows, MockClient()), "done"
    page = pages.results_page(s)
    assert "Nothing to fix" in page
    assert 'id="fix-form"' not in page


# ---- Files: kept on this machine, deleted on request, on stop and on start ---------------------

def test_upload_is_kept_in_a_private_session_folder(server):
    sid = upload_ok(server)
    folder = server.sessions.get(sid).folder
    assert folder.parent == server.sessions.root
    assert (folder / "upload.csv").read_bytes() == SAMPLE
    if os.name == "posix":
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_delete_my_files_now(server):
    sid = scanned(server)
    _, page = text(server, f"/s/{sid}/results")
    assert "Delete my files now" in page
    folder = server.sessions.get(sid).folder
    status, headers, _ = call(server, f"/s/{sid}/delete", *form([]))
    assert status == 303 and headers["Location"] == "/?deleted=1"
    assert not folder.exists()
    assert text(server, f"/s/{sid}/results")[0] == 404
    assert "Your files were deleted from this computer." in text(server, "/?deleted=1")[1]


def test_leftover_sessions_are_deleted_on_start(tmp_path):
    root = tmp_path / "sessions"
    (root / "crashed-session").mkdir(parents=True)
    (root / "crashed-session" / "upload.csv").write_bytes(SAMPLE)
    (root / "stray.txt").write_text("x")
    SessionStore(root)
    assert root.exists() and list(root.iterdir()) == []


def test_files_are_deleted_when_the_server_stops(tmp_path, monkeypatch):
    monkeypatch.setattr("retrack.sessions.DEFAULT_ROOT", tmp_path / "default-root")
    seen = {}

    def fake_run(server, open_browser):  # stands in for "serve until Ctrl+C"
        session = server.sessions.create("c.csv", SAMPLE)
        seen["folder"] = session.folder
        assert session.folder.exists()
        return 0

    monkeypatch.setattr(web, "run_until_stopped", fake_run)
    assert web.main(["--mock", "--port", "0"], open_browser=None) == 0
    assert not seen["folder"].exists()
    assert not (tmp_path / "default-root").exists()


def test_closing_the_window_or_kill_also_cleans_up(tmp_path):
    """SIGTERM (kill) and SIGHUP (closing the terminal) stop the server like Ctrl+C does,
    so the uploaded files are deleted. A real process, started in the background."""
    import signal, subprocess, sys
    if not hasattr(signal, "SIGHUP"):
        pytest.skip("no SIGHUP on this system")
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        env = dict(os.environ, TMPDIR=str(tmp_path / sig.name))
        (tmp_path / sig.name).mkdir()
        code = ("import sys, tempfile, threading, os, signal; from retrack import web, sessions; "
                "sessions.DEFAULT_ROOT = __import__('pathlib').Path(tempfile.gettempdir()) / 'retrack-sessions'; "
                "web.run_until_stopped = lambda srv, ob: (srv.sessions.create('c.csv', b'x'), print('ready', flush=True), "
                "srv.serve_forever()); sys.exit(web.main(['--mock', '--port', '0'], open_browser=None))")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        assert proc.stdout.readline().strip() == b"ready"
        root = tmp_path / sig.name / "retrack-sessions"
        assert len(list(root.iterdir())) == 1
        proc.send_signal(sig)
        proc.wait(10)
        assert not root.exists(), f"{sig.name} left files behind"


# ---- Rules that always hold -------------------------------------------------------------------

def test_server_listens_on_this_computer_only(server):
    assert server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize("path", ["/upload", "/s/aaaaaaaaaaaaaaaaaaaaaa/scan", "/s/aaaaaaaaaaaaaaaaaaaaaa/delete",
                                  "/s/aaaaaaaaaaaaaaaaaaaaaa/fix/download"])
def test_posts_from_other_websites_are_refused(server, path):
    status, _, _ = call(server, path, b"", "application/x-www-form-urlencoded",
                        {"Origin": "https://some-other-site.example"})
    assert status == 403


def test_posts_from_the_page_itself_are_allowed(server):
    status, _, _ = upload(server, headers={"Origin": f"http://localhost:{server.server_port}"})
    assert status == 303


def test_other_files_are_not_served(server):
    for path in ("/.env", "/retrack/web.py", "/s/../../.env", "/fixtures/mlc_mock.json"):
        assert call(server, path)[0] == 404


def test_template_download_has_just_the_headers(server):
    status, headers, body = call(server, "/template.csv")
    assert body.decode() == "title,artist,isrc,writers\r\n"
    assert "attachment" in headers["Content-Disposition"]


def test_pages_load_nothing_from_the_internet_and_have_no_logos(server):
    sid = scanned(server)
    for path in ("/", f"/s/{sid}/columns", f"/s/{sid}/results"):
        page = text(server, path)[1].lower()
        assert "<link" not in page and "<script src" not in page and "@import" not in page
        assert "<img" not in page and "<svg" not in page and "logo" not in page


def test_compliance_text_on_report_and_download_pages(server):
    sid = scanned(server, ai_search=True)
    _, results = text(server, f"/s/{sid}/results")
    _, report = text(server, f"/s/{sid}/report.html")
    ids = fixable_ids(results)
    _, review = text(server, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                     content_type="application/x-www-form-urlencoded")
    for page in (results, report, review):
        assert COMPLIANCE_TEXT in page


def test_sample_data_banner_on_every_page_in_mock_mode(server):
    sid = scanned(server, ai_search=True)
    _, results = text(server, f"/s/{sid}/results")
    ids = fixable_ids(results)
    pages_seen = [
        text(server, "/")[1],
        text(server, f"/s/{sid}/columns")[1],
        results,
        text(server, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
             content_type="application/x-www-form-urlencoded")[1],
        text(server, "/s/aaaaaaaaaaaaaaaaaaaaaa/results")[1],  # the error page too
        upload(server, "x.pdf", b"x")[2].decode(),
    ]
    for page in pages_seen:
        assert BANNER in page
    store = SessionStore(server.sessions.root.parent / "other")
    s = store.create("c.csv", SAMPLE)
    s.rows, s.state = [object()] * 3, "running"
    assert BANNER in pages.progress_page(s, mock=True)
    assert BANNER not in pages.home_page(mock=False)


def test_both_toggles_are_off_by_default_with_one_sentence_each(server):
    sid = upload_ok(server)
    _, page = text(server, f"/s/{sid}/columns")
    import html
    assert '<input type="checkbox" id="smart_names" name="smart_names" value="on">' in page  # not checked
    assert '<input type="checkbox" id="ai_search" name="ai_search" value="on">' in page
    shown = html.unescape(page)
    for sentence in (pages.SMART_NAMES_TEXT, pages.AI_SEARCH_TEXT):
        assert sentence in shown
        assert sentence.count(".") == 1 and "Claude" in sentence  # one plain sentence, naming Claude


# ---- Privacy text matches exactly what a scan sends --------------------------------------------

class RecordingMLC(MockClient):
    """The mock MLC, noting what kind of data each request carried."""

    def __init__(self):
        super().__init__()
        self.sent = set()

    def search_recordings(self, isrc):
        self.sent.add(ISRCS)
        return super().search_recordings(isrc)

    def get_works(self, codes):
        if codes:
            self.sent.add(SONG_CODES)
        return super().get_works(codes)

    def search_songcode(self, title, writers=()):
        self.sent.add(TITLES)
        if writers:
            self.sent.add(WRITER_NAMES)
        return super().search_songcode(title, writers)


class RecordingClaude:
    """A fake Claude API that notes what each request carried and answers 'no'."""

    def __init__(self):
        self.sent = set()
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        system, content = kw["system"], kw["messages"][0]["content"]
        if "same person" in system:
            self.sent.add(WRITER_NAMES)
            answer = {"same_person": False, "confidence": 0.9, "reason": "different"}
        elif "Suggest other searches" in system:
            self.sent |= {TITLES, WRITER_NAMES}
            answer = {"searches": []}
        else:
            self.sent |= {TITLES, WRITER_NAMES}
            answer = {"same_song": False, "confidence": 0.9, "reason": "different"}
        assert "QZABC" not in content  # never an ISRC
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(answer))])


@pytest.mark.parametrize("smart_names", [False, True])
@pytest.mark.parametrize("ai_search", [False, True])
def test_privacy_text_matches_what_a_scan_actually_sends(tmp_path, smart_names, ai_search):
    mlc, claude = RecordingMLC(), RecordingClaude()
    srv = start(tmp_path, lambda: mlc, mock=False,
                make_name_matcher=lambda: ClaudeNameMatcher(client=claude),
                make_work_searcher=lambda: ClaudeWorkSearcher(client=claude))
    try:
        sid = upload_ok(srv)
        scan(srv, sid, smart_names=smart_names, ai_search=ai_search)
        assert wait_done(srv, sid)["state"] == "done"
        _, results = text(srv, f"/s/{sid}/results")
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()

    declared = data_flows(smart_names, ai_search)
    assert mlc.sent == declared.mlc          # exactly what The MLC received
    assert claude.sent == declared.claude    # exactly what Claude received

    # ...and the words on the page say exactly that
    import html
    shown = html.unescape(results)
    for heading, sentence in privacy_items(smart_names, ai_search):
        assert heading in shown and sentence in shown
    claude_line = next(h for h, _ in privacy_items(smart_names, ai_search) if "Claude" in h)
    for kind in (ISRCS, SONG_CODES, TITLES, WRITER_NAMES):
        assert (kind in claude_line) == (kind in declared.claude)


def test_columns_page_shows_the_privacy_text_for_each_toggle_setting(server):
    sid = upload_ok(server)
    _, page = text(server, f"/s/{sid}/columns")
    import html, re
    for sn in (False, True):
        for ai in (False, True):
            block = re.search(rf'<ul class="privacy-list" data-privacy="{int(sn)}{int(ai)}"[^>]*>(.*?)</ul>', page).group(1)
            for heading, sentence in privacy_items(sn, ai, mock=True):
                assert heading in html.unescape(block) and sentence in html.unescape(block)
    # only the list for the current settings (both off) is visible without CSS or JS
    lists = re.findall(r'<ul class="privacy-list" data-privacy="(\d\d)"( hidden)?>', page)
    assert lists == [("00", ""), ("01", " hidden"), ("10", " hidden"), ("11", " hidden")]


def test_mock_mode_sends_nothing_and_says_so(tmp_path, monkeypatch):
    def must_not_run(*a, **kw):
        raise AssertionError("mock mode must not create a Claude client")

    monkeypatch.setattr(web, "ClaudeNameMatcher", must_not_run)
    monkeypatch.setattr(web, "ClaudeWorkSearcher", must_not_run)
    monkeypatch.setattr(web, "MLCClient", must_not_run)
    assert data_flows(True, True, mock=True).mlc == frozenset()
    assert data_flows(True, True, mock=True).claude == frozenset()
    assert "Nothing goes to The MLC." in privacy_text(True, True, mock=True)
    assert "Nothing goes to Claude." in privacy_text(True, True, mock=True)
    srv = start(tmp_path)  # mock=True: the mock matchers are used, never Claude's
    try:
        sid = upload_ok(srv)
        scan(srv, sid, smart_names=True, ai_search=True)  # both checks on, still nothing real runs
        assert wait_done(srv, sid)["state"] == "done"
        _, page = text(srv, f"/s/{sid}/results")
        assert "Nothing goes to Claude." in page and "Nothing goes to The MLC." in page
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_without_mlc_login_it_explains_and_exits(monkeypatch, capsys):
    monkeypatch.delenv("MLC_USERNAME", raising=False)
    monkeypatch.delenv("MLC_PASSWORD", raising=False)
    assert web.main([], open_browser=None) == 1
    assert "--mock" in capsys.readouterr().err
