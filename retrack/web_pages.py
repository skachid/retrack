"""The web UI's pages, as HTML strings. No rules live here: every decision
(column mapping, verdicts, which songs can be fixed, the price, what leaves the
computer) comes from the functions this file imports.

Pages, in the order a user meets them:
  home_page      upload a spreadsheet
  columns_page   confirm the column mapping, choose the extra checks
  progress_page  "Checking 37 of 120…" (refreshes itself; no JavaScript needed)
  results_page   the report, plus ticking the songs to fix
  review_page    "N songs × $X", the attestation checkbox, download the .zip
  error_page     anything that went wrong, in plain English, with what to do next

All catalog text goes through escape(). The CSS and the small script are
pasted in, so the pages load nothing from the internet.
"""

import json
from html import escape
from pathlib import Path

from .checker import GREEN, RED
from .compliance import ATTESTATION_TEXT, COMPLIANCE_TEXT, NEVER_TEXT
from .fixes import (ALL_SET, CLAIM, FIX_PRICE_USD, LINK, REGISTER, UNRESOLVED, bulk_match_name, fix_kind,
                    sold_kind,
                    price_line)
from .payments import DEMO, DEMO_BANNER, TEST, TEST_BANNER
from .importer import FIELDS, LOW_CONFIDENCE, REQUIRED, SAMPLE_ROWS, column_letter
from .privacy import privacy_items
from .sessions import SESSION_SECONDS
from .report import CSS_PATH, GROUPS, MANUAL_CHECKS, date_text, findings_html, groups_html, summary_html

HERE = Path(__file__).parent

STEPS = [("upload", "Upload"), ("columns", "Columns"), ("report", "Report"), ("fix", "Fix pack")]
FIELD_LABELS = {"title": "Song title", "artist": "Artist", "isrc": "ISRC", "writers": "Writers"}
KIND_TEXT = {
    REGISTER: "Register it: rows for The MLC’s Bulk Work Registration template",
    LINK: "Link it: a match file to link your recording to the song",
    CLAIM: "Claim it: a checklist of steps in The MLC Portal",
    UNRESOLVED: "Needs fixing: we’ll work out exactly how after checkout.",
    ALL_SET: "Nothing to fix after a closer look",
}
# The page says "an AI service", not the provider's name: users need to know it's an
# outside AI and exactly what it gets (privacy.py), not which one.
SMART_NAMES_TEXT = ("After checkout, for the songs you pay for: when a writer’s name doesn’t match exactly, "
                    "Retrack sends the two writer names to an AI service to ask if they’re the same person.")
AI_SEARCH_TEXT = ("After checkout, for the red songs you pay for: Retrack sends the song title and writer "
                  "names to The MLC’s song search and to an AI service, to tell whether each needs "
                  "registering or linking.")
# The same check offered on the fix page, for songs already paid for.
AI_SEARCH_NOW_TEXT = ("Sends these songs’ titles and writer names to The MLC’s song search and to an AI "
                      "service, to tell whether each needs registering or linking.")
MOCK_TOGGLE_TEXT = " In sample data mode an offline stand-in answers instead, so nothing is sent."


# ---- The frame every page shares ---------------------------------------------------------

def _stylesheet():
    """report.css (tokens and song cards) plus home.css, with home.css's Hallmark stamp on top."""
    report = CSS_PATH.read_text(encoding="utf-8")
    home = (HERE / "home.css").read_text(encoding="utf-8")
    stamp, home_rules = home.split("/* Homepage.", 1)
    report_rules = report[report.index("/* Tokens."):]  # skip the report's own stamp
    return stamp + report_rules + "\n/* Homepage." + home_rules


SAMPLE_BANNER = ('<div class="sample-banner" role="note"><div class="container">'
                 "<strong>Sample data, not real results.</strong> Retrack is running with sample data: "
                 "every answer comes from built-in test data, not The MLC.</div></div>")


def steps_nav(current):
    names = [key for key, _ in STEPS]
    n = names.index(current)
    current_attr = ' aria-current="step"'
    items = "".join(f"<li{current_attr if key == current else ''}>{label}</li>" for key, label in STEPS)
    return (f'<ol class="steps" aria-label="Steps">{items}</ol>'
            f'<p class="steps-short">Step {n + 1} of {len(STEPS)}: {STEPS[n][1]}</p>')


def layout(title, body, mock=False, step=None, head_extra=""):
    nav = steps_nav(step) if step else '<nav aria-label="Page"><a href="#how">How it works</a></nav>'
    manual = " and ".join(f'<a href="{escape(url)}">{escape(name)}</a>' for name, url in MANUAL_CHECKS)
    script = (HERE / "home.js").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Check your catalog’s registrations with The MLC and get the files to fix what’s missing.">
<title>{escape(title)}</title>
{head_extra}
<style>
{_stylesheet()}
</style>
</head>
<body class="home">
{SAMPLE_BANNER if mock else ""}
<header class="bar">
  <div class="container">
    <a class="wordmark" href="/" aria-label="Retrack home">Retrack</a>
    {nav}
  </div>
</header>
<main>
{body}
</main>
<footer class="foot">
  <div class="container">
    <p>Retrack checks The MLC’s Public Search API, which covers US streaming mechanicals.
    SoundExchange and ASCAP/BMI have no public API, so check those by hand: {manual}.</p>
    <p class="small compliance">{escape(COMPLIANCE_TEXT)}</p>
    <p class="small"><a href="/privacy">Privacy</a></p>
  </div>
