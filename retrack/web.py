"""The Retrack homepage: upload a catalog CSV, see the results on the same page.

    python -m retrack.web            # checks against the real MLC API (needs MLC_USERNAME/MLC_PASSWORD)
    python -m retrack.web --mock     # checks against the fake data in fixtures/ (no login needed)

Like --serve, this uses only Python's built-in http.server, listens on
127.0.0.1 (this computer only) and serves nothing but its own pages.

Routes:
  GET  /               the homepage
  POST /check          a CSV upload; answers with the page plus results
                       (or just the results, when the page's script asks)
  GET  /template.csv   an empty catalog with the four column headers
"""

import argparse
import sys
import webbrowser
from email.parser import BytesParser
from email.policy import HTTP
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .checker import check_catalog, parse_catalog
from .mlc_client import MLCClient, MLCError, MockClient
from .name_matcher import MockNameMatcher
from .preview import HOST, PORT, port_busy_message, run_until_stopped
from .report import CSS_PATH, GROUPS, MANUAL_CHECKS, date_text, groups_html, summary_html

HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 2_000_000  # about 20,000 songs; anything bigger is probably the wrong file
MAX_SONGS = 500               # each song is 2 API calls, so keep one check to a few minutes
TEMPLATE_CSV = "title,artist,isrc,writers\r\n"


class UploadError(Exception):
    """Something wrong with the uploaded file. The message is shown to the user."""


# ---- Reading the upload ------------------------------------------------------------

def read_upload(content_type, body):
    """Pull the uploaded file out of a multipart/form-data request.

    Browsers send file uploads in "multipart" format: the body is split into
    parts, one per form field. Python's email parser understands that format
    (emails with attachments use it too), so we borrow it instead of adding a
    dependency. Returns (filename, file_bytes).
    """
    if not content_type.startswith("multipart/form-data"):
        raise UploadError("Choose a CSV file, then press Check my catalog.")
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") == "catalog":
            data = part.get_payload(decode=True) or b""
            if not data:
                break
            return part.get_filename() or "catalog.csv", data
    raise UploadError("Choose a CSV file, then press Check my catalog.")


