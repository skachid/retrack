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
from .fixes import CLAIM, FIX_PRICE_USD, LINK, REGISTER, TEMPLATE_HELP, fix_kind, price_line
from .importer import FIELDS, LOW_CONFIDENCE, REQUIRED, SAMPLE_ROWS, column_letter
from .privacy import privacy_items
from .report import CSS_PATH, GROUPS, MANUAL_CHECKS, date_text, findings_html, groups_html, summary_html

HERE = Path(__file__).parent

STEPS = [("upload", "Upload"), ("columns", "Columns"), ("check", "Check"), ("report", "Report"),
         ("fix", "Fix pack")]
FIELD_LABELS = {"title": "Song title", "artist": "Artist", "isrc": "ISRC", "writers": "Writers"}
KIND_TEXT = {
    REGISTER: "Register it: rows for The MLC’s Bulk Work Registration template",
    LINK: "Link it: a match file to link your recording to the song",
    CLAIM: "Claim it: a checklist of steps in The MLC Portal",
}
# The page says "an AI service", not the provider's name: users need to know it's an
# outside AI and exactly what it gets (privacy.py), not which one.
SMART_NAMES_TEXT = ("When a writer’s name doesn’t match exactly, Retrack sends the two writer "
                    "names to an AI service to ask if they’re the same person.")
AI_SEARCH_TEXT = ("For red songs and songs with missing writers, Retrack sends the song title and "
                  "writer names to The MLC’s song search and to an AI service, to find registrations "
                  "that aren’t linked to your ISRC.")
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
                 "<strong>Sample data, not real results.</strong> Retrack is running with --mock: "
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
      <p class="lead">Five steps, about five minutes.</p>
      <ol class="how-steps">
        <li><h3>Upload</h3><p>Your spreadsheet as it is: a distributor export or your own sheet.</p></li>
        <li><h3>Check the columns</h3><p>Retrack shows which column it read as the title, ISRC and writers.</p></li>
        <li><h3>Read the report</h3><p>Red songs come first, each with what’s wrong and how to fix it.</p></li>
        <li><h3>Get the fix pack</h3><p>Tick the songs to fix and download the files to upload yourself.</p></li>
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

        <div class="actions"><button class="btn" type="submit">Next: choose the checks</button>
          <a href="/">Upload a different file</a></div>
      </form>
    </div>
  </section>"""
    return layout("Check the columns, Retrack", body, mock, step="columns")


# ---- 3. Check: choose the extra checks, then run ---------------------------------------------

def _switch(field_id, name, label, sentence, checked, mock, required=False):
    note = MOCK_TOGGLE_TEXT if mock else ""
    return (f'<label class="switch"><input type="checkbox" id="{field_id}" name="{name}" value="on"'
            f'{" checked" if checked else ""}{" required" if required else ""}>'
            f"<span><strong>{label}</strong> {escape(sentence + note)}</span></label>")


def check_page(session, mock=False, error=None):
    s = session.settings
    variants = "".join(
        privacy_list(sn, ai, mock, f' data-privacy="{int(sn)}{int(ai)}"'
                     + ("" if (sn, ai) == (s["smart_names"], s["ai_search"]) else " hidden"))
        for sn in (False, True) for ai in (False, True))
    n = len(session.rows)
    body = f"""
  <section class="flow" aria-labelledby="check-title">
    <div class="container">
      <h1 id="check-title">Check your songs</h1>
      <p class="lead">{n} {"song is" if n == 1 else "songs are"} ready to check with The MLC.</p>
      {_import_notes(session)}
      {alert(*error) if error else ""}
      <form id="scan-form" class="scan-form" method="post" action="/s/{session.id}/scan">
        <fieldset class="settings"><legend class="h2">Extra checks (optional)</legend>
          {_switch("smart_names", "smart_names", "Smart names", SMART_NAMES_TEXT, s["smart_names"], mock)}
          {_switch("ai_search", "ai_search", "AI search", AI_SEARCH_TEXT, s["ai_search"], mock)}
        </fieldset>
        <section class="privacy" aria-labelledby="leaves-title">
          <h2 id="leaves-title">What will leave this computer</h2>
          <div aria-live="polite">{variants}</div>
        </section>
        <div class="actions"><button class="btn" type="submit">Start the check</button>
          <a href="/s/{session.id}/columns">Back to the columns</a></div>
      </form>
    </div>
  </section>"""
    return layout("Check your songs, Retrack", body, mock, step="check")


def progress_text(session):
    if session.total == 0:
        return "Getting ready…"
    return f"Checking {min(session.done + 1, session.total)} of {session.total}…"


def progress_page(session, mock=False):
    s = session.settings
    body = f"""
  <section class="flow progress-page" aria-labelledby="progress-title">
    <div class="container">
      <h1 id="progress-title" data-status-url="/s/{session.id}/status" aria-live="polite">{escape(progress_text(session))}</h1>
      <progress id="progress-bar" max="{max(session.total, 1)}" value="{session.done}">{session.done} of {session.total}</progress>
      <p class="lead">Retrack is asking The MLC about each song, one at a time. This page updates by itself; keep this window open.</p>
      <section class="privacy" aria-labelledby="privacy-title">
        <h2 id="privacy-title">What is leaving this computer</h2>
        {privacy_list(s["smart_names"], s["ai_search"], mock)}
      </section>
    </div>
  </section>"""
    refresh = f'<meta http-equiv="refresh" content="2;url=/s/{session.id}/progress">'
    return layout("Checking your catalog, Retrack", body, mock, step="check", head_extra=refresh)


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


def _needs_search(session, unfixable_red, mock, error=None):
    """Red songs fix_kind() can't place yet: AI search tells "register" from "link"."""
    n = len(unfixable_red)
    songs = "".join(f'<li><span class="title">{escape(r.row.title)}</span> '
                    f'<span class="artist">{escape(r.row.artist)}</span></li>' for r in unfixable_red)
    if session.settings["ai_search"]:
        head = f"{n} {'song' if n == 1 else 'songs'} couldn’t be searched fully"
        why = ("The title search didn’t finish for these (The MLC or the AI service didn’t answer), so "
               "Retrack can’t yet tell whether they need registering. Try again in a few minutes.")
    else:
        head = f"{n} {'song needs' if n == 1 else 'songs need'} AI search first"
        why = ("Retrack found no match for these ISRCs. AI search tells whether each song needs registering "
               "or is already registered under another title, since registering it twice creates a duplicate.")
    return f"""<div class="needs-search" role="region" aria-labelledby="needs-title">
        <h3 id="needs-title">{head}</h3>
        <p>{why}</p>
        <ul class="needs-list">{songs}</ul>
        {alert(*error) if error else ""}
        <form method="post" action="/s/{session.id}/recheck">
          {_switch("recheck-ai", "ai_search", "AI search", AI_SEARCH_TEXT, session.settings["ai_search"], mock, required=True)}
          <button class="btn btn--quiet" type="submit">Check {"this song" if n == 1 else f"these {n} songs"} again</button>
        </form>
      </div>"""