</footer>
<script>
{script}
</script>
</body>
</html>
"""


def alert(title, message, extra=""):
    return (f'<div class="alert" role="alert"><h2>{escape(title)}</h2>'
            f"<p>{escape(message)}</p>{extra}</div>")


def privacy_list(smart_names, ai_search, mock, attrs=""):
    items = "".join(f"<li><strong>{escape(h)}</strong><span>{escape(s)}</span></li>"
                    for h, s in privacy_items(smart_names, ai_search, mock))
    return f'<ul class="privacy-list"{attrs}>{items}</ul>'


def delete_form(session):
    return (f'<form class="delete-form" method="post" action="/s/{session.id}/delete">'
            '<button class="btn btn--quiet" type="submit">Delete my files now</button>'
            "<p class=\"helper\">Deletes your file and these results from this computer.</p></form>")


def privacy_panel(session, mock):
    s = session.settings
    return f"""<section class="privacy" aria-labelledby="privacy-title">
      <h2 id="privacy-title">What left this computer</h2>
      {privacy_list(s["smart_names"], s["ai_search"], mock)}
      {delete_form(session)}
    </section>"""


# ---- 1. Upload ------------------------------------------------------------------------------

def home_page(mock=False, error=None, notice=""):
    error_html = alert(*error) if error else ""
    notice_html = f'<p class="notice-ok" role="status">{escape(notice)}</p>' if notice else ""
    # One row per colour: the word set in its colour, the meaning beside it in words,
    # so the colour is never the only signal.
    colours = "".join(f'<div class="is-{cls}"><dt>{verdict}</dt><dd>{escape(meaning)}</dd></div>'
                      for cls, _, verdict, meaning in GROUPS.values())
    body = f"""
  <section class="hero" aria-labelledby="hero-title">
    <div class="container">
      <div>
        <h1 id="hero-title">Find the royalties your songs are missing.</h1>
        <p class="lead">Upload your catalog and Retrack checks each song’s registration with The MLC.</p>
      </div>
      <form id="upload" class="upload" aria-label="Upload your catalog" action="/upload" method="post" enctype="multipart/form-data">
        {notice_html}{error_html}
        <label id="drop" class="drop" for="catalog">
          <span class="drop-title">Drop your spreadsheet here</span>
          <span class="drop-hint">or click to choose a .csv or .xlsx file</span>
          <span id="file-name" class="drop-file" aria-live="polite"></span>
          <input id="catalog" name="catalog" type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required aria-describedby="upload-help">
        </label>
        <div class="upload-row">
          <p id="upload-help" class="helper">Any columns, as your distributor exports them. <a href="/template.csv">Blank template</a></p>
          <button id="check" class="btn" type="submit">Upload</button>
        </div>
      </form>
    </div>
  </section>

  <section id="how" class="section" aria-labelledby="how-title">
    <div class="container">
      <h2 id="how-title">How it works</h2>
      <p class="lead">Four steps, about five minutes.</p>
      <ol class="how-steps">
        <li><h3>Upload</h3><p>Your spreadsheet as it is: a distributor export or your own sheet.</p></li>
        <li><h3>Check the columns</h3><p>Confirm which column is the title, ISRC and writers, then start the free check. No AI is used.</p></li>
        <li><h3>Read the report</h3><p>Red songs come first, each with what’s wrong and how to fix it.</p></li>
        <li><h3>Get the fix pack</h3><p>Tick the songs to fix, choose any extra checks, pay, and download the files to upload yourself.</p></li>
      </ol>
      <div class="file-spec">
        <div>
          <h3>What Retrack looks for</h3>
          <p>Four things per song. The headers can be named almost anything.</p>
        </div>
        <table class="columns">
          <thead><tr><th scope="col">Field</th><th scope="col">Headers it recognises</th><th scope="col">Needed</th></tr></thead>
          <tbody>
            <tr><th scope="row">Song title</th><td>Title, Song Name, Track Title</td><td>Yes</td></tr>
            <tr><th scope="row">Artist</th><td>Artist, Performer, Primary Artist</td><td>No</td></tr>
            <tr><th scope="row">ISRC</th><td>ISRC, ISRC code, Optional ISRC</td><td>Yes</td></tr>
            <tr><th scope="row">Writers</th><td>Writers, Composer(s), Songwriter 1, Songwriter 2</td><td>No, but the writer check needs it</td></tr>
          </tbody>
        </table>
      </div>
      <dl class="footnote" aria-label="What each colour means">{colours}</dl>
    </div>
  </section>

  <section class="band" aria-labelledby="band-title">
    <div class="container">
      <h2 id="band-title">Your file stays yours</h2>
      {privacy_list(False, False, mock)}
      <p class="band-note">Two extra checks are off unless you turn them on. The next step shows exactly what each one sends.</p>
    </div>
  </section>"""
    return layout("Retrack, find unpaid song royalties", body, mock)


# ---- 2. Columns ------------------------------------------------------------------------------

def _option_label(i, header):
    return f"{column_letter(i)}: {header.strip() or '(no header)'}"


def _options(headers, selected, none_label="(none)"):
    return f'<option value="">{none_label}</option>' + "".join(
        f'<option value="{i}"{" selected" if i == selected else ""}>{escape(_option_label(i, h))}</option>'
        for i, h in enumerate(headers))


def _sure_text(field, fm):
    if not fm.columns:
        return "Not found. Pick the column." if field in REQUIRED else "Not found. Optional."
    if fm.confidence < LOW_CONFIDENCE:
        return f"Not sure ({fm.confidence:.0%}). Please check."
    return f"{fm.confidence:.0%} sure"


def columns_page(session, mock=False, error=None):
    table, mapping = session.table, session.mapping
    headers = table.headers
    rows = []
    for field in FIELDS:
        fm = mapping[field]
        sure = f'<p class="sure" id="sure-{field}">{escape(_sure_text(field, fm))}</p>'
        if field == "writers":
            # Writers can sit in several columns (Songwriter 1, Songwriter 2...): one dropdown
            # per column found, plus an empty one to add another.
            chosen = list(fm.columns) + [None]
            selects = ""
            for n, col in enumerate(chosen):
                extra = "" if n == 0 else ' aria-label="Writers, another column"'  # only the first has the visible label
                none_label = "(none)" if n == 0 else "(add another column)"
                selects += (f'<select id="map-writers-{n}" name="map_writers" aria-describedby="sure-writers"{extra}>'
                            f"{_options(headers, col, none_label)}</select>")
            rows.append(f'<div class="map-row"><label for="map-writers-0">Writers</label>'
                        f'<div class="selects">{selects}</div>{sure}</div>')
        else:
            rows.append(f'<div class="map-row"><label for="map-{field}">{FIELD_LABELS[field]}</label>'
                        f'<select id="map-{field}" name="map_{field}" aria-describedby="sure-{field}">'
                        f'{_options(headers, fm.columns[0] if fm.columns else None)}</select>{sure}</div>')

    samples = [cells for _, cells in table.rows if any(c.strip() for c in cells)][:SAMPLE_ROWS]
    head = "".join(f'<th scope="col">{escape(_option_label(i, h))}</th>' for i, h in enumerate(headers))
    body_rows = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in samples)

    data_rows = sum(1 for _, cells in table.rows if any(c.strip() for c in cells))

    body = f"""
  <section class="flow" aria-labelledby="columns-title">
    <div class="container">
      <h1 id="columns-title">Check the columns</h1>
      <p class="lead">Retrack read <strong>{escape(session.filename)}</strong>: headers on row {table.header_row}, {data_rows} rows below. Make sure each field points at the right column.</p>
      {alert(*error) if error else ""}
      <form method="post" action="/s/{session.id}/columns">
        <fieldset class="mapping"><legend class="h2">Columns</legend>{"".join(rows)}</fieldset>

        <section class="preview" aria-labelledby="preview-title">
          <h2 id="preview-title">The first {len(samples)} rows of your file</h2>
          <div class="table-scroll" tabindex="0" role="region" aria-labelledby="preview-title">
            <table class="columns"><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table>
          </div>
        </section>

        <p class="helper check-line">{escape(CHECK_LINE)}</p>
        <div class="actions"><button class="btn" type="submit">Start the free check</button>
          <a href="/">Upload a different file</a></div>
      </form>
    </div>
  </section>"""
    return layout("Check the columns, Retrack", body, mock, step="columns")


# ---- The extra checks (chosen on the pay step), and the check running ------------------------

def _switch(field_id, name, label, sentence, checked, mock, required=False):
    note = MOCK_TOGGLE_TEXT if mock else ""
    return (f'<label class="switch"><input type="checkbox" id="{field_id}" name="{name}" value="on"'
            f'{" checked" if checked else ""}{" required" if required else ""}>'
            f"<span><strong>{label}</strong> {escape(sentence + note)}</span></label>")


CHECK_LINE = "The free check looks up each song at The MLC. No AI is used."


def extra_checks(session, mock):
    """The two optional AI checks, chosen on the pay step and run only after checkout,
    with the "what will leave this computer" text for every setting (from data_flows()).
    Only the list for the current settings shows without CSS or JS."""
    s = session.settings
    variants = "".join(
        privacy_list(sn, ai, mock, f' data-privacy="{int(sn)}{int(ai)}"'
                     + ("" if (sn, ai) == (s["smart_names"], s["ai_search"]) else " hidden"))
        for sn in (False, True) for ai in (False, True))
    return f"""<fieldset class="settings"><legend class="h2">Extra checks after checkout (optional)</legend>
          {_switch("smart_names", "smart_names", "Smart names", SMART_NAMES_TEXT, s["smart_names"], mock)}
          {_switch("ai_search", "ai_search", "AI search", AI_SEARCH_TEXT, s["ai_search"], mock)}
        </fieldset>
        <section class="privacy" aria-labelledby="leaves-title">
          <h2 id="leaves-title">What will leave this computer</h2>
          <div aria-live="polite">{variants}</div>
        </section>"""


def progress_text(session):
    if session.total == 0:
        return "Getting ready…"
    n = min(session.done + 1, session.total)
    if session.phase == "finish":
        return f"Working out how to fix song {n} of {session.total}…"
    return f"Checking {n} of {session.total}…"


def progress_page(session, mock=False):
    s = session.settings
    body = f"""
  <section class="flow progress-page" aria-labelledby="progress-title">
    <div class="container">
      <h1 id="progress-title" data-status-url="/s/{session.id}/status" aria-live="polite">{escape(progress_text(session))}</h1>
      <progress id="progress-bar" max="{max(session.total, 1)}" value="{session.done}">{session.done} of {session.total}</progress>
      <p class="lead">{escape(FINISH_LEAD if session.phase == "finish" else SCAN_LEAD)}</p>
      <section class="privacy" aria-labelledby="privacy-title">
        <h2 id="privacy-title">What is leaving this computer</h2>
        {privacy_list(s["smart_names"], s["ai_search"], mock)}
      </section>
    </div>
  </section>"""
    refresh = f'<meta http-equiv="refresh" content="2;url=/s/{session.id}/progress">'
    if session.phase == "finish":
        return layout("Working out your fixes, Retrack", body, mock, step="fix", head_extra=refresh)
    return layout("Checking your catalog, Retrack", body, mock, step="report", head_extra=refresh)


SCAN_LEAD = "Retrack is asking The MLC about each song, one at a time. This page updates by itself; keep this window open."
FINISH_LEAD = ("Paid, thank you. Retrack is now running the extra checks on the songs you paid for, to work out "
               "exactly how to fix each one. This page updates by itself; your fixes open when it’s done.")


# ---- 4. Report -----------------------------------------------------------------------------

def _filter_bar(results):
    counts = {cls: sum(1 for r in results if GROUPS[r.status][0] == cls) for cls, *_ in GROUPS.values()}
    buttons = ['<button type="button" class="chip" data-filter="all" aria-pressed="true">Summary</button>']
    for cls, heading, _, _ in GROUPS.values():
        if counts[cls]:
            buttons.append(f'<button type="button" class="chip is-{cls}" data-filter="{cls}" aria-pressed="false">'
                           f'{heading} <span class="num">{counts[cls]}</span></button>')
    return f"""<div class="filters" role="group" aria-label="Show songs" hidden>
      {"".join(buttons)}
      <button type="button" class="chip chip--quiet" data-expand>Open all</button>
    </div>"""


def _import_notes(session):
    result = session.import_result
    if not result or not result.problems:
        return ""
    items = "".join(f"<li>Row {p.row}: {escape(p.field)}{' “' + escape(p.value) + '”' if p.value else ''}: "
                    f"{escape(p.problem)} ({escape(p.outcome)})</li>" for p in result.problems)
    not_in = result.rows_in - len(result.rows)
    n = len(result.problems)
    not_checked = f", {not_in} {'row' if not_in == 1 else 'rows'} not checked" if not_in else ""
    return f"""<details class="import-notes">
      <summary>{n} {"note" if n == 1 else "notes"} from reading your file{not_checked}</summary>
      <ul>{items}</ul>
      <p><a href="/s/{session.id}/import_problems.csv" download>Download these notes (.csv)</a></p>
    </details>"""


def _needs_search(session, unresolved, mock, error=None):
    """Paid red songs AI search was off for: one callout, with the box that is the
    user's OK to send their titles and writer names, and a button for only those."""
    n = len(unresolved)
    head = f"{n} {'song needs' if n == 1 else 'songs need'} AI search to be worked out"
    why = ("Retrack found no match for these ISRCs. AI search tells whether each song needs registering "
           "or is already registered under another title, since registering it twice creates a duplicate. "
           "You’ve paid for these songs, so this costs nothing more.")
    return f"""<div class="needs-search" role="region" aria-labelledby="needs-title">
        <h3 id="needs-title">{head}</h3>
        <p>{why}</p>
        {alert(*error) if error else ""}
        <form method="post" action="/s/{session.id}/recheck">
          {_switch("recheck-ai", "ai_search", "AI search", AI_SEARCH_NOW_TEXT, False, mock, required=True)}
          <button class="btn btn--quiet btn--small" type="submit">Work out {"this song" if n == 1 else f"these {n} songs"}</button>
        </form>
      </div>"""


