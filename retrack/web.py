"""The Retrack web UI, on this computer only.

    python -m retrack.web            # checks against the real MLC API (needs MLC_USERNAME/MLC_PASSWORD)
    python -m retrack.web --mock     # sample data: nothing leaves the computer

The flow, one page per step (pages are built in web_pages.py):
  GET  /                        upload a spreadsheet (.csv or .xlsx, any columns)
  POST /upload                  saves it in a private session folder, reads it
  GET  /s/<id>/columns          confirm the column mapping
  POST /s/<id>/columns          converts the rows with that mapping
  GET  /s/<id>/check            choose the extra checks (smart names, AI search: after checkout)
  POST /s/<id>/scan             starts the free check in the background (it never calls the AI)
  POST /s/<id>/recheck          after checkout: AI search for paid red songs it was off for
  GET  /s/<id>/progress         "Checking 37 of 120…" (refreshes itself)
  GET  /s/<id>/status           the same, as JSON, for the page's script
  GET  /s/<id>/results          the report, with songs to tick for the fix pack
  POST /s/<id>/fix              the ticked songs -> the pay page ("N songs × $X = $Total")
  POST /s/<id>/pay              needs the attestation checkbox -> Stripe Checkout (or Pay (demo))
  GET  /s/<id>/paid             Stripe sends the customer back here; we ask Stripe if it's paid
  GET  /s/<id>/fixes            the on-screen fix pack (paid songs only). The first visit after
                                paying runs the AI checks on the paid songs ("Working out…")
  GET  /s/<id>/fixes.xlsx       Retrack_Fixes_YYYYMMDD.xlsx; also bulk_match.csv and, after
  POST /s/<id>/template         an uploaded MLC template, bulk_work_registration.xlsx
  GET  /s/<id>/time_left        how long the fix page has left (the page's script asks)
  GET  /privacy                 what leaves this computer, and how payments work
  POST /stripe/webhook          Stripe says a checkout was paid (signed; the only POST without
                                the Origin check, because Stripe isn't a browser)
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
import json
import os
import re
import signal
import sys
import threading
import time
import webbrowser
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from . import web_pages as pages
from .ai import (DAILY_LIMIT_ENV, AIConfigError, DailySpend, MeteredClient, daily_limit_from_env)
from .checker import RED, check_row, load_catalog, needs_ai_after_checkout
from .env import load_env
from .fixes import (FIX_PRICE_USD, UNRESOLVED, FixPackError, bulk_match_name, confirm, fixes_workbook_name,
                    open_template, plan_fixes, sold_kind, split_for_template, total_price, write_bulk_match,
                    write_fixes_workbook, write_template_copy)
from .payments import (DEMO, LIVE, TEST, WEBHOOK_EVENTS, PaymentConfigError, PaymentError, WebhookError,
                       checkout_from_event, payments_from_env, verify_webhook)
from .importer import (FIELDS, REQUIRED, CatalogImportError, FieldMap, HeaderRuleMapper, clean_mapping,
                       convert, read_table, sample_rows, write_catalog, write_problems)
from .mlc_client import MLCAuthError, MLCClient, MLCError, MockClient
from .name_matcher import ClaudeNameMatcher, MockNameMatcher
from .preview import HOST, PORT, port_busy_message, run_until_stopped
from .report import render_html
from .sessions import SessionStore
from .work_search import MAX_SEARCHES, ClaudeWorkSearcher, MockWorkSearcher

MAX_UPLOAD_BYTES = 5_000_000  # a big .xlsx catalog is well under this
MAX_FORM_BYTES = 200_000      # the non-file forms (columns, ticked songs)
MAX_SONGS = 500               # each song is at least 2 API calls, so keep one check to minutes
TEMPLATE_CSV = "title,artist,isrc,writers\r\n"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPIRED_TEXT = "Your files were deleted for your privacy. Please upload again."
SESSION_PATH = re.compile(r"^/s/([A-Za-z0-9_-]{22})/(columns|check|scan|recheck|progress|status|results|fix|pay|paid|"
                          r"fixes|fixes\.xlsx|bulk_match\.csv|template|bulk_work_registration\.xlsx|"
                          r"delete|time_left|report\.html|import_problems\.csv)$")
WEBHOOK_PATH = "/stripe/webhook"
MAX_WEBHOOK_BYTES = 1_000_000


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
FINISH_MLC_DOWN = ("The MLC didn’t answer while Retrack was working out your songs, so some of them "
                   "need checking by hand. Nothing you paid for is lost.")
FINISH_NO_AI = ("The AI service isn’t set up on this computer, so Retrack couldn’t run the extra checks. "
                "Those songs need checking by hand.")


def run_scan(session, make_client, name_matcher, work_searcher, on_done=None):
    """Check the rows listed in session.todo, updating session.done as it goes.
    Runs in its own thread.

    session.phase says which job this is:
      "scan"   - the free check of every row. Web UI: no AI at all (both are None).
      "finish" - after checkout: only paid songs, again, with the AI checks turned on.
                 AI search runs only for red songs. Their old results are replaced.
    on_done() runs at the end of a finish (it logs what the AI cost).
    """
    try:
        client = make_client()
        for i in session.todo:
            if session.cancelled:  # "Delete my files now" was pressed
                return
            before = session.results[i]
            searcher = work_searcher if before is not None and before.status == RED else None
            session.results[i] = check_row(session.rows[i], client, name_matcher, searcher)
            session.done += 1
        session.state = "done"
    except Exception as e:
        if session.phase == "finish":
            # The songs are paid for: never strand them on an error page. Keep what we
            # have; the fix page says which songs need checking by hand, and why.
            session.finish_problem = FINISH_MLC_DOWN
            session.state = "done"
        elif isinstance(e, MLCAuthError):
            session.fail(*MLC_LOGIN_FAILED)
        elif isinstance(e, (MLCError, requests.RequestException)):
            session.fail(*MLC_DOWN)
        else:  # a bug: say so plainly instead of hanging on "Checking…"
            session.fail("Something went wrong inside Retrack",
                         f"Your file is fine. Press Start the check to try again; if it happens again, "
                         f"restart Retrack. ({type(e).__name__})")
    finally:
        if session.phase == "finish":
            session.finished |= set(session.todo)
            if on_done:
                on_done()


# ---- Paid or not: one test, used by the return page, _sync_order() and the webhook ----------

def order_is_paid(session, checkout):
    """The only test for "paid": Stripe says so, about the order we created for this session.

    Every part must match what Retrack stored when it created the checkout: the
    Checkout Session id, our session id (client_reference_id), "paid", and the amount.
    """
    order = session.order
    return (order is not None and checkout.id == order["checkout_id"]
            and checkout.client_reference_id == session.id
            and checkout.payment_status == "paid"
            and checkout.amount_total == order["amount"])


def mark_paid(session, songs):
    """Unlock these songs. Doing it twice changes nothing (idempotent).

    Each newly paid song also adds MAX_SEARCHES AI calls to what this session may
    use after checkout: the per-order AI limit (see ai.py).
    """
    new = set(songs) - set(session.paid_songs)
    session.ai_budget.add_songs(len(new), MAX_SEARCHES)
    session.paid_songs = sorted(set(session.paid_songs) | set(songs))
    session.order = None


# ---- The server ------------------------------------------------------------------------------

class Config:
    """Everything the handler needs, passed in (dependency injection), so tests can
    swap the MLC and Claude for fakes."""

    def __init__(self, make_client, mock, sessions, make_name_matcher, make_work_searcher, payments,
                 webhook_secret="", log=None, make_claude=None, daily=None):
        self.payments = payments  # payments.StripePayments or DemoPayments
        self.webhook_secret = webhook_secret  # STRIPE_WEBHOOK_SECRET: checks Stripe's signature
        self.log = log or (lambda line: print(line, file=sys.stderr))  # event types and counts only
        self.deleted_session_events = 0
        self.make_client = make_client
        self.mock = mock
        self.sessions = sessions
        # The AI: make_claude() makes the raw API client; the two factories take the
        # (metered) client and return a matcher or searcher. daily holds today's AI spend.
        self.make_name_matcher = make_name_matcher
        self.make_work_searcher = make_work_searcher
        self.make_claude = make_claude or _anthropic_client
        self.daily = daily if daily is not None else DailySpend(daily_limit_from_env(os.environ))


def _anthropic_client():
    import anthropic  # imported here so --mock works without the anthropic package

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


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

        def _gone(self, session_id=""):
            if config.sessions.expired(session_id):
                return self._send(410, pages.error_page(
                    "This check has ended", EXPIRED_TEXT, mock,
                    action='<a class="btn" href="/">Upload again</a>'))
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
            if url.path == "/privacy":
                return self._send(200, pages.privacy_page(mock, config.payments.mode))
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
                return self._gone(match.group(1))
            getattr(self, "get_" + re.sub(r"[^a-z]", "_", match.group(2)), self._not_found)(session)

        def do_POST(self):
            url = urlsplit(self.path)
            if url.path == WEBHOOK_PATH:
                # Stripe's servers aren't a browser and send no Origin header, so this one route
                # skips the Origin check. Its protection is the signature check instead.
                return self.post_stripe_webhook()
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
                    return self._gone(match.group(1))
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
        def _ai_tools(self, session, smart_names, ai_search):
            """The AI checks to run after checkout, as (name matcher, work searcher), or
            None if the AI service isn't set up. Every call goes through a MeteredClient:
            it enforces the order's and the day's limits and counts the tokens."""
            client = None
            if not mock:  # --mock: offline stand-ins, no AI client is ever made
                try:
                    client = MeteredClient(config.make_claude(), session.ai_budget, config.daily)
                except Exception:  # no anthropic package, no API key...
                    return None
            name_matcher = (WatchedNameMatcher(config.make_name_matcher(client), session)
                            if smart_names else None)
            work_searcher = (WatchedWorkSearcher(config.make_work_searcher(client), session)
                             if ai_search else None)
            return name_matcher, work_searcher

        def _start(self, session, todo, name_matcher=None, work_searcher=None, phase="scan"):
            session.todo, session.done, session.phase = todo, 0, phase
            session.error, session.claude_failures, session.state = None, 0, "running"
            on_done = None
            if phase == "finish":
                session.ai_budget.start_run()
                on_done = lambda: self._log_ai_usage(session, len(todo))
            threading.Thread(target=run_scan, daemon=True,
                             args=(session, config.make_client, name_matcher, work_searcher, on_done)).start()
            self._redirect(f"/s/{session.id}/progress")

        def _log_ai_usage(self, session, songs):
            """What one order's AI checks cost, for the terminal. Counts and dollars only."""
            if mock:
                return
            line = (f"AI usage for one order ({songs} {'song' if songs == 1 else 'songs'}): "
                    f"{session.ai_budget.usage.summary()}. Today: about ${config.daily.spent_usd:.4f} "
                    f"of ${config.daily.limit_usd:.2f}.")
            if session.ai_budget.limit_hit:
                line += " Some AI calls were skipped because a limit was reached."
            config.log(line)

        def _finish_paid_songs(self, session):
            """The first visit to the fixes after paying: run the AI checks the user turned on,
            on the newly paid songs only. Returns True if a run started (we've redirected)."""
            pending = [i for i in session.paid_songs if i not in session.finished]
            if not pending:
                return False
            s = session.settings
            todo = [i for i in pending if needs_ai_after_checkout(session.results[i], s["smart_names"],
                                                                  s["ai_search"])]
            if todo:
                tools = self._ai_tools(session, s["smart_names"], s["ai_search"])
                if tools is not None:
                    self._start(session, todo, *tools, phase="finish")
                    return True
                session.finish_problem = FINISH_NO_AI
            session.finished |= set(pending)  # nothing to run for these
            return False

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
            if (session.settings["smart_names"] or session.settings["ai_search"]) and not mock:
                # The AI only runs after checkout, but say now if it isn't set up, not after paying.
                try:
                    config.make_claude()
                except Exception:  # no anthropic package, no API key...
                    return self._send(503, pages.check_page(session, mock, error=AI_MISSING))
            session.results = [None] * len(session.rows)
            # The free check never calls the AI: no name matcher, no work searcher.
            self._start(session, list(range(len(session.rows))))

        def post_recheck(self, session):
            """After checkout: AI search for the paid red songs that couldn't be placed
            because AI search was off. Only those, and only with the box ticked."""
            if self._need_paid(session):
                return
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            form = self._form()
            if "ai_search" not in form:  # the box is the user's OK to send titles and writer names
                return self._send_fixes(session, 400, recheck_error=NO_AI_CONSENT)
            todo = [i for i in session.paid_songs if sold_kind(session.results[i]) == UNRESOLVED]
            if not todo:
                return self._redirect(f"/s/{session.id}/fixes")
            tools = self._ai_tools(session, smart_names=False, ai_search=True)
            if tools is None:
                return self._send_fixes(session, 503, recheck_error=AI_MISSING)
            session.settings = dict(session.settings, ai_search=True)  # so the privacy copy says so
            self._start(session, todo, *tools, phase="finish")

        def get_progress(self, session):
            if session.state == "done":
                return self._redirect(f"/s/{session.id}/{'fixes' if session.phase == 'finish' else 'results'}")
            if session.state == "failed":
                title, message = session.error
                retry = (f'<a class="btn" href="/s/{session.id}/check">Back to the checks</a>'
                         '<a href="/">Upload a different file</a>')
                return self._send(502, pages.error_page(title, message, mock, step="check", action=retry))
            if session.state in ("columns", "ready"):
                return self._redirect(f"/s/{session.id}/{'columns' if session.state == 'columns' else 'check'}")
            self._send(200, pages.progress_page(session, mock))

        def get_status(self, session):
            done_page = "fixes" if session.phase == "finish" else "results"
            nxt = {"done": done_page, "failed": "progress", "columns": "columns",
                   "ready": "check"}.get(session.state, "progress")
            self._send(200, json.dumps({"state": session.state, "done": session.done, "total": session.total,
                                        "text": pages.progress_text(session), "next": f"/s/{session.id}/{nxt}"}),
                       "application/json")

        # -- 4. report --
        def get_results(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            self._sync_order(session)
            notice = (parse_qs(urlsplit(self.path).query).get("checkout") or [""])[0]
            self._send(200, pages.results_page(session, mock, notice=notice))

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

        # -- 5. fix pack: tick songs -> confirm -> pay -> verify -> fixes --
        def _chosen(self, session, values):
            n = len(session.results)
            chosen = sorted({int(v) for v in values if v.isdigit() and int(v) < n})
            return [i for i in chosen if sold_kind(session.results[i])]

        def _paid_plan(self, session):
            return plan_fixes([session.results[i] for i in session.paid_songs], sell_red=True)

        def _base_url(self):
            """Where Stripe should send the customer back: this same Retrack, as the browser sees it."""
            port = self.server.server_port
            host = self.headers.get("Host", "")
            return f"http://{host}" if host in (f"localhost:{port}", f"127.0.0.1:{port}") else f"http://localhost:{port}"

        def _sync_order(self, session):
            """If an order is open, ask Stripe about it (covers a customer who paid but never
            made it back to the return page). Quietly does nothing if Stripe doesn't answer."""
            if session.order is None or config.payments.mode == DEMO:
                return
            try:
                checkout = config.payments.retrieve(session.order["checkout_id"])
            except PaymentError:
                return
            if order_is_paid(session, checkout):
                mark_paid(session, session.order["songs"])

        def post_fix(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            chosen = [i for i in self._chosen(session, self._form().get("song", [])) if i not in session.paid_songs]
            if not chosen:
                return self._send(400, pages.results_page(session, mock, error=(
                    "No songs ticked", "Tick at least one song to fix, then press Review my fix pack.")))
            plan = plan_fixes([session.results[i] for i in chosen], sell_red=True)
            self._send(200, pages.pay_page(session, plan, chosen, mock, config.payments.mode))

        def post_pay(self, session):
            if session.state != "done":
                return self._redirect(f"/s/{session.id}/progress")
            form = self._form()
            # Songs already paid for are never charged again.
            chosen = [i for i in self._chosen(session, form.get("song", [])) if i not in session.paid_songs]
            if not chosen:
                return self._redirect(f"/s/{session.id}/{'fixes' if session.paid_songs else 'results'}")
            plan = plan_fixes([session.results[i] for i in chosen], sell_red=True)

            # No attestation, no fix pack: checked here, on the server, not just by the
            # checkbox's "required" attribute (which a browser or script can skip).
            if form.get("attest") != ["yes"]:
                return self._send(400, pages.pay_page(session, plan, chosen, mock, config.payments.mode, error=(
                    "No confirmation, no fix pack",
                    "Tick the box to confirm you reviewed the data and control the shares, then press "
                    "the button again. Nothing was charged.")))
            session.attestation = confirm("ticked the confirmation box on the Retrack page")

            if config.payments.mode == DEMO:
                mark_paid(session, chosen)
                return self._redirect(f"/s/{session.id}/fixes")

            amount = round(total_price(plan.songs) * 100)  # in cents, for checking Stripe's total
            order = session.order
            try:
                if order is not None:
                    checkout = config.payments.retrieve(order["checkout_id"])
                    if order_is_paid(session, checkout):  # already paid in another tab
                        mark_paid(session, order["songs"])
                        return self._redirect(f"/s/{session.id}/fixes")
                    if order["songs"] == chosen and checkout.status == "open":
                        return self._redirect(checkout.url)  # the same order: reuse it, never a second charge
                    if checkout.status == "open":
                        config.payments.expire(checkout.id)  # a different order replaces it: close the old one
                session.orders_placed += 1
                base = self._base_url()
                checkout = config.payments.create_checkout(
                    reference=session.id, quantity=len(chosen), unit_amount_cents=round(FIX_PRICE_USD * 100),
                    success_url=f"{base}/s/{session.id}/paid?checkout_session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{base}/s/{session.id}/results?checkout=canceled",
                    idempotency_key=f"retrack-{session.id}-order-{session.orders_placed}")
            except PaymentError as e:
                if e.retry:  # a network blip or an outage: trying again can work
                    error = ("Stripe didn’t answer",
                             f"Nothing was charged. Wait a minute and press the button again. ({e})")
                else:  # Stripe said no to the request itself: retrying gets the same answer
                    error = ("Stripe rejected the payment setup", "Stripe rejected the payment setup. "
                             "Nothing was charged. The details are in the terminal where Retrack is running.")
                return self._send(502, pages.pay_page(session, plan, chosen, mock, config.payments.mode, error=error))
            session.order = {"songs": chosen, "checkout_id": checkout.id, "url": checkout.url, "amount": amount}
            self._redirect(checkout.url)

        def get_paid(self, session):
            """Stripe sends the customer back here. The URL proves nothing: ask Stripe."""
            query = parse_qs(urlsplit(self.path).query)
            checkout_id = (query.get("checkout_session_id") or [""])[0]
            order = session.order
            if order is None and session.paid_songs:
                return self._redirect(f"/s/{session.id}/fixes")  # already verified (a reload)
            if order is None or checkout_id != order["checkout_id"]:
                return self._send(400, pages.error_page(
                    "That payment link doesn’t match this check",
                    "Retrack couldn’t find a payment for this check behind that link, so nothing was unlocked. "
                    "Go back to your results and try again; you won’t be charged twice.", mock, step="fix",
                    action=f'<a class="btn" href="/s/{session.id}/results">Back to the results</a>'))
            try:
                checkout = config.payments.retrieve(checkout_id)
            except PaymentError:
                return self._send(502, pages.error_page(
                    "Stripe didn’t answer", "If you paid, your payment is safe. Reload this page in a minute.",
                    mock, step="fix"))
            if order_is_paid(session, checkout):
                mark_paid(session, order["songs"])
                return self._redirect(f"/s/{session.id}/fixes")
            if checkout.client_reference_id != session.id or checkout.id != order["checkout_id"]:
                return self._send(400, pages.error_page(
                    "That payment link doesn’t match this check", "Nothing was unlocked.", mock, step="fix"))
            notice = "processing" if checkout.status == "complete" else "failed"
            self._redirect(f"/s/{session.id}/results?checkout={notice}")

        def _need_paid(self, session):
            if session.paid_songs:
                return False
            self._send(402, pages.error_page(
                "Pay first to see the fixes",
                "Your results are free. The fixes for each song show here once the payment is done.",
                mock, step="fix", action=f'<a class="btn" href="/s/{session.id}/results">Back to the results</a>'))
            return True

        # -- Stripe webhook --
        def _json(self, status, data):
            self._send(status, json.dumps(data), "application/json")

        def post_stripe_webhook(self):
            """Stripe tells us a checkout was paid. Same checks as the return page; safe to repeat."""
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_WEBHOOK_BYTES:
                self.close_connection = True
                return self._json(413, {"error": "too large"})
            payload = self.rfile.read(length)  # the raw bytes: the signature is over exactly these
            try:
                event = verify_webhook(payload, self.headers.get("Stripe-Signature"), config.webhook_secret)
            except WebhookError as e:
                return self._json(400, {"error": str(e)})
            if event.type not in WEBHOOK_EVENTS:
                return self._json(200, {"received": True, "ignored": True})
            event_type, checkout = checkout_from_event(event)
            session = config.sessions.get(checkout.client_reference_id)
            if session is None:
                # Its 60 minutes are up and its files are gone. Acknowledge, so Stripe stops
                # retrying, and log only the event type and a count: never any catalog data.
                config.deleted_session_events += 1
                config.log(f"Stripe webhook: {event_type} for a Retrack session that's already deleted "
                           f"({config.deleted_session_events} so far).")
                return self._json(200, {"received": True})
            if order_is_paid(session, checkout):
                mark_paid(session, session.order["songs"])
                return self._json(200, {"received": True, "unlocked": True})
            # Already unlocked (a repeat), or it doesn't match the order we stored: unlock nothing.
            # 200 either way, so Stripe doesn't retry something that will never match.
            return self._json(200, {"received": True, "unlocked": False})

        def get_time_left(self, session):
            seconds = config.sessions.seconds_left(session)
            self._json(200, {"seconds": seconds, "text": pages.time_left_text(seconds)})

        def _send_fixes(self, session, status=200, **kw):
            self._send(status, pages.fixes_page(session, self._paid_plan(session), mock, session.template_note,
                                                seconds_left=config.sessions.seconds_left(session),
                                                lifetime=config.sessions.lifetime, **kw))

        def get_fixes(self, session):
            self._sync_order(session)
            if self._need_paid(session):
                return
            if session.state == "running":
                return self._redirect(f"/s/{session.id}/progress")
            if self._finish_paid_songs(session):
                return
            self._send_fixes(session)

        def get_fixes_xlsx(self, session):
            if self._need_paid(session):
                return
            path = session.folder / fixes_workbook_name()
            write_fixes_workbook(self._paid_plan(session), session.attestation, path)
            self._send(200, path.read_bytes(), XLSX_TYPE,
                       [("Content-Disposition", f'attachment; filename="{path.name}"')])

        def get_bulk_match_csv(self, session):
            if self._need_paid(session):
                return
            plan = self._paid_plan(session)
            if not plan.link:
                return self._not_found()
            name = bulk_match_name()
            write_bulk_match(plan.link, session.folder / name)  # The MLC's exact format, nothing added
            self._send(200, (session.folder / name).read_bytes(), "text/csv; charset=utf-8",
                       [("Content-Disposition", f'attachment; filename="{name}"')])

        def post_template(self, session):
            if self._need_paid(session):
                return
            plan = self._paid_plan(session)
            fields = parse_multipart(self.headers.get("Content-Type", ""), self._body(MAX_UPLOAD_BYTES))
            uploads = [v for v in fields.get("template", []) if isinstance(v, tuple) and v[1]]
            if not plan.register or not uploads:
                return self._redirect(f"/s/{session.id}/fixes")
            template = session.folder / "your_template.xlsx"
            template.write_bytes(uploads[0][1])
            try:
                _, layout = open_template(template)
                to_fill, claim_instead = split_for_template(plan.register, layout)
                if not to_fill:
                    raise FixPackError("Every song to register already has an MLC song code, and this template "
                                       "has no column for it, so there’s nothing safe to put in it.")
                write_template_copy(template, to_fill, session.folder / "bulk_work_registration.xlsx")
            except FixPackError as e:
                return self._send(400, pages.fixes_page(session, plan, mock,
                                                        seconds_left=config.sessions.seconds_left(session),
                                                        lifetime=config.sessions.lifetime, error=(
                    "Retrack couldn’t use that template", f"{e} Your fixes above are unchanged.")))
            finally:
                template.unlink(missing_ok=True)  # only the filled copy is kept
            session.template_note = (
                f" {len(claim_instead)} {'song is' if len(claim_instead) == 1 else 'songs are'} not in the copy: "
                "they already have an MLC song code and your template has no column for it, so claim them in "
                "the Portal instead of registering them twice." if claim_instead else "")
            self._redirect(f"/s/{session.id}/fixes")

        def get_bulk_work_registration_xlsx(self, session):
            if self._need_paid(session):
                return
            path = session.folder / "bulk_work_registration.xlsx"
            if not path.exists():
                return self._not_found()
            self._send(200, path.read_bytes(), XLSX_TYPE,
                       [("Content-Disposition", 'attachment; filename="bulk_work_registration.xlsx"')])

        # -- delete --
        def post_delete(self, session):
            config.sessions.delete(session.id)
            self._redirect("/?deleted=1")

        def log_message(self, format, *args):
            pass  # keep the terminal quiet

    return Handler


def start_server(make_client, mock=False, host=HOST, port=PORT, sessions=None,
                 make_name_matcher=None, make_work_searcher=None, payments=None, webhook_secret=None, log=None,
                 make_claude=None, daily=None):
    """Open the port and return the server (not running yet). port=0 picks any free port.

    The server always listens on `host`, which defaults to 127.0.0.1 (this computer only).
    """
    sessions = sessions or SessionStore()  # clears leftovers from a crashed run
    # Each factory gets the metered AI client (None with --mock) and returns a matcher/searcher.
    if make_name_matcher is None:
        make_name_matcher = ((lambda client: MockNameMatcher()) if mock
                             else (lambda client: ClaudeNameMatcher(client=client)))
    if make_work_searcher is None:
        make_work_searcher = ((lambda client: MockWorkSearcher()) if mock
                              else (lambda client: ClaudeWorkSearcher(client=client)))
    if payments is None:
        payments = payments_from_env(os.environ, mock)  # raises PaymentConfigError for an unsafe key
    if webhook_secret is None:
        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    config = Config(make_client, mock, sessions, make_name_matcher, make_work_searcher, payments,
                    webhook_secret, log, make_claude, daily)  # daily=None: reads RETRACK_AI_DAILY_LIMIT_USD
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.sessions = sessions
    server.payments = payments
    server.ai_daily = config.daily
    _start_sweeper(sessions)
    return server


def _start_sweeper(sessions, every=60):
    """Delete sessions past their 60 minutes even when nobody is using the page."""
    def loop():
        while True:
            time.sleep(every)
            sessions.sweep()
    threading.Thread(target=loop, daemon=True).start()


def main(argv=None, open_browser=webbrowser.open):
    parser = argparse.ArgumentParser(description="Run the Retrack web UI on this computer.")
    data = parser.add_mutually_exclusive_group()
    data.add_argument("--mock", action="store_true",
                      help="sample data and demo payments: fully offline, nothing leaves this computer")
    data.add_argument("--mock-mlc", action="store_true",
                      help="sample MLC data and offline AI, but real Stripe test checkout when Stripe "
                           "test keys are set (to try payments before you have MLC access)")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to use (default: {PORT})")
    args = parser.parse_args(argv)
    load_env()  # .env in this folder, if there is one: no need to export variables by hand

    if args.mock or args.mock_mlc:
        make_client = MockClient
    else:
        try:
            MLCClient()  # fail now, with a clear message, if the login details are missing
        except MLCError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        make_client = MLCClient

    try:
        # --mock-mlc: the data side is sample data (mock=True: fake MLC, offline AI, the banner),
        # but payments come from the environment like a real run (still refusing live keys).
        payments = payments_from_env(os.environ, mock=False) if args.mock_mlc else None
        server = start_server(make_client, args.mock or args.mock_mlc, port=args.port, payments=payments)
    except (PaymentConfigError, AIConfigError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except OSError:
        print(port_busy_message(args.port))
        return 1
    if not (args.mock or args.mock_mlc):
        print(f"AI checks: up to ${server.ai_daily.limit_usd:.2f} a day ({DAILY_LIMIT_ENV}), and "
              f"{MAX_SEARCHES} AI calls per paid song. Each order's cost is logged here.")
    if server.payments.mode == TEST:
        print("Payments: Stripe test mode (no real money).")
        if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
            print("Tip: set STRIPE_WEBHOOK_SECRET so Stripe can confirm payments directly (see the README).")
    elif args.mock_mlc:
        print("Payments: no Stripe test keys found, so the pay button is Pay (demo).")
    elif server.payments.mode == LIVE:
        print("Payments: Stripe LIVE mode. Real cards will be charged.")
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
