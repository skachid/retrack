"""Show the HTML report in your browser: python -m retrack catalog.csv --serve

This starts a tiny web server on your own computer, using only Python's
built-in http.server module (no new dependencies).

Two safety choices:
  - It listens on 127.0.0.1 only, the "loopback" address. Only programs on
    this computer can reach it; other devices on your Wi-Fi can't.
  - It serves the report and nothing else. Python's usual quick server
    (`python -m http.server`) shares the whole folder, which here would
    include your .env file with MLC credentials.
"""

import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 8000
REPORT_PATHS = {"/", "/index.html", "/report.html"}


def make_handler(html):
    """Build a request handler that answers every page request with `html`."""
    body = html.encode("utf-8")

    class ReportHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path not in REPORT_PATHS:
                self.send_error(404, "Only the report lives here: go to /")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")  # always show the latest run
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # keep the terminal quiet; the default prints a line per request

    return ReportHandler


def start_server(html, host=HOST, port=PORT):
    """Open the port and return the server (not running yet). port=0 picks any free port."""
    return ThreadingHTTPServer((host, port), make_handler(html))


def port_busy_message(port):
    return (f"Port {port} is already in use, maybe by another Retrack server that's still running.\n"
            f"Stop that one first (Ctrl+C in its window), or see what's using it with: lsof -i :{port}")


def run_until_stopped(server, open_browser=webbrowser.open):
    """Open the browser, then answer requests until Ctrl+C. Returns a CLI exit code."""
    url = f"http://localhost:{server.server_port}/"
    print(f"\nRetrack is live at {url}  (only this computer can see it)")
    print("Press Ctrl+C to stop.")
    if open_browser:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # Ctrl+C
        print("\nStopped.")
    finally:
        server.server_close()  # give the port back
    return 0


def serve(html, port=PORT, open_browser=webbrowser.open):
    """Serve one finished report until Ctrl+C (the --serve flag)."""
    try:
        server = start_server(html, port=port)
    except OSError:
        print(port_busy_message(port))
        return 1
    return run_until_stopped(server, open_browser)
