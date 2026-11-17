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
- `retrack/importer.py`: `python -m retrack import file.{csv,xlsx} --out catalog.csv [--smart-import] [--mock] [--yes]`. Reads any spreadsheet (busiest sheet, merged cells filled, header row found in the first 30 rows), maps columns, shows the mapping for the user to confirm or edit, then converts with plain Python. Offline `HeaderRuleMapper` is the default (also the fallback when the AI fails); `ClaudeColumnMapper` runs only with `--smart-import` (and not with `--mock`). Both have `map_columns(headers, samples)`. When the rule mapper has a missing or low-confidence field (`unclear_fields`), the output suggests `--smart-import` (`smart_import_hint`). Cleanup rules (`clean_isrc`, `split_writers`, duplicates, missing ISRC/writers) and their problems go to `import_problems.csv` with spreadsheet row numbers.
- `retrack/fixes.py`: the fix pack (`--fix` / `--fix-songs`, `--template`, `--i-confirm`), written to `fix_pack/`. `plan_fixes()` → `build_fix_pack()`. REGISTER (Not found anywhere) → a filled copy of the user's MLC template (or `registration_worksheet.csv`), LINK (not linked) → `match_suggestions.csv` (for review) + `BulkMatchSuggestions_YYYYMMDD.csv` (The MLC's exact format), CLAIM (yellow) → `claim_checklist.md`, plus `ATTESTATION.txt` and `README.md`. Template columns are found by header name (`classify_header`), never by hard-coded letters.
- `retrack/compliance.py`: `COMPLIANCE_TEXT`, `NEVER_TEXT`, `ATTESTATION_TEXT`. Import them; never reword them elsewhere.
- `retrack/name_matcher.py`: second-pass writer-name check. `ClaudeNameMatcher` (Claude API, opt-in via `--smart-names`) + `MockNameMatcher` (offline, used with `--mock`). Both have `same_person(a, b)`.
- `retrack/report.py`: output (terminal, CSV, `render_html`). HTML is one self-contained file: CSS from `retrack/report.css` inlined, no JS, no web fonts/CDNs. Escape all catalog text with `html.escape`.
- `retrack/report.css`: report styles (Hallmark, theme Specimen). Every colour/font is a token in `:root`.
- `retrack/web.py`: the homepage (`python -m retrack.web [--mock]`). Upload → `parse_catalog` → `check_catalog` → results on the same page. Shares `summary_html`/`groups_html` with the report. Binds 127.0.0.1, checks the Origin header on POST, caps 2 MB / 500 songs.
- `retrack/home.css` + `home.js`: homepage design (Hallmark, Specimen, extends report.css). The form must keep working with JS off.
- `retrack/preview.py`: `--serve`. Binds 127.0.0.1:8000 only and serves the report only (never the folder: `.env` lives there).
- `fixtures/import/`: made-up files shaped like a DistroKid export (CSV), a TuneCore export and a messy label sheet (.xlsx). Rebuild the .xlsx files with `python fixtures/import/build_import_fixtures.py`.
- `fixtures/mlc_mock.json`: fake API responses shaped like the real spec. `songcode_search` holds what `/search/songcode` can find: "THE BLUE HOUR" (BH3456, unlinked match for Blue Hour), a "BLUE HOUR" look-alike by another writer (must not match), and CG9013 (second registration of Concrete Garden).

## MLC API facts (spec v1.2.0, public-api.themlc.com/api/doc)
- Auth: `POST /oauth/token` with username/password → `accessToken` (Bearer).
- The only ISRC lookup is `POST /search/recordings` → gives `mlcsongCode`.
- `POST /works` takes `[{"mlcsongCode": ...}]` (lowercase s) → writers, publishers, `collectionShare`.
- `POST /search/songcode` takes `{"title", "writers": [{"writerFirstName", "writerLastName", "writerIPI"}]}` (no field required) → array of `{"mlcSongCode"` (capital S), `"workTitle", "iswc", "writers": [{writerId, writerFirstName, writerLastName, writerIPI, writerRoleCode}]}`. How it matches (exact/fuzzy) isn't documented.
- There is no ISRC→work or ISWC lookup.

