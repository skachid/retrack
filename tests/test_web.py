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
from pathlib import Path
from types import SimpleNamespace

import pytest

from retrack import web
from retrack import web_pages as pages
from retrack.checker import load_catalog
from retrack.compliance import ATTESTATION_TEXT, COMPLIANCE_TEXT
from retrack.fixes import price_line, price_parts
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


def columns(srv, sid, mapping=SAMPLE_MAPPING):
    return call(srv, f"/s/{sid}/columns", *form(mapping))


def check_pairs(smart_names=False, ai_search=False):
    """The pay step's two extra-check boxes, as form fields."""
    return ([("smart_names", "on")] if smart_names else []) + ([("ai_search", "on")] if ai_search else [])


def scan(srv, sid, mapping=SAMPLE_MAPPING, smart_names=False, ai_search=False):
    """Confirm the columns, which starts the free check. The extra checks are chosen on
    the pay step, so they're remembered here and sent by pay_all()."""
    if not hasattr(srv, "checks"):
        srv.checks = {}
    srv.checks[sid] = check_pairs(smart_names, ai_search)
    return columns(srv, sid, mapping)


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


def open_fixes(srv, sid):
    """GET the fix page. The first visit after paying runs the AI checks on the paid
    songs first ("Working out…"), so follow that until it's done. Returns (status, page)."""
    status, headers, body = call(srv, f"/s/{sid}/fixes")
    if status == 303 and headers["Location"].endswith("/progress"):
        assert wait_done(srv, sid)["state"] == "done"
        status, headers, body = call(srv, f"/s/{sid}/fixes")
    return status, body.decode()


def pay_all(srv, sid, ids=None):
    """Tick every song on offer (or just `ids`) and pay. Tests have no Stripe keys: Pay (demo)."""
    ids = ids if ids is not None else fixable_ids(text(srv, f"/s/{sid}/results")[1])
    extra = getattr(srv, "checks", {}).get(sid, [])
    status, headers, _ = call(srv, f"/s/{sid}/pay", *form([("song", i) for i in ids] + extra + [("attest", "yes")]))
    assert status == 303 and headers["Location"].endswith("/fixes")
    return ids


def pay_step(srv, sid):
    """Tick every song on offer and open the pay step. Returns its HTML."""
    ids = fixable_ids(text(srv, f"/s/{sid}/results")[1])
    status, page = text(srv, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                        content_type="application/x-www-form-urlencoded")
    assert status == 200
    return page


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
    assert '<option value="4" selected>E: Composer(s)</option>' in page  # writers: a dropdown too
    assert 'id="smart_names"' not in page  # the extra checks live on the pay step
    assert "The free check looks up each song at The MLC. No AI is used." in page
    assert "Start the free check" in page

    # 3. confirming the columns starts the free check straight away: no separate Check step
    status, headers, _ = columns(server, sid, [("map_title", "1"), ("map_artist", "2"), ("map_isrc", "3"),
                                               ("map_writers", "4")])
    assert status == 303 and headers["Location"].endswith("/progress")
    assert wait_done(server, sid)["state"] == "done"

    # 4. the report, with import notes and the songs to tick
    status, page = text(server, f"/s/{sid}/results")
    assert status == 200
    assert "Results for label_catalog_messy.xlsx" in page
    assert "notes from reading your file" in page
    assert COMPLIANCE_TEXT in page
    ids = fixable_ids(page)
    assert ids

    # 5. tick songs -> the pay page shows "N songs × $X = $Total" from price_line
    status, page = text(server, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                        content_type="application/x-www-form-urlencoded")
    assert status == 200
    assert f"{len(ids)} songs × $" in page
    assert ATTESTATION_TEXT in page and COMPLIANCE_TEXT in page
    assert "Pay (demo)" in page  # --mock: no Stripe

    assert 'id="ai_search"' not in page  # the add-ons are chosen on the results page, not here

    # 6. confirm and pay (demo) -> the on-screen fix pack
    status, headers, _ = call(server, f"/s/{sid}/pay", *form([("song", i) for i in ids]
                                                             + check_pairs(ai_search=True) + [("attest", "yes")]))
    assert status == 303 and headers["Location"].endswith("/fixes")
    status, page = open_fixes(server, sid)
    assert status == 200
    assert "Your fixes stay here for 60 minutes, then your files are deleted for your privacy." in page
    assert "You must fill this in" in page and COMPLIANCE_TEXT in page

    # 7. the one main download
    status, headers, data = call(server, f"/s/{sid}/fixes.xlsx")
    assert status == 200 and "Retrack_Fixes_" in headers["Content-Disposition"]
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Summary", "Register", "Link", "Claim", "Confirmation"]
    assert "ticked the confirmation box on the Retrack page" in [c.value for c in wb["Confirmation"]["B"]]


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

