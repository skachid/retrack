"""Turns results into something a human can read: terminal summary, CSV and HTML."""

import csv
import datetime
from html import escape
from pathlib import Path

from .checker import GREEN, RED, RED_CASES, YELLOW
from .compliance import COMPLIANCE_TEXT

ICONS = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴"}
ORDER = {RED: 0, YELLOW: 1, GREEN: 2}

# Sites with no public API: we link to them for a manual check (never scrape them).
MANUAL_CHECKS = [
    ("SoundExchange ISRC search", "https://isrc.soundexchange.com/"),
    ("ASCAP/BMI Songview", "https://repertoire.bmi.com/"),
]


def suggestion_heading(r):
    """Every AI suggestion is labelled "please confirm": it's a lead, not a verdict."""
    if r.status == RED:
        return "Possible match at The MLC, please confirm"
    return "Possible second registration, please confirm"


def suggestion_text(r):
    c = r.suggested_match
    if c is None:
        return ""
    writers = ", ".join(c.writers) or "no writers listed"
    return (f'{suggestion_heading(r)} ({c.confidence:.0%} sure): song code {c.song_code}, '
            f"“{c.title}” by {writers}")


def print_summary(results):
    counts = {s: sum(1 for r in results if r.status == s) for s in (RED, YELLOW, GREEN)}
    print(f"\nChecked {len(results)} songs: "
          f"{counts[RED]} red, {counts[YELLOW]} yellow, {counts[GREEN]} green\n")
    for r in sorted(results, key=lambda r: ORDER[r.status]):
        print(f"{ICONS[r.status]} {r.row.title} — {r.row.artist} ({r.row.isrc})")
        if r.red_case:
            print(f"     {RED_CASES[r.red_case]}")
        for p in r.problems:
            print(f"     ✗ {p}")
        for fix in r.fixes:
            print(f"     → {fix}")
        for note in r.notes:
            print(f"     ≈ {note}")
        if r.suggested_match:
            print(f"     ? {suggestion_text(r)}")
    print("\nAlso check by hand (no public API for these):")
    for name, url in MANUAL_CHECKS:
        print(f"  • {name + ':':<26} {url}")


def write_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "title", "artist", "isrc", "mlc_song_code", "iswc",
                    "claimed_share_pct", "mlc_writers", "problems", "fixes", "notes",
                    "red_case", "suggested_song_code", "suggested_title", "suggested_writers",
                    "suggested_confidence"])
        for r in sorted(results, key=lambda r: ORDER[r.status]):
            w.writerow([r.status, r.row.title, r.row.artist, r.row.isrc, r.song_code, r.iswc,
                        r.claimed_share, "; ".join(r.mlc_writers),
                        " | ".join(r.problems), " | ".join(r.fixes), " | ".join(r.notes),
                        RED_CASES.get(r.red_case, ""), *_suggestion_columns(r.suggested_match)])


def _suggestion_columns(c):
    if c is None:
        return ["", "", "", ""]
    return [c.song_code, c.title, "; ".join(c.writers), f"{c.confidence:.2f}"]


# ---- HTML report -------------------------------------------------------------
#
# One self-contained file: the CSS is pasted into a <style> tag, there is no
# JavaScript and nothing is loaded from the internet, so it opens offline and
# survives being sent as an email attachment.
#
# Every piece of catalog text goes through escape() before it lands in the HTML.
# A song called "<b>Hits</b>" must show up as those characters, not as bold text
# (and a nastier title can't sneak a script into the page). That's called
# "escaping output".

CSS_PATH = Path(__file__).parent / "report.css"

