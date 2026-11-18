"""The Retrack web UI, on this computer only.

    python -m retrack.web            # checks against the real MLC API (needs MLC_USERNAME/MLC_PASSWORD)
    python -m retrack.web --mock     # sample data: nothing leaves the computer

The flow, one page per step (pages are built in web_pages.py):
  GET  /                        upload a spreadsheet (.csv or .xlsx, any columns)
  POST /upload                  saves it in a private session folder, reads it
  GET  /s/<id>/columns          confirm the column mapping
  POST /s/<id>/columns          converts the rows with that mapping
  GET  /s/<id>/check            choose the extra checks (smart names, AI search)
  POST /s/<id>/scan             starts the check in the background
  POST /s/<id>/recheck          checks red songs again with AI search (only those)
  GET  /s/<id>/progress         "Checking 37 of 120…" (refreshes itself)
  GET  /s/<id>/status           the same, as JSON, for the page's script
  GET  /s/<id>/results          the report, with songs to tick for the fix pack
  POST /s/<id>/fix              the ticked songs -> review page with the price
  POST /s/<id>/fix/download     needs the attestation checkbox -> the fix pack .zip
  POST /s/<id>/delete           "Delete my files now"
  GET  /s/<id>/report.html, /s/<id>/import_problems.csv, /template.csv  downloads

Every rule is a call to the existing modules: importer (reading, mapping,
cleanup), checker (verdicts), fixes (what can be fixed, price, attestation,
the pack itself) and privacy (what leaves the computer). This file only
wires them to web pages.

Safety: it listens on 127.0.0.1 only, refuses POSTs from other websites
(Origin check), serves nothing but its own pages, and keeps uploads in
sessions.SessionStore, which deletes them when the server stops.
"""

import argparse
import datetime
import io
import json
import re
import signal
import sys
import tempfile
import threading
import webbrowser
import zipfile
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from . import web_pages as pages
from .checker import RED, check_row, load_catalog
from .fixes import FixPackError, build_fix_pack, check_template, confirm, fix_kind, plan_fixes
from .importer import (FIELDS, REQUIRED, CatalogImportError, FieldMap, HeaderRuleMapper, clean_mapping,
                       convert, read_table, sample_rows, write_catalog, write_problems)
from .mlc_client import MLCAuthError, MLCClient, MLCError, MockClient
from .name_matcher import ClaudeNameMatcher, MockNameMatcher
from .preview import HOST, PORT, port_busy_message, run_until_stopped
from .report import render_html
from .sessions import SessionStore
from .work_search import ClaudeWorkSearcher, MockWorkSearcher

MAX_UPLOAD_BYTES = 5_000_000  # a big .xlsx catalog is well under this
MAX_FORM_BYTES = 200_000      # the non-file forms (columns, ticked songs)
MAX_SONGS = 500               # each song is at least 2 API calls, so keep one check to minutes
TEMPLATE_CSV = "title,artist,isrc,writers\r\n"
SESSION_PATH = re.compile(r"^/s/([A-Za-z0-9_-]{22})/(columns|check|scan|recheck|progress|status|results|fix|fix/download|"
                          r"delete|report\.html|import_problems\.csv)$")


class UploadError(Exception):
    """Something wrong with what was sent. The message is shown to the user."""


# ---- Reading what the browser sent ----------------------------------------------------------

def parse_multipart(content_type, body):
    """A multipart/form-data body -> {field name: [values]}. Files come back as
    (filename, bytes), other fields as text.

    Browsers send file uploads in "multipart" format. Python's email parser
    understands it (emails with attachments use it too), so we borrow it
    instead of adding a dependency.
    """
    if not content_type.startswith("multipart/form-data"):
        raise UploadError("Choose a file, then press Upload.")
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        value = (filename, data) if filename is not None else data.decode("utf-8", "replace")
        fields.setdefault(name, []).append(value)
    return fields


def read_upload(content_type, body):
    """The uploaded catalog: (filename, bytes)."""
    for value in parse_multipart(content_type, body).get("catalog", []):
        if isinstance(value, tuple) and value[1]:
            return value
    raise UploadError("Choose a .csv or .xlsx file, then press Upload.")


def check_file_type(filename):
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        hint = {".xls": "That’s an older Excel file. In Excel choose File, Save As, Excel Workbook (.xlsx).",
                ".numbers": "In Numbers choose File, Export To, Excel or CSV.",
                ".pdf": "Retrack needs the spreadsheet itself, not a PDF of it."}.get(suffix, "")
        raise UploadError(f"Retrack reads .csv and .xlsx files, and this is “{Path(filename).name}”. "
                          f"{hint} From Google Sheets use File, Download, CSV.".strip())