def test_paying_without_attestation_is_refused_and_unlocks_nothing(server):
    sid = scanned(server, ai_search=True)
    _, page = text(server, f"/s/{sid}/results")
    ids = fixable_ids(page)
    folder = server.sessions.get(sid).folder
    before = sorted(p.name for p in folder.iterdir())

    for extra in ([], [("attest", "no")], [("attest", "")]):
        status, headers, data = call(server, f"/s/{sid}/pay", *form([("song", i) for i in ids] + extra))
        assert status == 400
        assert headers["Content-Type"].startswith("text/html")
        assert b"No confirmation, no fix pack" in data
    s = server.sessions.get(sid)
    assert s.paid_songs == [] and s.attestation is None
    assert call(server, f"/s/{sid}/fixes")[0] == 402
    assert sorted(p.name for p in folder.iterdir()) == before  # nothing was written


def test_no_songs_ticked_explains(server):
    sid = scanned(server)
    status, page = text(server, f"/s/{sid}/fix", data=b"", content_type="application/x-www-form-urlencoded")
    assert status == 400 and "Tick at least one song" in page


def test_price_on_the_page_comes_from_price_line(server):
    sid = scanned(server)
    _, page = text(server, f"/s/{sid}/results")
    import html, re
    lines = json.loads(html.unescape(re.search(r'data-lines="([^"]*)"', page).group(1)))
    results = server.sessions.get(sid).results
    from retrack.fixes import sold_kind
    fixable = [r for r in results if sold_kind(r)]  # red songs too: worked out after checkout
    expected = {}
    for n in range(len(fixable) + 1):
        for ai in (False, True):
            for sn in (False, True):
                addons = [a for a, on in (("ai_search", ai), ("smart_names", sn)) if on]
                expected[f"{n}|{int(ai)}{int(sn)}"] = price_parts(fixable[:n], addons)
    assert lines == expected  # every song count with every add-on combination
    assert lines["4|11"] == ["4 songs × $5.00 = $20.00", "AI search $2.00", "Smart names $2.00", "Total $24.00"]
    assert "·" not in page.split('<section class="fix"')[1]  # one part per line, never joined by dots


# ---- Progress ---------------------------------------------------------------------------------

def test_progress_text_and_page_work_without_javascript(tmp_path):
    store = SessionStore(tmp_path / "s")
    s = store.create("c.csv", SAMPLE)
    s.rows, s.todo, s.done, s.state = [object()] * 120, list(range(120)), 36, "running"
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
    status, _, body = columns(server, sid, [("map_title", "0"), ("map_isrc", "")])
    assert status == 400
    assert "Pick the missing columns" in body.decode() and "ISRC" in body.decode()


def test_no_valid_isrcs(server):
    sid = upload_ok(server)
    status, _, body = columns(server, sid, [("map_title", "0"), ("map_isrc", "1")])  # artist column as ISRC
    assert status == 400 and "No songs with a valid ISRC" in body.decode()