GROUPS = {
    # status: (css class, group heading, short verdict, what the colour means)
    RED: ("red", "Needs action", "Red", "Money is probably stuck"),
    YELLOW: ("yellow", "Needs a look", "Yellow", "Registered, but something is off"),
    GREEN: ("green", "All set", "Green", "Registered and fully claimed"),
}


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _lead_sentence(counts, total):
    if total == 0:
        return "There were no songs with an ISRC in the catalog file."
    parts = []
    if counts[RED]:
        parts.append(f"{_plural(counts[RED], 'song')} {'needs' if counts[RED] == 1 else 'need'} action")
    if counts[YELLOW]:
        parts.append(f"{counts[YELLOW]} {'needs' if counts[YELLOW] == 1 else 'need'} a closer look")
    if counts[GREEN]:
        parts.append(f"{counts[GREEN]} {'is' if counts[GREEN] == 1 else 'are'} all set")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1] + "."
    return parts[0][0].upper() + parts[0][1:] + "."


def _gist(r):
    """The one line a collapsed row shows: the main problem, or that all is well."""
    if r.problems:
        more = len(r.problems) - 1
        extra = f' <span class="more">and {more} more</span>' if more else ""
        return escape(r.problems[0]) + extra
    if r.notes:
        return f"Nothing to fix. {_plural(len(r.notes), 'name')} to confirm."
    return "Nothing to fix."


def _suggestion_html(r):
    """The suggested MLC song, in its own box, always labelled "please confirm"."""
    c = r.suggested_match
    if c is None:
        return ""
    facts = [("MLC song code", f"<code>{escape(c.song_code)}</code>"),
             ("MLC title", escape(c.title)),
             ("Writers at The MLC", escape(", ".join(c.writers) or "None listed"))]
    if c.iswc:
        facts.insert(1, ("ISWC", f"<code>{escape(c.iswc)}</code>"))
    facts_html = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
    reason = f": {escape(c.reason)}" if c.reason else ""
    why = f'<p class="why">How sure: <span class="num">{c.confidence:.0%}</span>{reason}</p>'
    others = ""
    if r.other_matches:
        codes = ", ".join(o.song_code for o in r.other_matches)
        others = f'<p class="why">Also possible: {escape(codes)}.</p>'
    return f"""<div class="suggestion">
          <h3>{escape(suggestion_heading(r))}</h3>
          <dl class="facts">{facts_html}</dl>
          {why}{others}
        </div>"""


def _song_card(r, expanded=True):
    """One song as a <details> row: click the summary line to open the details.

    <details> is built into HTML, so opening and closing works without JavaScript.
    """
    cls = GROUPS[r.status][0]
    facts = [("ISRC", f'<code>{escape(r.row.isrc)}</code>')]
    if r.song_code:
        facts.append(("MLC song code", f'<code>{escape(r.song_code)}</code>'))
    if r.iswc:
        facts.append(("ISWC", f'<code>{escape(r.iswc)}</code>'))
    if r.status != RED:
        facts.append(("Share claimed", f'<span class="num">{r.claimed_share:g}%</span>'))
    if r.row.writers:
        facts.append(("Your writers", escape(", ".join(r.row.writers))))
    if r.mlc_writers:
        facts.append(("Writers at The MLC", escape(", ".join(r.mlc_writers))))
    facts_html = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)

    body = _suggestion_html(r)  # first: the fix steps below refer to it
    if r.problems or r.fixes:
        wrong = "".join(f"<li>{escape(p)}</li>" for p in r.problems)
        fixes = "".join(f"<li>{escape(f)}</li>" for f in r.fixes)
        body += '<div class="steps">'
        if wrong:
            body += f'<div class="wrong"><h3>What we found</h3><ul>{wrong}</ul></div>'
        if fixes:
            body += f'<div class="fix"><h3>How to fix it</h3><ol>{fixes}</ol></div>'
        body += "</div>"
    else:
        body += '<p class="all-good">Nothing to fix.</p>'
    for note in r.notes:
        body += f'<p class="note"><strong>Worth confirming:</strong> {escape(note)}</p>'

    case = RED_CASES.get(r.red_case, "")
    case_html = f'<span class="case">{escape(case)}</span>' if case else ""

    return f"""
    <details class="song is-{cls}"{" open" if expanded else ""}>
      <summary>
        <span class="song-name"><span class="title">{escape(r.row.title)}</span><span class="artist">{escape(r.row.artist)}</span>{case_html}</span>
        <span class="gist">{_gist(r)}</span>
        <span class="toggle" aria-hidden="true"></span>
      </summary>
      <div class="song-body">
        <dl class="facts">{facts_html}</dl>
        {body}
      </div>
    </details>"""