def _fix_section(session, error=None, mock=False, recheck_error=None):
    fixable = [(i, r) for i, r in enumerate(session.results) if fix_kind(r)]
    unfixable_red = [r for r in session.results if r.status == RED and not fix_kind(r)]
    if not fixable and not unfixable_red:
        return ""
    # Every possible total, worked out by the same price_line() the CLI uses, so the
    # page's script only picks one; it never does the sum itself.
    lines = [price_line([r for _, r in fixable[:n]]) for n in range(len(fixable) + 1)]
    items = "".join(
        f'<li><label class="fix-row"><input type="checkbox" name="song" value="{i}">'
        f'<span class="fix-song"><span class="title">{escape(r.row.title)}</span>'
        f'<span class="artist">{escape(r.row.artist)}</span></span>'
        f'<span class="fix-kind">{escape(KIND_TEXT[fix_kind(r)])}</span></label></li>'
        for i, r in fixable)
    form = ""
    if fixable:
        form = f"""<form id="fix-form" method="post" action="/s/{session.id}/fix" data-lines="{escape(json.dumps(lines))}">
        {alert(*error) if error else ""}
        <ul class="fix-list">{items}</ul>
        <p id="fix-total" class="fix-total" aria-live="polite">${FIX_PRICE_USD:,.2f} per song, the same for every verdict. No payment is taken yet.</p>
        <button class="btn" type="submit">Review my fix pack</button>
      </form>"""
    return f"""<section class="fix" aria-labelledby="fix-title">
      <h2 id="fix-title">Fix pack</h2>
      <p class="lead">Tick the songs you want fixed. You get the files to upload to The MLC yourself.</p>
      {_needs_search(session, unfixable_red, mock, recheck_error) if unfixable_red else ""}
      {form}
    </section>"""


def results_page(session, mock=False, error=None, recheck_error=None):
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
      {summary_html(results, mock)}
      {claude_note}
      {_import_notes(session)}
      {list_html}
      {_fix_section(session, error, mock, recheck_error)}
      {privacy_panel(session, mock)}
    </div>
  </section>"""
    return layout(f"Results for {session.filename}, Retrack", body, mock, step="report")


# ---- 5. Fix pack ---------------------------------------------------------------------------

def review_page(session, plan, chosen, mock=False, error=None):
    rows = "".join(f"<li><span class=\"title\">{escape(r.row.title)}</span> "
                   f"<span class=\"fix-kind\">{escape(KIND_TEXT[fix_kind(r)])}</span></li>" for r in plan.songs)
    hidden = "".join(f'<input type="hidden" name="song" value="{i}">' for i in chosen)
    template = ""
    if plan.register:
        template = f"""<div class="field">
          <label for="template">Your MLC template (optional)</label>
          <input type="file" id="template" name="template" accept=".xlsx" aria-describedby="template-help">
          <p id="template-help" class="helper">{escape(TEMPLATE_HELP.replace(' Then run Retrack again with --template path/to/template.xlsx.', ''))} Without it, you get a worksheet to copy from.</p>
        </div>"""
    body = f"""
  <section class="flow" aria-labelledby="review-title">
    <div class="container narrow">
      <h1 id="review-title">Your fix pack</h1>
      <p class="lead price">{escape(price_line(plan.songs))}</p>
      <ul class="review-list">{rows}</ul>
      <form method="post" action="/s/{session.id}/fix/download" enctype="multipart/form-data">
        {alert(*error) if error else ""}
        {hidden}
        {template}
        <label class="attest"><input type="checkbox" name="attest" value="yes" required>
          <span>{escape(ATTESTATION_TEXT)}</span></label>
        <p class="never">{escape(NEVER_TEXT)}</p>
        <p class="compliance-box">{escape(COMPLIANCE_TEXT)}</p>
        <div class="actions"><button class="btn" type="submit">Download fix pack (.zip)</button>
          <a href="/s/{session.id}/results">Back to the results</a></div>
      </form>
    </div>
  </section>"""
    return layout("Your fix pack, Retrack", body, mock, step="fix")


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