def test_too_many_songs(server, monkeypatch):
    monkeypatch.setattr(web, "MAX_SONGS", 2)
    sid = upload_ok(server)
    status, _, body = columns(server, sid)
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
    srv = start(tmp_path, client, mock=False)
    try:
        sid = upload_ok(srv)
        scan(srv, sid)
        assert wait_done(srv, sid)["state"] == "failed"
        status, page = text(srv, f"/s/{sid}/progress")
        assert status == 502 and expected in page
        assert "Your file is fine" in page or "Nothing is wrong with your file" in page
        assert "Try the check again" in page and f'action="/s/{sid}/scan"' in page
        # ...and trying again really runs the check again (here The MLC still fails)
        status, headers, _ = call(srv, f"/s/{sid}/scan", *form([]))
        assert status == 303 and headers["Location"].endswith("/progress")
        assert wait_done(srv, sid)["state"] == "failed"
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_claude_not_set_up_is_explained_before_paying(tmp_path):
    def no_claude():
        raise RuntimeError("no ANTHROPIC_API_KEY")

    # The AI runs only after checkout, but a missing key is reported on the pay step, not after paying.
    srv = start(tmp_path, mock=False, make_claude=no_claude)
    try:
        sid = scanned(srv)  # the free check never needs the AI
        ids = fixable_ids(text(srv, f"/s/{sid}/results")[1])
        songs = [("song", i) for i in ids]
        # Reported as soon as the add-on is chosen on the results page, with the ticks kept...
        status, _, body = call(srv, f"/s/{sid}/fix", *form(songs + check_pairs(smart_names=True)))
        page = body.decode()
        assert status == 503 and "The AI checks aren’t set up on this computer" in page
        assert 'id="smart_names" name="smart_names" value="on" data-addon="smart_names" checked' in page
        # ...and again on the pay step, before any payment.
        status, _, body = call(srv, f"/s/{sid}/pay", *form(songs + check_pairs(smart_names=True)
                                                           + [("attest", "yes")]))
        assert status == 503 and "The AI checks aren’t set up on this computer" in body.decode()
        assert not srv.sessions.get(sid).paid_songs  # nothing unlocked
        status, headers, _ = call(srv, f"/s/{sid}/pay", *form(songs + [("attest", "yes")]))
        assert status == 303 and headers["Location"].endswith("/fixes")  # with both checks off it goes ahead
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_claude_unavailable_after_checkout_is_reported(tmp_path):
    class Down:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(ConnectionError()))

    srv = start(tmp_path, mock=False, make_claude=Down)
    try:
        sid = scanned(srv, smart_names=True, ai_search=True)
        pay_all(srv, sid)
        status, page = open_fixes(srv, sid)  # the after-checkout checks still finish
        assert status == 200 and "The AI service didn’t answer for" in page
        unresolved = page.split('data-status="unresolved"')[1].split("</section>")[0]
        assert "Ghost Signal" in unresolved and "Blue Hour" in unresolved  # nothing guessed: by hand
        assert "Couldn’t finish the automatic check; here’s how to check by hand." in unresolved
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


def test_colour_note_lists_each_colour_with_its_meaning_in_words():
    page = pages.home_page()
    assert '<div class="is-red"><dt>Red</dt><dd>Money is probably stuck</dd></div>' in page
    assert '<div class="is-yellow"><dt>Yellow</dt><dd>Registered, but something is off</dd></div>' in page
    assert '<div class="is-green"><dt>Green</dt><dd>Registered and fully claimed</dd></div>' in page


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
                                  "/s/aaaaaaaaaaaaaaaaaaaaaa/pay", "/s/aaaaaaaaaaaaaaaaaaaaaa/template"])
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
    s.rows, s.todo, s.state = [object()] * 3, [0, 1, 2], "running"
    assert BANNER in pages.progress_page(s, mock=True)
    assert BANNER not in pages.home_page(mock=False)


def test_both_add_ons_are_off_by_default_with_one_sentence_each(server):
    sid = scanned(server)
    _, page = text(server, f"/s/{sid}/results")
    import html
    form_html = page.split('id="fix-form"')[1].split("</form>")[0]
    for addon in ("smart_names", "ai_search"):
        box = form_html.split(f'id="{addon}"')[1].split(">")[0]
        assert "checked" not in box  # off by default
    # below the song list, above the price line
    assert form_html.index('class="fix-list"') < form_html.index("Extra checks (optional)") < form_html.index('id="fix-total"')
    assert "AI search, +$2" in form_html and "Smart names, +$2" in form_html
    shown = html.unescape(page)
    for sentence in (pages.SMART_NAMES_TEXT, pages.AI_SEARCH_TEXT):
        assert sentence in shown
        assert sentence.count(".") == 1 and "an AI service" in sentence  # one plain sentence, generic "AI"


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
        self.messages = SimpleNamespace(create=self._create)

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
    srv = start(tmp_path, lambda: mlc, mock=False, make_claude=lambda: claude)
    declared = data_flows(smart_names, ai_search)
    try:
        sid = upload_ok(srv)
        scan(srv, sid, smart_names=smart_names, ai_search=ai_search)
        assert wait_done(srv, sid)["state"] == "done"
        _, results = text(srv, f"/s/{sid}/results")
        # The free check: exactly what's declared for it, and never anything to the AI.
        assert mlc.sent == declared.mlc
        assert claude.sent == declared.claude == frozenset()
        # After checkout (every song paid for): exactly what's declared for the paid songs too.
        pay_all(srv, sid)
        _, fixes = open_fixes(srv, sid)
        assert mlc.sent == declared.mlc_all
        assert claude.sent == declared.claude_all
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()

    # ...and the words on the fix page (after checkout, with these checks chosen) say exactly that
    import html
    shown = html.unescape(fixes)
    for heading, sentence in privacy_items(smart_names, ai_search):
        assert heading in shown and sentence in shown
    claude_line = next(h for h, _ in privacy_items(smart_names, ai_search) if "AI service" in h)
    for kind in (ISRCS, SONG_CODES, TITLES, WRITER_NAMES):
        assert (kind in claude_line) == (kind in declared.claude_all)
    if declared.claude_all:
        assert "after checkout" in claude_line


