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
              report.py ─► terminal summary + report.csv
```

| File | What it does |
|---|---|
| `retrack/mlc_client.py` | Logs in to The MLC and runs lookups. `MockClient` fakes it using `fixtures/mlc_mock.json`. |
| `retrack/checker.py` | The rules that decide red/yellow/green and the fix steps. |
| `retrack/name_matcher.py` | Second-pass name check: `ClaudeNameMatcher` (Claude API) and `MockNameMatcher` (offline rules). |
| `retrack/report.py` | Prints the summary and writes the CSV. |
| `tests/test_checker.py` | One test per verdict, plus name and ISRC cleanup. |

## Getting real API access

Apply for The MLC's **Public Search API** on their Data Programs page: https://www.themlc.com/dqi

## Known limits (v1)

- Only checks The MLC. SoundExchange and ASCAP/BMI Songview have no public API, so the report links to them for a manual check.
- Name matching is basic by default. "J. Okafor" won't match "Jide Okafor" unless you add `--smart-names`, which asks the Claude API about names that don't match exactly. Only the two writer names are sent. It needs `ANTHROPIC_API_KEY` and `pip install anthropic`. `--mock` uses offline rules instead, so it needs no key.
- If one ISRC links to several works, only the first is checked.

## Roadmap

1. ✅ CLI script + mock mode
2. ✅ Smarter writer-name matching with the Claude API (`--smart-names`)
3. Plain-English HTML/PDF report
4. Web app (Next.js + Supabase + Vercel)
5. Multi-artist label dashboard + monthly re-scan alerts