def _paid_rows(session, rows):
    if not rows:
        return ""
    items = "".join(f'<li class="fix-paid"><span class="title">{escape(r.row.title)}</span> '
                    f'<span class="fix-kind">Paid</span></li>' for r in rows)
    return (f'<ul class="fix-list paid">{items}</ul>'
            f'<p><a href="/s/{session.id}/fixes">See your fixes</a></p>')


def _fix_section(session, error=None, mock=False):
    paid = set(session.paid_songs)
    fixable = [(i, r) for i, r in enumerate(session.results) if sold_kind(r) and i not in paid]
    paid_rows = [r for i, r in enumerate(session.results) if i in paid]
    if not fixable and not paid_rows:
        return ""
    # Every possible total, worked out by the same price_line() the CLI uses, so the
    # page's script only picks one; it never does the sum itself.
    lines = [price_line([r for _, r in fixable[:n]]) for n in range(len(fixable) + 1)]
    items = "".join(
        f'<li><label class="fix-row"><input type="checkbox" name="song" value="{i}">'
        f'<span class="fix-song"><span class="title">{escape(r.row.title)}</span>'
        f'<span class="artist">{escape(r.row.artist)}</span></span>'
        f'<span class="fix-kind">{escape(KIND_TEXT[sold_kind(r)])}</span></label></li>'
        for i, r in fixable)
    form = ""
    if fixable:
        form = f"""<form id="fix-form" method="post" action="/s/{session.id}/fix" data-lines="{escape(json.dumps(lines))}">
        {alert(*error) if error else ""}
        <ul class="fix-list">{items}</ul>
        <p id="fix-total" class="fix-total" aria-live="polite">${FIX_PRICE_USD:,.2f} per song, the same for every verdict. You pay on the next step.</p>
        <button class="btn" type="submit">Review my fix pack</button>
      </form>"""
    return f"""<section class="fix" aria-labelledby="fix-title">
      <h2 id="fix-title">Fix pack</h2>
      <p class="lead">Tick the songs you want fixed. You get the files to upload to The MLC yourself.</p>
      {form}
      {_paid_rows(session, paid_rows)}
    </section>"""