# ---- Watching Claude, so a silent fallback is reported -------------------------------------
#
# The Claude matchers are fail-safe: when Claude doesn't answer they say "no
# match" and the check carries on. These thin wrappers count those times so the
# results page can say so. They change no answer.

UNAVAILABLE = ("unavailable", "no answer")


class WatchedNameMatcher:
    def __init__(self, inner, session):
        self.inner, self.session = inner, session

    def same_person(self, a, b):
        verdict = self.inner.same_person(a, b)
        if any(word in verdict.reason for word in UNAVAILABLE):
            self.session.claude_failures += 1
        return verdict


class WatchedWorkSearcher:
    def __init__(self, inner, session):
        self.inner, self.session = inner, session

    def suggest_queries(self, title, writers):
        queries = self.inner.suggest_queries(title, writers)
        if queries is None:
            self.session.claude_failures += 1
        return queries

    def judge(self, title, writers, candidate):
        verdict = self.inner.judge(title, writers, candidate)
        if any(word in verdict.reason for word in UNAVAILABLE):
            self.session.claude_failures += 1
        return verdict


# ---- The check, in the background -----------------------------------------------------------

MLC_LOGIN_FAILED = ("Retrack couldn’t log in to The MLC",
                    "The MLC turned down Retrack’s login. Check MLC_USERNAME and MLC_PASSWORD in your "
                    ".env file, then stop Retrack (Ctrl+C) and start it again. Your file is fine.")
MLC_DOWN = ("The MLC didn’t answer",
            "Nothing is wrong with your file. The MLC’s service may be busy or down. "
            "Wait a few minutes, then press Start the check again.")
AI_MISSING = ("The AI checks aren’t set up on this computer",
              "Smart names and AI search need an API key for the AI service (ANTHROPIC_API_KEY, see "
              "the README). Turn both checks off to go on without them, or set the key and restart Retrack.")
NO_AI_CONSENT = ("Tick AI search first",
                 "These songs can only be placed with AI search, which sends their titles and writer names "
                 "out of this computer. Tick the box to allow it, then press the button again.")


def run_scan(session, make_client, name_matcher, work_searcher):
    """Check the rows listed in session.todo, updating session.done as it goes.
    Runs in its own thread. A first check lists every row; a re-check lists only
    the songs being checked again, and their old results are replaced."""
    try:
        client = make_client()
        for i in session.todo:
            if session.cancelled:  # "Delete my files now" was pressed
                return
            session.results[i] = check_row(session.rows[i], client, name_matcher, work_searcher)
            session.done += 1
        session.state = "done"
    except MLCAuthError:
        session.fail(*MLC_LOGIN_FAILED)
    except (MLCError, requests.RequestException):
        session.fail(*MLC_DOWN)
    except Exception as e:  # a bug: say so plainly instead of hanging on "Checking…"
        session.fail("Something went wrong inside Retrack",
                     f"Your file is fine. Press Start the check to try again; if it happens again, "
                     f"restart Retrack. ({type(e).__name__})")


# ---- The server ------------------------------------------------------------------------------

class Config:
    """Everything the handler needs, passed in (dependency injection), so tests can
    swap the MLC and Claude for fakes."""

    def __init__(self, make_client, mock, sessions, make_name_matcher, make_work_searcher):
        self.make_client = make_client
        self.mock = mock
        self.sessions = sessions
        self.make_name_matcher = make_name_matcher
        self.make_work_searcher = make_work_searcher