def test_there_is_no_check_step_any_more(server):
    import re
    assert "Four steps" in text(server, "/")[1]
    sid = upload_ok(server)
    _, page = text(server, f"/s/{sid}/columns")
    steps = re.search(r'<ol class="steps"[^>]*>(.*?)</ol>', page, re.S)
    labels = re.findall(r"<li[^>]*>(.*?)</li>", steps.group(1)) if steps else []
    assert labels == ["Upload", "Columns", "Report", "Fix pack"], labels
    assert "Step 2 of 4: Columns" in page
    # An old /check link (or the Back button) lands where the session is now.
    status, headers, _ = call(server, f"/s/{sid}/check")
    assert status == 303 and headers["Location"].endswith("/columns")
    columns(server, sid)
    wait_done(server, sid)
    status, headers, _ = call(server, f"/s/{sid}/check")
    assert status == 303 and headers["Location"].endswith("/progress")


def test_results_page_shows_the_privacy_text_for_each_add_on_setting(server):
    sid = scanned(server)
    _, page = text(server, f"/s/{sid}/results")
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
    assert "Nothing goes to an AI service." in privacy_text(True, True, mock=True)
    srv = start(tmp_path)  # mock=True: the mock matchers are used, never Claude's
    try:
        sid = upload_ok(srv)
        scan(srv, sid, smart_names=True, ai_search=True)  # both checks on, still nothing real runs
        assert wait_done(srv, sid)["state"] == "done"
        _, page = text(srv, f"/s/{sid}/results")
        assert "Nothing goes to an AI service." in page and "Nothing goes to The MLC." in page
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_without_mlc_login_it_explains_and_exits(monkeypatch, capsys):
    monkeypatch.delenv("MLC_USERNAME", raising=False)
    monkeypatch.delenv("MLC_PASSWORD", raising=False)
    assert web.main([], open_browser=None) == 1
    assert "--mock" in capsys.readouterr().err


# ---- Polish round: AI wording, steps, summary, re-check, consistent dropdowns ------------------

def test_pages_say_ai_not_claude(server):
    sid = scanned(server)
    for path in ("/", f"/s/{sid}/columns", f"/s/{sid}/results"):
        assert "Claude" not in text(server, path)[1], path


def test_writers_use_dropdowns_like_the_other_fields(server):
    tunecore = (ROOT / "fixtures" / "import" / "tunecore_export.xlsx").read_bytes()
    sid = upload_ok(server, "tunecore_export.xlsx", tunecore)
    _, page = text(server, f"/s/{sid}/columns")
    assert 'type="checkbox"' not in page.split('<fieldset class="mapping">')[1].split("</fieldset>")[0]
    # one dropdown per writer column found (Songwriter 1 and 2), plus an empty one to add another
    assert page.count('name="map_writers"') == 3
    assert '<option value="7" selected>H: Songwriter 1</option>' in page
    assert '<option value="8" selected>I: Songwriter 2</option>' in page
    assert '<option value="">(add another column)</option>' in page
    # an empty dropdown is ignored; two picked columns both count
    columns(server, sid, [("map_title", "3"), ("map_isrc", "5"), ("map_writers", "7"),
                          ("map_writers", "8"), ("map_writers", "")])
    assert server.sessions.get(sid).mapping["writers"].columns == [7, 8]


