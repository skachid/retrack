# Retrack

Finds songs in a music catalog that aren't properly registered with **The MLC** (the US body that pays streaming mechanical royalties), so the money stops sitting unclaimed.

Give it a CSV of songs, and every song comes back as:

| | Meaning |
|---|---|
| 🔴 RED | The MLC has no record of the recording, or it isn't linked to a song. Money is probably stuck. |
| 🟡 YELLOW | The song is there, but shares don't add to 100%, nobody's collecting, or writers are missing. |
| 🟢 GREEN | Registered, fully claimed, writers match. |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# With fake data (no API access needed):
python -m retrack sample_catalog.csv --mock

# With the real MLC API (after you're approved):
cp .env.example .env   # fill in your MLC login
export $(cat .env | xargs)
python -m retrack your_catalog.csv --out report.csv
```

Run the tests: `pytest`

## The homepage (upload a CSV, see results)

The easiest way in: a web page where you drop your catalog CSV and the results appear right below it.

```bash
python -m retrack.web --mock   # try it with fake MLC data, no login needed
python -m retrack.web          # the real thing (needs MLC_USERNAME / MLC_PASSWORD)
```

Your browser opens **http://localhost:8000**. The page walks you through getting the file ready, has a blank template to download, and shows each song's red/yellow/green verdict with fix steps once you press **Check my catalog**. Press **Ctrl+C** in the terminal to stop it. Like `--serve`, it only answers this computer, keeps no copy of the file, and up to 500 songs are checked at a time. Use `--port 8001` if 8000 is taken.

## Read the report in your browser

Retrack can also make a readable HTML report: a summary of red/yellow/green counts, then one card per song with what's wrong and how to fix it (red first).

```bash
# Save it as a file you can open, print or email:
python -m retrack sample_catalog.csv --mock --html report.html

# Or preview it straight away:
python -m retrack sample_catalog.csv --mock --serve
```

`--serve` runs the check, then opens **http://localhost:8000** in your default browser. Leave the terminal open while you look; press **Ctrl+C** to stop. Only your own computer can see the page (other devices on your Wi-Fi can't), and it serves the report only, never other files in this folder.

The HTML file is self-contained: no JavaScript, no web fonts, nothing loaded from the internet. It works offline, as an email attachment, and prints cleanly (each song stays on one page).

If you see "Port 8000 is already in use", an earlier `--serve` is probably still running in another terminal window. Press Ctrl+C there first.

## Catalog CSV format

```csv
title,artist,isrc,writers
Paper Crowns,Nova Reyes,QZ-ABC-24-00002,Nova Reyes; Marcus Bell
```

Writers go in one column, separated by `;`. Dashes and spaces in ISRCs are fine.

## How it works

```
catalog.csv ─► checker.py ─► mlc_client.py ─► The MLC API
                   │            1. POST /search/recordings  (ISRC → song code)
                   │            2. POST /works              (song code → writers, shares)
                   ▼
              report.py ─► terminal summary + report.csv (+ report.html)
```

| File | What it does |
|---|---|
| `retrack/mlc_client.py` | Logs in to The MLC and runs lookups. `MockClient` fakes it using `fixtures/mlc_mock.json`. |
| `retrack/checker.py` | The rules that decide red/yellow/green and the fix steps. |
| `retrack/name_matcher.py` | Second-pass name check: `ClaudeNameMatcher` (Claude API) and `MockNameMatcher` (offline rules). |
| `retrack/report.py` | Prints the summary, writes the CSV and builds the HTML report. |
| `retrack/report.css` | The HTML report's styles. They're pasted into the HTML file, so it needs nothing else. |
| `retrack/web.py` | The homepage: upload form, `/check` (runs the check on an uploaded CSV), `/template.csv`. |
| `retrack/home.css`, `retrack/home.js` | The homepage's styles and its small script (drag and drop, results without a reload). The page still works with JavaScript off. |
| `retrack/preview.py` | `--serve`: a tiny local web server (Python's built-in `http.server`) for the HTML report. |
| `tests/test_checker.py` | One test per verdict, plus name and ISRC cleanup. |
| `tests/test_web.py` | The homepage: uploads, errors, results on the same page, refusing other websites. |
| `tests/test_html_report.py` | The HTML report (order, counts, escaping, offline, print) and the preview server. |

## Getting real API access

Apply for The MLC's **Public Search API** on their Data Programs page: https://www.themlc.com/dqi

## Known limits (v1)

- Only checks The MLC. SoundExchange and ASCAP/BMI Songview have no public API, so the report links to them for a manual check.
- Name matching is basic by default. "J. Okafor" won't match "Jide Okafor" unless you add `--smart-names`, which asks the Claude API about names that don't match exactly. Only the two writer names are sent. It needs `ANTHROPIC_API_KEY` and `pip install anthropic`. `--mock` uses offline rules instead, so it needs no key.
- If one ISRC links to several works, only the first is checked.

## Roadmap

1. ✅ CLI script + mock mode
2. ✅ Smarter writer-name matching with the Claude API (`--smart-names`)
3. ✅ Plain-English HTML report (`--html`, `--serve`); print it to PDF from the browser
4. Web app (Next.js + Supabase + Vercel)
5. Multi-artist label dashboard + monthly re-scan alerts