def summary_html(results, mock=False):
    """The lead sentence, the sample-data notice and the red/yellow/green ledger."""
    counts = {s: sum(1 for r in results if r.status == s) for s in (RED, YELLOW, GREEN)}
    ledger = "".join(
        f'<div class="is-{GROUPS[s][0]}"><dt>{GROUPS[s][2]}</dt>'
        f'<dd><span class="figure">{counts[s]}</span>'
        f'<span class="meaning">{GROUPS[s][3]}</span></dd></div>'
        for s in (RED, YELLOW, GREEN)
    )
    notice = ""
    if mock:
        notice = ('<p class="notice"><strong>Sample data, not real results.</strong> These come from '
                  "Retrack’s built-in test data, not from The MLC. Run without --mock to check "
                  "your real registrations.</p>")
    return f"""<p class="lead">{escape(_lead_sentence(counts, len(results)))}</p>
    {notice}
    <dl class="ledger" aria-label="Summary">{ledger}</dl>"""


def groups_html(results, expanded=True):
    """One section per colour (red first), each holding its song rows.

    expanded=True opens every row (the saved report: paper can't be clicked).
    The homepage passes False so a catalog reads as a short list first.
    """
    if not results:
        return ('<p class="empty">No songs to show. Check that your CSV has an '
                "<code>isrc</code> column and that it isn’t empty.</p>")
    out = ""
    for status in (RED, YELLOW, GREEN):
        songs = [r for r in results if r.status == status]
        if not songs:
            continue
        cls, heading, _, _ = GROUPS[status]
        cards = "".join(_song_card(r, expanded) for r in songs)
        out += f"""
  <section class="group is-{cls}" data-status="{cls}" aria-labelledby="group-{cls}">
    <div class="group-head"><h2 id="group-{cls}">{heading}</h2><span class="count">{_plural(len(songs), 'song')}</span></div>
    {cards}
  </section>"""
    return out


def date_text(day=None):
    day = day or datetime.date.today()
    return f"{day.day} {day:%B %Y}"


def render_html(results, catalog_name="", mock=False, checked_on=None):
    """Build the whole report as one HTML string (red songs first)."""
    checked = date_text(checked_on)
    manual = "".join(f'<li><a href="{escape(url)}">{escape(name)}</a></li>' for name, url in MANUAL_CHECKS)
    catalog_text = f" for {escape(catalog_name)}" if catalog_name else ""
    css = CSS_PATH.read_text(encoding="utf-8")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Which songs in this catalog are missing or incomplete at The MLC, and how to fix each one.">
<title>Catalog check, {checked}</title>
<style>
{css}
</style>
</head>
<body>
<header class="bar">
  <div class="container">
    <span class="wordmark">Retrack</span>
    <span class="bar-meta">Checked {checked}{catalog_text}</span>
  </div>
</header>
<main class="container">
  <div class="intro">
    <h1>Your catalog at The MLC</h1>
    {summary_html(results, mock)}
  </div>
  {groups_html(results)}
</main>
<footer class="foot">
  <div class="container">
    <h2>Also check by hand</h2>
    <p>The MLC covers streaming mechanicals in the US. These have no public API, so look your songs up there yourself:</p>
    <ul>{manual}</ul>
    <p class="small">Retrack only reads registration data from The MLC’s Public Search API.
    It never touches your audio and never trains on your catalog.</p>
    <p class="small compliance">{escape(COMPLIANCE_TEXT)}</p>
  </div>
</footer>
</body>
</html>
"""


def write_html(results, path, **kwargs):
    Path(path).write_text(render_html(results, **kwargs), encoding="utf-8")