PAY_NOTICES = {
    "canceled": "Payment canceled. Nothing was charged, and your results are all still here.",
    "failed": "Your payment didn’t go through, so nothing was charged. Tick your songs and try again.",
    "processing": "Stripe is still confirming your payment. Reload this page in a minute to check again.",
}


def _paid_link(session):
    if not session.paid_songs:
        return ""
    return (f'<p class="notice-ok" role="status">You’ve paid for {len(session.paid_songs)} '
            f'{"fix" if len(session.paid_songs) == 1 else "fixes"}. '
            f'<a href="/s/{session.id}/fixes">See your fixes</a></p>')


def results_page(session, mock=False, error=None, notice=""):
    results = session.results
    claude_note = ""
    if session.claude_failures:
        claude_note = ('<p class="notice" role="status"><strong>The AI service didn’t answer for '
                       f"{session.claude_failures} {'check' if session.claude_failures == 1 else 'checks'}.</strong> "
                       "Those songs were checked without it, so some matches may be missing. Nothing is "
                       "wrong with your file. Check again later for the full result.</p>")
    if results and all(r.status == GREEN for r in results):
        list_html = ('<div class="empty-state"><h2>Nothing to fix</h2><p>Every song is registered at The MLC, '
                     "fully claimed, and your writers match. Check again after your next release.</p></div>")
    else:
        list_html = (f'{_filter_bar(results)}'
                     f'<section class="findings-panel" data-summary aria-labelledby="summary-title">'
                     f'<h2 id="summary-title">Summary</h2>{findings_html(results)}</section>'
                     f'<div class="song-list">{groups_html(results, expanded=False)}</div>')
    body = f"""
  <section class="flow results" aria-labelledby="results-title">
    <div class="container">
      <div class="results-head">
        <div>
          <h1 id="results-title">Results for {escape(session.filename)}</h1>
          <p class="results-meta">Checked {date_text()}</p>
        </div>
        <div class="head-actions">
          <a class="btn btn--quiet" href="/s/{session.id}/report.html" download>Download report</a>
          <button type="button" class="btn btn--quiet" data-print hidden>Print</button>
        </div>
      </div>
      {f'<p class="notice" role="status">{escape(PAY_NOTICES[notice])}</p>' if notice in PAY_NOTICES else ""}
      {_paid_link(session)}
      {summary_html(results, mock)}
      {claude_note}
      {_import_notes(session)}
      {list_html}
      {_fix_section(session, error, mock)}
      {privacy_panel(session, mock)}
    </div>
  </section>"""
    return layout(f"Results for {session.filename}, Retrack", body, mock, step="report")


