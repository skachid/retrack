# Notes for Claude Code

## What this is
A tool that checks indie music catalogs against The MLC's Public Search API and flags royalty gaps (red/yellow/green). The customers are small indie labels, managers and producers. They distrust generative AI, so the product never touches audio, never trains on their data, and doesn't market itself as "AI".

## The owner
A junior developer learning as he goes. When you make changes:
- Explain *why* in plain words, briefly, and name the concept (e.g. "this is dependency injection").
- Prefer simple, readable code over clever code.
- Keep the tests passing (`pytest`) and add a test for every new rule.

## Code map
- `retrack/mlc_client.py`: API client (`MLCClient`) + `MockClient`. Both have `search_recordings(isrc)`, `get_works(song_codes)` and `search_songcode(title, writers)` (writers = plain names; `search_writers()` turns them into SearchWriter objects).
- `retrack/checker.py`: verdict rules. All red/yellow/green logic lives here, including the RED split (`red_case`: `NOT_LINKED` / `NOT_FOUND`) and the `WORK_MATCH_MIN_CONFIDENCE` bar.
- `retrack/work_search.py`: `--ai-search`. `search_for_work()` runs the exact search + variants (max `MAX_SEARCHES`=5 per song, max `MAX_CANDIDATES`=5 judged). `ClaudeWorkSearcher` + `MockWorkSearcher` (with `--mock`), both with `suggest_queries()` and `judge()`. It only suggests; never decide a verdict there. Sends only titles + writer names. Suggestions are always shown as "please confirm".
- `retrack/name_matcher.py`: second-pass writer-name check. `ClaudeNameMatcher` (Claude API, opt-in via `--smart-names`) + `MockNameMatcher` (offline, used with `--mock`). Both have `same_person(a, b)`.
- `retrack/report.py`: output (terminal, CSV, `render_html`). HTML is one self-contained file: CSS from `retrack/report.css` inlined, no JS, no web fonts/CDNs. Escape all catalog text with `html.escape`.
- `retrack/report.css`: report styles (Hallmark, theme Specimen). Every colour/font is a token in `:root`.
- `retrack/web.py`: the homepage (`python -m retrack.web [--mock]`). Upload → `parse_catalog` → `check_catalog` → results on the same page. Shares `summary_html`/`groups_html` with the report. Binds 127.0.0.1, checks the Origin header on POST, caps 2 MB / 500 songs.
- `retrack/home.css` + `home.js`: homepage design (Hallmark, Specimen, extends report.css). The form must keep working with JS off.
- `retrack/preview.py`: `--serve`. Binds 127.0.0.1:8000 only and serves the report only (never the folder: `.env` lives there).
- `fixtures/mlc_mock.json`: fake API responses shaped like the real spec. `songcode_search` holds what `/search/songcode` can find: "THE BLUE HOUR" (BH3456, unlinked match for Blue Hour), a "BLUE HOUR" look-alike by another writer (must not match), and CG9013 (second registration of Concrete Garden).

## MLC API facts (spec v1.2.0, public-api.themlc.com/api/doc)
- Auth: `POST /oauth/token` with username/password → `accessToken` (Bearer).
- The only ISRC lookup is `POST /search/recordings` → gives `mlcsongCode`.
- `POST /works` takes `[{"mlcsongCode": ...}]` (lowercase s) → writers, publishers, `collectionShare`.
- `POST /search/songcode` takes `{"title", "writers": [{"writerFirstName", "writerLastName", "writerIPI"}]}` (no field required) → array of `{"mlcSongCode"` (capital S), `"workTitle", "iswc", "writers": [{writerId, writerFirstName, writerLastName, writerIPI, writerRoleCode}]}`. How it matches (exact/fuzzy) isn't documented.
- There is no ISRC→work or ISWC lookup.

## Rules
- Never commit `.env` or credentials.
- Don't scrape SoundExchange, BMI or ASCAP sites. Link to them for manual checks instead.