def decode_csv(data):
    """Turn the file's bytes into text. Most CSVs are UTF-8; older Excel uses Windows-1252."""
    if data[:2] == b"PK":  # .xlsx files are zip archives, which start with "PK"
        raise UploadError("That looks like an Excel workbook. In Excel choose File, Save As, "
                          "CSV UTF-8, then upload the .csv file.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError("Retrack couldn’t read that file as text. Save it as CSV UTF-8 and try again.")


def check_upload(filename, data, make_client, name_matcher=None):
    """Upload bytes in, results out. Raises UploadError or MLCError."""
    try:
        rows = parse_catalog(decode_csv(data))
    except ValueError as e:  # missing columns, e.g. "CSV is missing columns: writers"
        missing = str(e).split(": ", 1)[-1]
        raise UploadError(f"Your file has no {missing} column. The first row needs these four "
                          "headers: title, artist, isrc, writers.")
    if len(rows) > MAX_SONGS:
        raise UploadError(f"That file has {len(rows)} songs. Retrack checks up to {MAX_SONGS} at a time; "
                          "split the file and check each part.")
    return check_catalog(rows, make_client(), name_matcher)


# ---- Building the page --------------------------------------------------------------

def results_section(results, filename, mock):
    return f"""<div class="container">
    <div class="results-head">
      <div>
        <h2>Results for {escape(filename)}</h2>
        <p class="results-meta">Checked {date_text()}</p>
      </div>
      <button type="button" class="btn btn--quiet" data-print hidden>Print or save as PDF</button>
    </div>
    {summary_html(results, mock)}
    {groups_html(results)}
  </div>"""


def error_section(title, message):
    return f"""<div class="container">
    <div class="alert" role="alert">
      <h2>{escape(title)}</h2>
      <p>{escape(message)}</p>
    </div>
  </div>"""


def _stylesheet():
    """report.css (tokens and song cards) plus home.css, with home.css's Hallmark stamp on top."""
    report = CSS_PATH.read_text(encoding="utf-8")
    home = (HERE / "home.css").read_text(encoding="utf-8")
    stamp, home_rules = home.split("/* Homepage.", 1)
    report_rules = report[report.index("/* Tokens."):]  # skip the report's own stamp
    return stamp + report_rules + "\n/* Homepage." + home_rules


def render_home(results_inner="", mock=False):
    meanings = "".join(
        f'<div class="is-{cls}"><dt>{verdict}: {meaning.lower()}</dt><dd>{explain}</dd></div>'
        for (cls, _, verdict, meaning), explain in zip(
            (GROUPS[s] for s in GROUPS),
            ("The MLC has no match for the recording, or it isn’t linked to a song. "
             "Royalties for it are probably going unpaid.",
             "Shares don’t add up to 100%, nobody is collecting, or one of your writers is missing.",
             "The recording is linked to a song, 100% is claimed and your writers match."),
        )
    )
    manual = " and ".join(f'<a href="{escape(url)}">{escape(name)}</a>' for name, url in MANUAL_CHECKS)
    mode = ('<p class="mode"><strong>Demo mode.</strong> Files are checked against Retrack’s '
            "test data, not The MLC.</p>") if mock else ""
    script = (HERE / "home.js").read_text(encoding="utf-8")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Upload your catalog and Retrack checks every song’s registration with The MLC, then shows what to fix.">
<title>Retrack, find unpaid song royalties</title>
<style>
{_stylesheet()}
</style>
</head>
<body class="home">
<header class="bar">
  <div class="container">
    <a class="wordmark" href="/" aria-label="Retrack home">Retrack</a>
    <nav aria-label="Page"><a href="#prepare">Get your file ready</a></nav>
  </div>
</header>

<main>
  <section class="hero" aria-labelledby="hero-title">
    <div class="container">
      <div>
        <h1 id="hero-title">Find the royalties your songs are missing.</h1>
        <p class="lead">Upload your catalog and Retrack checks each song’s registration with The MLC.</p>
      </div>
      <form id="upload" class="upload" action="/check" method="post" enctype="multipart/form-data">
        <label id="drop" class="drop" for="catalog">
          <span class="drop-title">Drop your catalog CSV here</span>
          <span class="drop-hint">or click to choose a file</span>
          <span id="file-name" class="drop-file" aria-live="polite"></span>
          <input id="catalog" name="catalog" type="file" accept=".csv,text/csv" required aria-describedby="upload-help">
        </label>
        <div class="upload-row">
          <p id="upload-help" class="helper">Columns: title, artist, isrc, writers. <a href="/template.csv">Blank template</a></p>
          <button id="check" class="btn" type="submit"><span class="spinner" aria-hidden="true"></span><span class="label">Check my catalog</span></button>
        </div>
        {mode}
      </form>
    </div>
  </section>

  <section id="results" class="results" aria-live="polite" aria-label="Results">{results_inner}</section>

  <section id="prepare" class="section" aria-labelledby="prepare-title">
    <div class="container how">
      <div>
        <h2 id="prepare-title">Get your file ready</h2>
        <p class="lead">Three steps, about five minutes.</p>
      </div>
      <div>
        <ol class="how-steps">
          <li><div><h3>Export your catalog</h3><p>From your distributor’s dashboard, or copy it from your own spreadsheet.</p></div></li>
          <li><div><h3>Keep four columns</h3><p>Name them exactly as in the table below, one song per row.</p></div></li>
          <li><div><h3>Save as CSV and upload</h3><p>Excel: File, Save As, CSV UTF-8. Google Sheets: File, Download, CSV.</p></div></li>
        </ol>
        <table class="columns">
          <thead><tr><th scope="col">Column</th><th scope="col">What goes in it</th><th scope="col">Looks like</th></tr></thead>
          <tbody>
            <tr><th scope="row"><code>title</code></th><td>The song’s name as released</td><td>Example Song</td></tr>
            <tr><th scope="row"><code>artist</code></th><td>The releasing artist</td><td>Example Artist</td></tr>
            <tr><th scope="row"><code>isrc</code></th><td>The recording’s 12-character code, from your distributor. Dashes are fine.</td><td><code>CC-XXX-YY-NNNNN</code></td></tr>
            <tr><th scope="row"><code>writers</code></th><td>Every songwriter, split by a semicolon</td><td>First Writer; Second Writer</td></tr>
          </tbody>
        </table>
        <p class="template"><a href="/template.csv">Download a blank template</a> with these headers.</p>
      </div>
    </div>
  </section>

  <section class="section" aria-labelledby="colours-title">
    <div class="container">
      <h2 id="colours-title">What each colour means</h2>
      <dl class="meaning-rows">{meanings}</dl>
    </div>
  </section>

  <section class="band" aria-labelledby="privacy-title">
    <div class="container">
      <h2 id="privacy-title">Your file stays yours</h2>
      <ul>
        <li><strong>Read once, saved nowhere.</strong><span>Retrack runs on this computer and forgets the file after the check.</span></li>
        <li><strong>Only codes go to The MLC.</strong><span>It sends ISRCs and song codes, never the whole file.</span></li>
        <li><strong>No audio, no training.</strong><span>It never touches your recordings and never learns from your catalog.</span></li>
      </ul>
    </div>
  </section>
</main>

<footer class="foot">
  <div class="container">
    <p>Retrack checks The MLC’s Public Search API, which covers US streaming mechanicals.
    SoundExchange and ASCAP/BMI have no public API, so check those by hand: {manual}.</p>
  </div>
</footer>
<script>
{script}
</script>
</body>
</html>
"""


# ---- The server -----------------------------------------------------------------------

def make_handler(make_client, name_matcher=None, mock=False):
    """Build the request handler. `make_client` returns a fresh MLC client for each check."""

    class HomeHandler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="text/html; charset=utf-8", extra_headers=()):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _answer(self, status, inner):
            # The page's script only wants the results; a plain form post gets the whole page.
            if self.headers.get("X-Retrack-Fragment") == "1":
                self._send(status, inner)
            else:
                self._send(status, render_home(inner, mock))

        def _from_another_site(self):
            # Any website you visit could try to post a form to localhost. Browsers
            # say where a request came from in the Origin header, so refuse anything
            # that isn't this page. (This guards against "cross-site request forgery".)
            origin = self.headers.get("Origin")
            if not origin:
                return False
            port = self.server.server_port
            return origin not in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, render_home("", mock))
            elif path == "/template.csv":
                self._send(200, TEMPLATE_CSV, "text/csv; charset=utf-8",
                           [("Content-Disposition", 'attachment; filename="retrack-template.csv"')])
            else:
                self.send_error(404, "Nothing here. The homepage is at /")

        def do_POST(self):
            if urlsplit(self.path).path != "/check":
                self.send_error(404)
                return
            if self._from_another_site():
                self.send_error(403, "Uploads only come from the Retrack page itself.")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD_BYTES:
                self.close_connection = True  # don't read a huge body we won't use
                self._answer(413, error_section("That file is too big",
                             "Retrack takes catalog files up to 2 MB. Check that you picked the CSV, not a recording."))
                return
            body = self.rfile.read(length)
            try:
                filename, data = read_upload(self.headers.get("Content-Type", ""), body)
                results = check_upload(filename, data, make_client, name_matcher)
            except UploadError as e:
                self._answer(400, error_section("Retrack couldn’t check that file", str(e)))
                return
            except MLCError as e:
                self._answer(502, error_section("The MLC didn’t answer",
                             f"Nothing is wrong with your file. Try again in a minute. ({e})"))
                return
            self._answer(200, results_section(results, filename, mock))

        def log_message(self, format, *args):
            pass  # keep the terminal quiet

    return HomeHandler


def start_server(make_client, name_matcher=None, mock=False, host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), make_handler(make_client, name_matcher, mock))


def main(argv=None, open_browser=webbrowser.open):
    parser = argparse.ArgumentParser(description="Run the Retrack homepage on this computer.")
    parser.add_argument("--mock", action="store_true", help="check uploads against the fake data in fixtures/")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to use (default: {PORT})")
    args = parser.parse_args(argv)

    if args.mock:
        make_client, name_matcher = MockClient, MockNameMatcher()
    else:
        try:
            MLCClient()  # fail now, with a clear message, if the login details are missing
        except MLCError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        make_client, name_matcher = MLCClient, None

    try:
        server = start_server(make_client, name_matcher, args.mock, port=args.port)
    except OSError:
        print(port_busy_message(args.port))
        return 1
    return run_until_stopped(server, open_browser)


if __name__ == "__main__":
    sys.exit(main())