# ---- 5. Pay, then the fix pack ---------------------------------------------------------------

def keep_text(seconds=SESSION_SECONDS):
    """The warning on the fix page, built from the session's real lifetime so it can't drift."""
    minutes = seconds // 60
    return (f"Your fixes stay here for {minutes} {'minute' if minutes == 1 else 'minutes'}, then your "
            "files are deleted for your privacy. Print, download, or screenshot this page to keep it.")


def time_left_text(seconds):
    if seconds <= 0:
        return "Time’s up: your files are being deleted."
    minutes = -(-seconds // 60)  # round up: "about 1 minute" until the very end
    return f"About {minutes} {'minute' if minutes == 1 else 'minutes'} left."


def _time_left(session, seconds_left):
    # Rendered here, so it's right with scripting off; home.js asks /time_left to keep it current.
    return (f'<p class="time-left" data-time-left-url="/s/{session.id}/time_left" aria-live="polite">'
            f"{escape(time_left_text(seconds_left))}</p>")


def payment_privacy(payment_mode):
    if payment_mode == DEMO:
        return ("No payment is taken in demo mode.",
                "Pay (demo) skips Stripe, so nothing is charged and no payment service is contacted.")
    return ("Payments are handled by Stripe.", "Retrack never sees your card number.")


def _pay_unresolved_note(session, plan):
    if not plan.unresolved:
        return ""
    return ('<p class="helper">After checkout, Retrack works out the songs marked “Needs fixing” if you tick '
            "AI search below; otherwise you get the steps to check them by hand. The price doesn’t change.</p>")


def pay_page(session, plan, chosen, mock=False, payment_mode=DEMO, error=None):
    rows = "".join(f'<li><span class="title">{escape(r.row.title)}</span> '
                   f'<span class="fix-kind">{escape(KIND_TEXT[sold_kind(r)])}</span></li>' for r in plan.songs)
    hidden = "".join(f'<input type="hidden" name="song" value="{i}">' for i in chosen)
    banner = {TEST: f'<p class="pay-banner" role="note"><strong>{escape(TEST_BANNER)}</strong></p>',
              DEMO: f'<p class="pay-banner" role="note"><strong>{escape(DEMO_BANNER)}</strong></p>'}.get(payment_mode, "")
    button = "Pay (demo)" if payment_mode == DEMO else "Pay and see your fixes"
    head, sentence = payment_privacy(payment_mode)
    body = f"""
  <section class="flow" aria-labelledby="pay-title">
    <div class="container narrow">
      <h1 id="pay-title">Your fix pack</h1>
      <p class="lead price">{escape(price_line(plan.songs))}</p>
      <ul class="review-list">{rows}</ul>
      {_pay_unresolved_note(session, plan)}
      {banner}
      <form class="checks-form" method="post" action="/s/{session.id}/pay">
        {alert(*error) if error else ""}
        {hidden}
        {extra_checks(session, mock)}
        <label class="attest"><input type="checkbox" name="attest" value="yes" required>
          <span>{escape(ATTESTATION_TEXT)}</span></label>
        <p class="never">{escape(head)} {escape(sentence)} {escape(NEVER_TEXT)}</p>
        <p class="compliance-box">{escape(COMPLIANCE_TEXT)}</p>
        <div class="actions"><button class="btn" type="submit">{button}</button>
          <a href="/s/{session.id}/results">Back to the results</a></div>
      </form>
    </div>
  </section>"""
    return layout("Your fix pack, Retrack", body, mock, step="fix")


MUST_FIELDS = {
    REGISTER: ["Writer IPI", "Writer role code", "Publisher name", "Publisher IPI", "Collection share"],
    LINK: ["Confirm this is your song"],
}
# Each step: (the verb, set bold; the rest of its one short line; the full explanation).
# The short lines are always visible; the full ones sit under one "More detail" per section.
STEPS_FOR = {
    REGISTER: [
        ("Download your fixes", " and open the Register tab.",
         "Download Retrack_Fixes and open the Register tab."),
        ("Fill in every yellow cell", ": the ownership details.",
         "Fill in every yellow cell: writer IPIs, role codes, publisher names and IPIs, and collection "
         "shares. Retrack never guesses these. Enter publishing information only for shares you control."),
        ("Download the MLC template", " from your MLC Portal.",
         "Log in to your MLC Portal yourself, go to Works Registration, then Bulk Registration Tool, and "
         "download the Bulk Work Registration template."),
        ("Copy your rows", " into the template, columns unmoved.",
         "Copy the rows into that template, without moving or deleting its columns, or upload the template "
         "here and Retrack fills a copy for you."),
        ("Upload it", " in the Bulk Registration Tool.",
         "Upload it in the Bulk Registration Tool. If a writer is self-administered with no publisher IPI, "
         "register that song on its own in the Portal instead."),
    ],
    LINK: [
        ("Check the match", " really is your song.",
         "Check that the MLC title and writers really are your song. Retrack only suggests the match."),
        ("Propose the match", " in the Portal’s Matching Tool.",
         "For a few songs: in your MLC Portal, open the Matching Tool, search for your recording and "
         "propose the match to the MLC song code shown."),
        ("Email the bulk match file", " instead, for many songs.",
         "For many songs: download the bulk match file here, delete any row you didn’t confirm, add your "
         "member name to the front of the file name, and email it to bulk.matching@themlc.com. The MLC "
         "says it takes 30 to 90 days."),
    ],
    CLAIM: [
        ("Open the MLC song code", " in your MLC Portal.",
         "Log in to your MLC Portal yourself and open the MLC song code shown."),
        ("Do the song’s own steps", ", then claim your share.",
         "Do the steps listed for the song, then claim your share. Only you know how big it is."),
    ],
    UNRESOLVED: [
        ("Search The MLC", " by this song’s title and writers.",
         "Search The MLC’s public work search (on its website) for the song title and each writer’s name. "
         "Also try without “The” or “(feat. …)”, and with writers’ last names only: registrations often "
         "differ from the release."),
        ("Link it if you find it", " to your recording’s ISRC.",
         "If a registration is clearly your song, don’t register it again: a duplicate slows payment down. "
         "In your MLC Portal, open the Matching Tool, search for your recording and propose the match to "
         "that song code."),
        ("Register it if you don’t", " in your MLC Portal.",
         "If nothing matches, log in to your MLC Portal yourself and register the song (Works Registration), "
         "listing this ISRC on it. Enter publishing information only for shares you control."),
    ],
    ALL_SET: [
        ("Nothing to do", " for this song.",
         "Smart names found that the writer names which looked different are the same people, and "
         "nothing else is wrong with this song at The MLC."),
    ],
}
KIND_HEADS = {REGISTER: "Register", LINK: "Link", CLAIM: "Claim", UNRESOLVED: "Check by hand",
              ALL_SET: "All set"}
KIND_LEADS = {
    REGISTER: "Not at The MLC yet: register these songs.",
    LINK: "Registered, but not linked to your recording: link them.",
    CLAIM: "Registered, but something’s missing: claim or fix them in the Portal.",
    UNRESOLVED: "Couldn’t finish the automatic check; here’s how to check by hand.",
    ALL_SET: "A closer look found nothing to fix.",
}
# When AI search was off, nothing was attempted, so say that instead of "couldn't finish".
LEAD_AI_SEARCH_OFF = ("AI search was off, so Retrack couldn’t tell whether these need registering or "
                      "linking. Here’s how to check by hand, or turn on AI search to work them out.")
# Short meanings for the count row at the top, like Red/Yellow/Green on the results page.
KIND_MEANINGS = {REGISTER: "Not at The MLC yet", LINK: "Registered, not linked", CLAIM: "Registered, something missing",
                 UNRESOLVED: "Not placed yet", ALL_SET: "Nothing to fix"}
# The verdict colour behind each kind of fix (register and link are red songs, claim is yellow).
KIND_COLOURS = {REGISTER: "red", LINK: "red", CLAIM: "yellow", UNRESOLVED: "red", ALL_SET: "green"}


def _facts(pairs):
    return "".join(f"<div><dt>{escape(k)}</dt><dd>{escape(v) if v else '(not known)'}</dd></div>" for k, v in pairs)


def _must(labels):
    return "".join(f'<div class="must"><dt>{escape(k)}</dt><dd>You must fill this in</dd></div>' for k in labels)


def _plural(n, word):
    return f"{n} {word if n == 1 else word + 's'}"


def _fix_card(r, kind):
    """One song as a closed <details> row, like the results page: the summary line says
    how much is filled in and how much is left; opening it shows every field."""
    if kind in (UNRESOLVED, ALL_SET):
        return _plain_card(r, kind)
    if kind == REGISTER:
        filled = [("Work title", r.row.title), ("Writers", ", ".join(r.row.writers)),
                  ("Recording artist", r.row.artist), ("ISRC", r.row.isrc)]
        if r.song_code:
            filled.append(("MLC song code", r.song_code))
        must = list(MUST_FIELDS[REGISTER]) + ([] if r.row.writers else ["Writer names"])
        steps = []
    elif kind == LINK:
        c = r.suggested_match
        filled = [("Your ISRC", r.row.isrc), ("MLC song code", c.song_code), ("MLC title", c.title),
                  ("Writers at The MLC", ", ".join(c.writers)), ("How sure", f"{c.confidence:.0%}")]
        must = MUST_FIELDS[LINK]
        steps = []
    else:
        filled = [("MLC song code", r.song_code), ("Share claimed now", f"{r.claimed_share:g}%"),
                  ("Writers at The MLC", ", ".join(r.mlc_writers)),
                  ("Your writers missing there", ", ".join(r.missing_writers) or "None")]
        must = ["Your share to claim"] + [f"IPI for {w}" for w in r.missing_writers]
        steps = r.fixes  # this song's own steps, from checker.py
    known = sum(1 for _, v in filled if v)
    gist = f"Retrack filled in {_plural(known, 'field')}; you fill in {len(must)}"
    gist += f", plus {_plural(len(steps), 'step')}." if steps else "."
    own = "".join(f"<li>{escape(x)}</li>" for x in steps)
    return f"""<details class="song fix-card">
        <summary>
          <span class="song-name"><span class="title">{escape(r.row.title)}</span>
            <span class="artist">{escape(r.row.artist)}</span></span>
          <span class="gist">{escape(gist)}</span>
          <span class="toggle" aria-hidden="true"></span>
        </summary>
        <div class="song-body">
          <div class="fix-facts">
            <div><h3>What Retrack filled in</h3><dl class="facts">{_facts(filled)}</dl></div>
            <div><h3>What you fill in</h3><dl class="facts must-list">{_must(must)}</dl></div>
          </div>
          {f'<h3>For this song</h3><ol class="song-steps">{own}</ol>' if own else ""}
        </div>
      </details>"""


def _plain_card(r, kind):
    """A paid song with nothing to fill in: one still to check by hand, or one that's all set."""
    facts = [("Title", r.row.title), ("Writers", ", ".join(r.row.writers)), ("ISRC", r.row.isrc)]
    gist = "Not placed yet: check it by hand." if kind == UNRESOLVED else "Nothing to fix after a closer look."
    notes = "".join(f"<li>{escape(n)}</li>" for n in r.notes)
    return f"""<details class="song fix-card">
        <summary>
          <span class="song-name"><span class="title">{escape(r.row.title)}</span>
            <span class="artist">{escape(r.row.artist)}</span></span>
          <span class="gist">{escape(gist)}</span>
          <span class="toggle" aria-hidden="true"></span>
        </summary>
        <div class="song-body">
          <h3>What Retrack knows</h3><dl class="facts">{_facts(facts)}</dl>
          {f'<h3>Notes</h3><ul class="song-steps">{notes}</ul>' if notes else ""}
        </div>
      </details>"""


def _how_to(kind, lead=None):
    """Short steps, each one line starting with a bold verb, then one "More detail"
    disclosure with the full explanation of every step (a <details>: no script needed)."""
    short = "".join(f"<li><strong>{escape(verb)}</strong>{escape(rest)}</li>" for verb, rest, _ in STEPS_FOR[kind])
    full = "".join(f"<li>{escape(detail)}</li>" for _, _, detail in STEPS_FOR[kind])
    return f"""<div class="how">
          <h3 class="steps-head">How to do it</h3>
          <p class="group-lead">{escape(lead or KIND_LEADS[kind])}</p>
          <ol class="how-to">{short}</ol>
          <details class="more"><summary>More detail</summary><ol class="how-to-full">{full}</ol></details>
        </div>"""


def _mlc_files(session, kind, template_note, songs=(), mock=False, recheck_error=None):
    """The file for The MLC that belongs to this kind of fix, after its steps. For songs
    still to check by hand that AI search was off for: the callout to turn it on."""
    if kind == UNRESOLVED:
        return "" if session.settings["ai_search"] else _needs_search(session, songs, mock, recheck_error)
    if kind == LINK:
        return (f'<div class="mlc-file"><h3 class="steps-head">File for The MLC</h3>'
                f'<p><a href="/s/{session.id}/bulk_match.csv" download data-no-warn>{escape(bulk_match_name())}</a></p>'
                "<p class=\"helper\">The links in The MLC’s exact Bulk Match format. You’d need it to link many "
                "songs at once by email, instead of one by one in the Matching Tool.</p></div>")
    if kind == REGISTER:
        # Optional: Retrack never supplies the template itself; the user brings their own copy.
        copy_link = ""
        if session_template_ready(session):
            copy_link = (f'<p><a class="btn btn--quiet btn--small" href="/s/{session.id}/bulk_work_registration.xlsx" '
                         'download data-no-warn>Download your filled-in template</a></p>')
        note = f'<p class="helper">{escape(template_note)}</p>' if template_note else ""
        return f"""<div class="mlc-file mlc-file--optional"><h3 class="steps-head">File for The MLC</h3>
          <form method="post" action="/s/{session.id}/template" enctype="multipart/form-data">
            <label for="template">Lots of songs? Upload your MLC template and Retrack fills a copy.</label>
            <p id="template-help" class="helper">Optional. Download it from your MLC Portal (Works Registration,
            then Bulk Registration Tool). Retrack leaves your original alone.</p>
            <input type="file" id="template" name="template" accept=".xlsx" required aria-describedby="template-help">
            <button class="btn btn--quiet btn--small" type="submit">Fill in my template</button>
          </form>{note}{copy_link}</div>"""
    return ""


def _fix_filters(kinds):
    """Tabs like the results page: All, then one per kind of fix. Hidden until the script runs,
    so with scripting off every group simply shows, one after another."""
    buttons = ['<button type="button" class="chip" data-filter="all" aria-pressed="true">All fixes</button>']
    for kind, songs in kinds:
        buttons.append(f'<button type="button" class="chip is-{KIND_COLOURS[kind]}" data-filter="{kind}" '
                       f'aria-pressed="false">{KIND_HEADS[kind]} <span class="num">{len(songs)}</span></button>')
    return f"""<div class="filters" role="group" aria-label="Show fixes" hidden>
      {"".join(buttons)}
      <button type="button" class="chip chip--quiet" data-expand>Open all</button>
    </div>"""


def fixes_page(session, plan, mock=False, template_note="", error=None, seconds_left=SESSION_SECONDS,
               lifetime=SESSION_SECONDS, recheck_error=None):
    kinds = [(kind, songs) for kind, songs in plan.by_kind() if songs]
    # Only kinds with songs: no "0" tiles, tabs or sections.
    ledger = "".join(
        f'<div class="is-{KIND_COLOURS[kind]}"><dt>{KIND_HEADS[kind]}</dt>'
        f'<dd><span class="figure">{len(songs)}</span><span class="meaning">{KIND_MEANINGS[kind]}</span></dd></div>'
        for kind, songs in kinds)
    groups = ""
    for kind, songs in kinds:
        # Songs first, then how to do it, then the file for The MLC.
        groups += f"""
      <section class="group fix-group is-{KIND_COLOURS[kind]}" data-status="{kind}" aria-labelledby="group-{kind}">
        <div class="group-head"><h2 id="group-{kind}">{KIND_HEADS[kind]}</h2>
          <span class="count">{_plural(len(songs), "song")}</span></div>
        {"".join(_fix_card(r, kind) for r in songs)}
        <div class="fix-how">
          {_how_to(kind, LEAD_AI_SEARCH_OFF if kind == UNRESOLVED and not session.settings["ai_search"] else None)}
          {_mlc_files(session, kind, template_note, songs, mock, recheck_error)}
        </div>
      </section>"""

    # One warning box at the top, with the downloads in it: so it's both "at the top" and
    # "by the downloads" without saying the same thing twice.
    body = f"""
  <section class="flow results fixes" data-warn-leave aria-labelledby="fixes-title">
    <div class="container">
      <div class="results-head">
        <div>
          <h1 id="fixes-title">Your fixes</h1>
          <p class="results-meta">Paid: {escape(price_line(plan.songs))}</p>
        </div>
      </div>
      <p class="lead">Everything Retrack could fill in is below; the highlighted fields are yours to fill in.</p>
      {alert(*error) if error else ""}
      {_finish_notes(session)}
      <div class="vanish keep" role="note">
        <div><p>{escape(keep_text(lifetime))}</p>{_time_left(session, seconds_left)}</div>
        <div class="actions">
          <a class="btn btn--small" href="/s/{session.id}/fixes.xlsx" download data-no-warn>Download fixes (.xlsx)</a>
          <button type="button" class="btn btn--quiet btn--small" data-print hidden>Print</button>
        </div>
      </div>
      <dl class="ledger ledger--fit" aria-label="Fixes by type">{ledger}</dl>
      {_fix_filters(kinds)}
      <div class="song-list">{groups}</div>
      <p class="compliance-box">{escape(COMPLIANCE_TEXT)}</p>
      {privacy_panel(session, mock)}
    </div>
  </section>"""
    return layout("Your fixes, Retrack", body, mock, step="fix")


def _finish_notes(session):
    """Why some paid songs couldn't be worked out after checkout, if anything went wrong."""
    notes = []
    if session.finish_problem:
        notes.append(session.finish_problem)
    if session.claude_failures:
        n = session.claude_failures
        notes.append(f"The AI service didn’t answer for {n} {'check' if n == 1 else 'checks'} (or a spending "
                     "limit was reached), so those songs are marked for checking by hand.")
    return "".join(f'<p class="notice" role="status">{escape(n)}</p>' for n in notes)


def session_template_ready(session):
    return (session.folder / "bulk_work_registration.xlsx").exists()


def privacy_page(mock=False, payment_mode=DEMO):
    head, sentence = payment_privacy(payment_mode)
    items = privacy_items(False, False, mock) + [(head, sentence)]
    lst = "".join(f"<li><strong>{escape(h)}</strong><span>{escape(t)}</span></li>" for h, t in items)
    body = f"""
  <section class="flow" aria-labelledby="privacy-page-title">
    <div class="container narrow">
      <h1 id="privacy-page-title">Privacy</h1>
      <p class="lead">What happens to your catalog, and what leaves this computer.</p>
      <ul class="privacy-list single">{lst}</ul>
      <p class="helper">Smart names and AI search are off unless you turn them on; the pay step shows
      exactly what each one sends before you pay. Uploads are deleted after 60 minutes, when you press
      Delete my files now, or when Retrack stops.</p>
      <div class="actions"><a class="btn" href="/">Back to Retrack</a></div>
    </div>
  </section>"""
    return layout("Privacy, Retrack", body, mock)


# ---- Errors --------------------------------------------------------------------------------

def error_page(title, message, mock=False, step=None, action='<a class="btn" href="/">Start again</a>'):
    body = f"""
  <section class="flow" aria-labelledby="error-title">
    <div class="container narrow">
      <div class="alert" role="alert"><h1 id="error-title">{escape(title)}</h1><p>{escape(message)}</p></div>
      <div class="actions">{action}</div>
    </div>
  </section>"""
    return layout(f"{title}, Retrack", body, mock, step=step)