## MLC process facts (from The MLC's help pages, checked Nov 2026)
- Bulk Work Registration: Portal → Works Registration → Bulk Registration Tool. `.xlsx`, columns must not move, max 300 rows, a work title appears once per document, each extra writer on its own row below with the work columns blank. Publishing info only for shares you control. Collection share can't be 0.
- Bulk Match Submission: CSV UTF-8, header exactly `ISRC,MLC Song Code`, no blank rows, no duplicate ISRCs, named `MemberName_BulkMatchSuggestions_YYYYMMDD.csv`, emailed to bulk.matching@themlc.com, 30–90 days.

## Compliance rules
These keep Retrack honest and safe to sell. Every change must keep them, and `tests/test_fixes.py` checks them.
- **No files without attestation.** Before a fix pack is written, the user confirms `ATTESTATION_TEXT` (they reviewed the data and control the shares), by typing yes at the prompt or with `--i-confirm`. `build_fix_pack()` refuses without an `Attestation` and writes nothing. The confirmation, how it was given and a UTC timestamp go in `ATTESTATION.txt`. Without a terminal and without `--i-confirm`, there's no confirmation.
- **Never guess ownership.** Publisher shares, IPIs, role codes and publisher names are never filled in, inferred or suggested, not even by the AI. Only facts from the catalog and The MLC are pre-filled; ownership cells stay blank, highlighted and marked `YOU MUST FILL THIS IN`.
- **Never recreate The MLC's template.** The user downloads it from their own MLC Portal and passes `--template`. Fill a copy (openpyxl); never change the original. Without a template, write the CSV worksheet that is clearly labelled as not the template, and say how to get the template. (Tests may build a fake template; product code never does.)
- **No duplicates.** Include the MLC song code whenever a work already exists. If the template has no song code column, such songs go in the claim checklist, not the template. Red songs without `--ai-search` are skipped, not registered.
- **One flat price.** `FIX_PRICE_USD` per fixed song, the same for every verdict. No per-verdict pricing. The summary shows "N songs × $X". No payment code yet.
- **Compliance text.** `COMPLIANCE_TEXT` on the HTML report, the homepage, and the fix pack's `README.md` and `ATTESTATION.txt` (and Retrack's own worksheet/checklist/review files). It never goes inside files whose format The MLC dictates: the template copy and the bulk match CSV.
- **MLC-format files stay exactly in The MLC's format.** The template copy is untouched apart from the filled rows (cell values, plus yellow highlights and "you must fill this in" comments on those rows): no extra tabs, no notes, no changed properties, columns never moved. The bulk match CSV is exactly `ISRC,MLC Song Code`, UTF-8, no blank rows, no duplicate ISRCs.
- **No MLC branding.** No MLC logos or images anywhere. Say "The MLC" in plain text only, and never imply endorsement.
- **Hands off the Portal.** Retrack never logs into a customer's MLC Portal, never asks for a customer's Portal login or password, and never uploads or submits anything on the user's behalf. Fix pack code makes no network calls. Retrack's own Public Search API credentials in `.env` are fine (that's our API access, not a customer's Portal login); they're never prompted for and never written to any output.
- **AI only suggests.** Matches are labelled "please confirm"; verdicts come from `checker.py`.
- **Every AI feature is opt-in** (`--smart-names`, `--ai-search`, `--smart-import`). The default path never calls Claude, and `--mock` always stays offline.
- **Import: the AI (only with `--smart-import`) only sees the header row and up to `SAMPLE_ROWS` (5) sample rows**, and only answers which column is which. Every row is converted by plain Python; the AI never rewrites data. The user confirms the mapping first (or `--yes`); no terminal and no `--yes` means nothing is converted. Import never drops a row silently: every row that isn't imported is in `import_problems.csv` with its row number. When a writer split is unsure ("Smith, John"), leave it as it is and flag it.

## Rules
- Never commit `.env` or credentials. `fix_pack/` is git-ignored (it holds catalog data).
- Don't scrape SoundExchange, BMI or ASCAP sites. Link to them for manual checks instead.