def make_handler(config):
    mock = config.mock

    class Handler(BaseHTTPRequestHandler):
        # -- sending --
        def _send(self, status, body, content_type="text/html; charset=utf-8", headers=()):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location):
            self.send_response(303)  # "see other": after a form post, go to this page with GET
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _from_another_site(self):
            # Any website you visit could try to post a form to localhost. Browsers
            # say where a request came from in the Origin header, so refuse anything
            # that isn't this page. (This guards against "cross-site request forgery".)
            origin = self.headers.get("Origin")
            if not origin or origin == "null":
                return origin == "null"
            port = self.server.server_port
            return origin not in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")

        def _body(self, limit):
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self.close_connection = True  # don't read a huge body we won't use
                raise UploadError(f"That’s too big: Retrack takes files up to {limit // 1_000_000 or 1} MB. "
                                  "Check that you picked your catalog, not a recording.")
            return self.rfile.read(length)

        def _form(self):
            return parse_qs(self._body(MAX_FORM_BYTES).decode("utf-8", "replace"))

        def _gone(self):
            self._send(404, pages.error_page(
                "This check has ended",
                "Retrack deletes files when you press Delete my files now or stop it, so this page "
                "is no longer here. Upload your spreadsheet again to start a new check.", mock))

        # -- routing --
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                notice = "Your files were deleted from this computer." if url.query == "deleted=1" else ""
                return self._send(200, pages.home_page(mock, notice=notice))
            if url.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")  # no icon; just don't log an error
            if url.path == "/template.csv":
                return self._send(200, TEMPLATE_CSV, "text/csv; charset=utf-8",
                                  [("Content-Disposition", 'attachment; filename="retrack-template.csv"')])
            match = SESSION_PATH.match(url.path)
            if not match:
                return self.send_error(404, "Nothing here. Retrack is at /")
            session = config.sessions.get(match.group(1))
            if session is None:
                return self._gone()
            getattr(self, "get_" + re.sub(r"[^a-z]", "_", match.group(2)), self._not_found)(session)

        def do_POST(self):
            url = urlsplit(self.path)
            if self._from_another_site():
                return self.send_error(403, "Only the Retrack page itself can send this.")
            try:
                if url.path == "/upload":
                    return self.post_upload()
                match = SESSION_PATH.match(url.path)
                if not match:
                    return self.send_error(404)
                session = config.sessions.get(match.group(1))
                if session is None:
                    return self._gone()
                getattr(self, "post_" + re.sub(r"[^a-z]", "_", match.group(2)), self._not_found)(session)
            except UploadError as e:
                self._send(400, pages.home_page(mock, error=("Retrack couldn’t use that", str(e))))

        def _not_found(self, session=None):
            self.send_error(404)

        # -- 1. upload --
        def post_upload(self):
            body = self._body(MAX_UPLOAD_BYTES)
            filename, data = read_upload(self.headers.get("Content-Type", ""), body)
            check_file_type(filename)
            session = config.sessions.create(filename, data)
            try:
                session.table = read_table(session.upload_path)
            except CatalogImportError as e:
                config.sessions.delete(session.id)  # nothing to keep
                message = str(e).replace(session.upload_path.name, session.filename)  # their name, not ours
                raise UploadError(f"{message} Check that it’s your catalog spreadsheet, then try again.")
            if not session.table.rows:
                config.sessions.delete(session.id)
                raise UploadError("Your file has headers but no songs under them. Check that you "
                                  "exported the whole catalog, then try again.")
            session.mapping = HeaderRuleMapper().map_columns(session.table.headers, sample_rows(session.table))
            session.proposed = {f: FieldMap(list(m.columns), m.confidence) for f, m in session.mapping.items()}
            self._redirect(f"/s/{session.id}/columns")

        # -- 2. columns --
        def get_columns(self, session):
            self._send(200, pages.columns_page(session, mock))

        def post_columns(self, session):
            if session.state == "running":
                return self._redirect(f"/s/{session.id}/progress")
            form = self._form()
            n = len(session.table.headers)
            chosen = {}
            for f in FIELDS:
                raw = form.get("map_writers", []) if f == "writers" else form.get(f"map_{f}", [])[:1]
                cols = [int(v) for v in raw if v.isdigit() and int(v) < n]
                kept = cols == session.proposed[f].columns  # unchanged: keep the rules' confidence
                chosen[f] = FieldMap(cols, session.proposed[f].confidence if kept else 1.0)
            session.mapping = clean_mapping(chosen, n)

            missing = [pages.FIELD_LABELS[f] for f in REQUIRED if not session.mapping[f].columns]
            if missing:
                return self._send(400, pages.columns_page(session, mock, error=(
                    "Pick the missing columns",
                    f"Retrack needs a column for {' and '.join(missing)} to check your songs. "
                    "Choose it from the list, then press Next.")))

            # Same path as the command line: convert, save catalog.csv, read it back with
            # load_catalog. So the web UI checks exactly what `python -m retrack` would.
            session.import_result = convert(session.table, session.mapping)
            write_catalog(session.import_result.rows, session.folder / "catalog.csv")
            write_problems(session.import_result.problems, session.folder / "import_problems.csv")
            rows = load_catalog(session.folder / "catalog.csv")
            if not rows:
                return self._send(400, pages.columns_page(session, mock, error=(
                    "No songs with a valid ISRC",
                    "None of the rows had an ISRC Retrack could use. Check that the ISRC column is the "
                    "right one; ISRCs look like CC-XXX-YY-NNNNN.")))
            if len(rows) > MAX_SONGS:
                return self._send(400, pages.columns_page(session, mock, error=(
                    "That’s a lot of songs at once",
                    f"Your file has {len(rows)} songs; Retrack checks up to {MAX_SONGS} at a time. "
                    "Split the spreadsheet into parts and upload each one.")))
            session.rows, session.results, session.state = rows, [], "ready"
            self._redirect(f"/s/{session.id}/check")

        # -- 3. check --
        def _checkers(self, session):
            """The extra checks the settings ask for. Raises if the AI service isn't set up."""
            name_matcher = (WatchedNameMatcher(config.make_name_matcher(), session)
                            if session.settings["smart_names"] else None)
            work_searcher = (WatchedWorkSearcher(config.make_work_searcher(), session)
                             if session.settings["ai_search"] else None)
            return name_matcher, work_searcher

        def _start(self, session, todo, name_matcher, work_searcher):
            session.todo, session.done = todo, 0
            session.error, session.claude_failures, session.state = None, 0, "running"
            threading.Thread(target=run_scan, daemon=True,
                             args=(session, config.make_client, name_matcher, work_searcher)).start()
            self._redirect(f"/s/{session.id}/progress")

        def get_check(self, session):
            if session.state == "columns":
                return self._redirect(f"/s/{session.id}/columns")
            if session.state == "running":
                return self._redirect(f"/s/{session.id}/progress")
            self._send(200, pages.check_page(session, mock))

        def post_scan(self, session):
            if session.state == "running":
                return self._redirect(f"/s/{session.id}/progress")
            if session.state == "columns":
                return self._redirect(f"/s/{session.id}/columns")
            form = self._form()
            session.settings = {"smart_names": "smart_names" in form, "ai_search": "ai_search" in form}
            try:
                name_matcher, work_searcher = self._checkers(session)
            except Exception:  # no anthropic package, no API key...
                return self._send(503, pages.check_page(session, mock, error=AI_MISSING))
            session.results = [None] * len(session.rows)
            self._start(session, list(range(len(session.rows))), name_matcher, work_searcher)

        def post_recheck(self, session):
            """Check the red songs that need AI search again, with AI search on. Only those."""
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            form = self._form()
            if "ai_search" not in form:  # the box is the user's OK to send titles and writer names
                return self._send(400, pages.results_page(session, mock, recheck_error=NO_AI_CONSENT))
            todo = [i for i, r in enumerate(session.results) if r.status == RED and not fix_kind(r)]
            if not todo:
                return self._redirect(f"/s/{session.id}/results")
            session.settings = dict(session.settings, ai_search=True)
            try:
                name_matcher, work_searcher = self._checkers(session)
            except Exception:
                session.settings = dict(session.settings, ai_search=False)
                return self._send(503, pages.results_page(session, mock, recheck_error=AI_MISSING))
            self._start(session, todo, name_matcher, work_searcher)

        def get_progress(self, session):
            if session.state == "done":
                return self._redirect(f"/s/{session.id}/results")
            if session.state == "failed":
                title, message = session.error
                retry = (f'<a class="btn" href="/s/{session.id}/check">Back to the checks</a>'
                         '<a href="/">Upload a different file</a>')
                return self._send(502, pages.error_page(title, message, mock, step="check", action=retry))
            if session.state in ("columns", "ready"):
                return self._redirect(f"/s/{session.id}/{'columns' if session.state == 'columns' else 'check'}")
            self._send(200, pages.progress_page(session, mock))

        def get_status(self, session):
            nxt = {"done": "results", "failed": "progress", "columns": "columns",
                   "ready": "check"}.get(session.state, "progress")
            self._send(200, json.dumps({"state": session.state, "done": session.done, "total": session.total,
                                        "text": pages.progress_text(session), "next": f"/s/{session.id}/{nxt}"}),
                       "application/json")

        # -- 4. report --
        def get_results(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            self._send(200, pages.results_page(session, mock))

        def get_report_html(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            html = render_html(session.results, catalog_name=session.filename, mock=mock)
            self._send(200, html, headers=[("Content-Disposition", 'attachment; filename="retrack-report.html"')])

        def get_import_problems_csv(self, session):
            path = session.folder / "import_problems.csv"
            if not path.exists():
                return self._not_found()
            self._send(200, path.read_bytes(), "text/csv; charset=utf-8",
                       [("Content-Disposition", 'attachment; filename="import_problems.csv"')])

        # -- 5. fix pack --
        def _chosen(self, session, values):
            n = len(session.results)
            chosen = sorted({int(v) for v in values if v.isdigit() and int(v) < n})
            return [i for i in chosen if fix_kind(session.results[i])]

        def post_fix(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            chosen = self._chosen(session, self._form().get("song", []))
            if not chosen:
                return self._send(400, pages.results_page(session, mock, error=(
                    "No songs ticked", "Tick at least one song to fix, then press Review my fix pack.")))
            plan = plan_fixes([session.results[i] for i in chosen])
            self._send(200, pages.review_page(session, plan, chosen, mock))

        def post_fix_download(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            fields = parse_multipart(self.headers.get("Content-Type", ""), self._body(MAX_UPLOAD_BYTES))
            chosen = self._chosen(session, [v for v in fields.get("song", []) if isinstance(v, str)])
            if not chosen:
                return self._redirect(f"/s/{session.id}/results")
            plan = plan_fixes([session.results[i] for i in chosen])

            # No attestation, no fix pack: checked here, on the server, not just by the
            # checkbox's "required" attribute (which a browser or script can skip).
            if fields.get("attest") != ["yes"]:
                return self._send(400, pages.review_page(session, plan, chosen, mock, error=(
                    "No confirmation, no fix pack",
                    "Tick the box to confirm you reviewed the data and control the shares, "
                    "then press Download again. No files were made.")))

            template_path = None
            uploads = [v for v in fields.get("template", []) if isinstance(v, tuple) and v[1]]
            if uploads:
                template_path = session.folder / "template.xlsx"
                template_path.write_bytes(uploads[0][1])
                try:
                    check_template(template_path)
                except FixPackError as e:
                    template_path.unlink(missing_ok=True)
                    return self._send(400, pages.review_page(session, plan, chosen, mock, error=(
                        "Retrack couldn’t use that template", f"{e} Or leave it out to get a worksheet instead.")))

            attestation = confirm("ticked the confirmation box on the Retrack page")
            with tempfile.TemporaryDirectory(dir=session.folder) as out:
                try:
                    written = build_fix_pack(plan, out, attestation, template_path)
                except FixPackError as e:
                    return self._send(400, pages.review_page(session, plan, chosen, mock,
                                                             error=("Retrack couldn’t make the fix pack", str(e))))
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
                    for name in written:
                        z.write(Path(out) / name, f"fix_pack/{name}")
            stamp = datetime.date.today().strftime("%Y%m%d")
            self._send(200, buffer.getvalue(), "application/zip",
                       [("Content-Disposition", f'attachment; filename="retrack-fix-pack-{stamp}.zip"')])

        # -- delete --
        def post_delete(self, session):
            config.sessions.delete(session.id)
            self._redirect("/?deleted=1")

        def log_message(self, format, *args):
            pass  # keep the terminal quiet

    return Handler


def start_server(make_client, mock=False, host=HOST, port=PORT, sessions=None,
                 make_name_matcher=None, make_work_searcher=None):
    """Open the port and return the server (not running yet). port=0 picks any free port.

    The server always listens on `host`, which defaults to 127.0.0.1 (this computer only).
    """
    sessions = sessions or SessionStore()  # clears leftovers from a crashed run
    if make_name_matcher is None:
        make_name_matcher = MockNameMatcher if mock else ClaudeNameMatcher
    if make_work_searcher is None:
        make_work_searcher = MockWorkSearcher if mock else ClaudeWorkSearcher
    config = Config(make_client, mock, sessions, make_name_matcher, make_work_searcher)
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.sessions = sessions
    return server


def main(argv=None, open_browser=webbrowser.open):
    parser = argparse.ArgumentParser(description="Run the Retrack web UI on this computer.")
    parser.add_argument("--mock", action="store_true", help="sample data from fixtures/: nothing leaves this computer")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to use (default: {PORT})")
    args = parser.parse_args(argv)

    if args.mock:
        make_client = MockClient
    else:
        try:
            MLCClient()  # fail now, with a clear message, if the login details are missing
        except MLCError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        make_client = MLCClient

    try:
        server = start_server(make_client, args.mock, port=args.port)
    except OSError:
        print(port_busy_message(args.port))
        return 1
    previous = stop_on_signals()
    try:
        return run_until_stopped(server, open_browser)
    finally:
        server.sessions.delete_all()  # uploaded files never outlive the server
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_on_signals():
    """Make every normal way of stopping the server run its clean-up.

    Ctrl+C sends SIGINT, closing the terminal window sends SIGHUP and `kill`
    sends SIGTERM. Only the first stops Python politely by default (and not
    even that when started in the background), so all three are turned into
    the same KeyboardInterrupt, which makes main() delete the uploaded files.
    Returns the old handlers so they can be put back.
    """
    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)  # SIGHUP doesn't exist on Windows
        if sig is not None:
            previous[sig] = signal.signal(sig, stop)
    return previous


if __name__ == "__main__":
    sys.exit(main())