def test_summary_filter_shows_a_summary_of_findings(server):
    sid = scanned(server, ai_search=True)
    _, page = text(server, f"/s/{sid}/results")
    assert '<button type="button" class="chip" data-filter="all" aria-pressed="true">Summary</button>' in page
    assert ">All <" not in page
    panel = page.split('<section class="findings-panel" data-summary')[1].split("</section>")[0]
    # The free check runs no AI search, so the red songs aren't split into cases yet.
    assert ('<dt>Red</dt><dd>2 songs have no match for their ISRCs yet: '
            '<span class="titles">Ghost Signal, Blue Hour</span>') in panel
    assert "not linked to your recording" not in panel
    assert "1 song is only partly claimed" in panel and "Paper Crowns" in panel
    assert '<dt>Green</dt><dd>1 song is all set' in panel


def test_red_songs_are_sold_as_needs_fixing_before_any_ai(server):
    sid = scanned(server, ai_search=True)
    _, page = text(server, f"/s/{sid}/results")
    fix_list = page.split('id="fix-form"')[1].split("</form>")[0]
    for title in ("Ghost Signal", "Blue Hour"):
        row = fix_list.split(title)[1].split("</li>")[0]
        assert "Needs fixing: we’ll work out exactly how after checkout." in row
    assert len(fixable_ids(page)) == 4  # the two red songs plus the two yellow ones
    assert 'class="needs-search"' not in page  # no AI callout before paying


def test_songs_needing_ai_search_get_a_callout_with_the_checkbox(server):
    sid = scanned(server)  # AI search off: after checkout the two red songs can't be placed
    pay_all(server, sid)
    _, page = open_fixes(server, sid)
    group = page.split('data-status="unresolved"')[1].split("</section>")[0]
    assert "Ghost Signal" in group and "Blue Hour" in group
    box = group.split('<div class="needs-search"')[1].split("</form>")[0]
    assert "2 songs need AI search to be worked out" in box
    assert 'name="ai_search" value="on" required' in box  # ticking it is the OK to send data
    assert "an AI service" in box and "Add AI search for $2" in box  # no longer free
    assert "AI search wasn’t added" in group  # says why, not "couldn't finish"


def test_recheck_without_the_box_ticked_sends_nothing(server):
    sid = scanned(server)
    pay_all(server, sid)
    open_fixes(server, sid)
    status, page = text(server, f"/s/{sid}/recheck", data=b"", content_type="application/x-www-form-urlencoded")
    assert status == 400 and "Tick AI search first" in page
    assert server.sessions.get(sid).settings["ai_search"] is False


def test_recheck_needs_payment(server):
    sid = scanned(server)
    status, _, _ = call(server, f"/s/{sid}/recheck", *form([("ai_search", "on")]))
    assert status == 402


def test_recheck_checks_only_the_red_songs_with_ai_search(tmp_path):
    checked = []

    class Counting(MockClient):
        def search_recordings(self, isrc):
            checked.append(isrc)
            return super().search_recordings(isrc)

    # real mode (not sample data), with offline stand-ins for the AI, so the privacy text is the real one
    srv = start(tmp_path, Counting, mock=False, make_claude=lambda: object(),
                make_name_matcher=lambda client: MockNameMatcher(),
                make_work_searcher=lambda client: MockWorkSearcher())
    try:
        sid = scanned(srv)
        assert len(checked) == 5
        pay_all(srv, sid)
        open_fixes(srv, sid)  # AI search off: nothing runs, the red songs wait for the box
        assert len(checked) == 5
        status, headers, _ = call(srv, f"/s/{sid}/recheck", *form([("ai_search", "on")]))
        # Pay (demo) for the AI search add-on, then the fix page works the red songs out
        assert status == 303 and headers["Location"].endswith("/fixes")
        _, page = open_fixes(srv, sid)
        assert sorted(checked[5:]) == ["QZABC2400004", "QZABC2400005"]  # just the two red songs
        s = srv.sessions.get(sid)
        assert s.settings["ai_search"] is True  # so "what left this computer" now says so
        assert 'class="needs-search"' not in page  # both songs are placed now
        assert 'data-status="register"' in page and 'data-status="link"' in page
        assert "song titles and writer names" in page  # the privacy panel follows the new setting
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()
