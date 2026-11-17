# Notes for Claude Code

## What this is
A tool that checks indie music catalogs against The MLC's Public Search API and flags royalty gaps (red/yellow/green). The customers are small indie labels, managers and producers. They distrust generative AI, so the product never touches audio, never trains on their data, and doesn't market itself as "AI".

## The owner
A junior developer learning as he goes. When you make changes:
- Explain *why* in plain words, briefly, and name the concept (e.g. "this is dependency injection").
- Prefer simple, readable code over clever code.
- Keep the tests passing (`pytest`) and add a test for every new rule.

## Code map
- `retrack/mlc_client.py`: API client (`MLCClient`) + `MockClient`. Both have `search_recordings(isrc)` and `get_works(song_codes)`.
- `retrack/checker.py`: verdict rules. All red/yellow/green logic lives here.
- `retrack/name_matcher.py`: second-pass writer-name check. `ClaudeNameMatcher` (Claude API, opt-in via `--smart-names`) + `MockNameMatcher` (offline, used with `--mock`). Both have `same_person(a, b)`.
- `retrack/report.py`: output (terminal, CSV, `render_html`). HTML is one self-contained file: CSS from `retrack/report.css` inlined, no JS, no web fonts/CDNs. Escape all catalog text with `html.escape`.
- `retrack/report.css`: report styles (Hallmark, theme Specimen). Every colour/font is a token in `:root`.
- `retrack/web.py`: the homepage (`python -m retrack.web [--mock]`). Upload → `parse_catalog` → `check_catalog` → results on the same page. Shares `summary_html`/`groups_html` with the report. Binds 127.0.0.1, checks the Origin header on POST, caps 2 MB / 500 songs.
- `retrack/home.css` + `home.js`: homepage design (Hallmark, Specimen, extends report.css). The form must keep working with JS off.
- `retrack/preview.py`: `--serve`. Binds 127.0.0.1:8000 only and serves the report only (never the folder: `.env` lives there).
- `fixtures/mlc_mock.json`: fake API responses shaped like the real spec.

## MLC API facts (spec v1.2.0, public-api.themlc.com/api/doc)
- Auth: `POST /oauth/token` with username/password → `accessToken` (Bearer).
- The only ISRC lookup is `POST /search/recordings` → gives `mlcsongCode`.
- `POST /works` takes `[{"mlcsongCode": ...}]` (lowercase s) → writers, publishers, `collectionShare`.
- There is no ISRC→work or ISWC lookup.

## Rules
- Never commit `.env` or credentials.
- Don't scrape SoundExchange, BMI or ASCAP sites. Link to them for manual checks instead.
