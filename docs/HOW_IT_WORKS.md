# How it works

[← Back to the README](../README.md) · [Setup](SETUP.md) · [Command line](CLI.md)

## The web UI, step by step

Start it with `python -m retrack.web --mock` (see [Setup](SETUP.md)). You go through four steps:

1. **Upload.** Drop in your spreadsheet as it is: a `.csv` or `.xlsx` with any columns (a distributor export, or your own sheet).
2. **Columns.** Retrack shows which column it read as the title, artist, ISRC and writers (several writer columns are fine), how sure it is, and your first 5 rows. Fix anything it got wrong with the dropdowns, then press **Start the free check** and watch "Checking 37 of 120…", which updates by itself. The free check looks up each song at The MLC. No AI is used.
3. **Report.** The red/yellow/green report. **Summary** groups what Retrack found ("1 song is only partly claimed: Paper Crowns"); the other filters show one colour. There are also notes from reading your file. Download it as HTML or print it.
4. **Fix pack.** Tick the songs to fix, or use **Select all songs** at the top of the list (it ticks songs only, never an add-on, and shows as mixed when only some are ticked). Songs are a flat $5 each, whatever the verdict. Red songs show as "Needs fixing: we’ll work out exactly how after checkout." and cost the same as any other. Below the list, **Extra checks (optional)** offers two paid add-ons, **$2 each per order** (not per song), both **off by default**. Each says in one sentence what it sends to an AI service (Claude), and both run **only after checkout, and only on the songs you pay for**:
   - **AI search, +$2**: for paid red songs, tells whether each needs registering or is registered but not linked to your ISRC. Only offered when a ticked song is red; otherwise greyed out with "Only needed for red songs."
   - **Smart names, +$2**: asks whether two writer names are the same person, for paid songs with a writer missing at The MLC. Only offered when a ticked song has one; otherwise greyed out with a one-line reason.
   Below them, "What will leave this computer" follows the add-ons you tick, and the price line follows everything: "4 songs × $5.00 = $20.00 · AI search $2.00 · Smart names $2.00 · Total $24.00". On the next step, confirm that you reviewed the data and control the shares (without the confirmation, nothing is unlocked), then press **Pay and see your fixes**. You pay once for the whole order on Stripe's own checkout page (a line for the songs and one per add-on); Retrack never sees your card number. Back on Retrack, it first says "Working out how to fix song 1 of 3…" while it runs the add-ons you paid for, on the paid songs only. Then the **fix pack page** shows:
   - each paid song, grouped by Register, Link and Claim, with what Retrack filled in, every "You must fill this in" field highlighted, and step-by-step instructions
   - **Check by hand**, for any red song the add-ons couldn't place (AI search wasn't added, the AI didn't answer, or a spending limit was reached), with the steps to search The MLC yourself
   - **one main download**, `Retrack_Fixes_YYYYMMDD.xlsx`, with Summary, Register, Link, Claim and Confirmation tabs
   - **For submitting to The MLC**, when it applies: the bulk match CSV in The MLC's exact format, and an optional step to upload your own MLC Bulk Work template so Retrack fills a copy

   **Your fixes stay there for 60 minutes**, then your files are deleted for your privacy. The page shows how long is left. Print it (Cmd+P), download it, or screenshot it to keep it. Refreshing the page is safe; your browser also asks before you leave. If you didn't add AI search, the paid red songs are listed together under Check by hand, with a checkbox and **Add AI search for $2**: a small checkout for that add-on only, after which it works out just those songs. If you already paid for AI search, it never charges again.

   Without Stripe keys (or with `--mock`), the button is **Pay (demo)**, which skips Stripe and charges nothing, so everything runs locally.

   If you cancel on Stripe's page, you come back to your results with nothing lost. If the payment fails, nothing is charged. Paying for the same order twice is impossible: pressing the button again reuses the same checkout, changing the songs or add-ons replaces it, and songs and add-ons already paid for are never charged again. After 60 minutes your files are deleted: "Your files were deleted for your privacy. Please upload again."

Every error says what went wrong and what to do next: the wrong kind of file, no ISRC column, The MLC turning down the login or not answering, or Claude not being set up.

**Your files.** The upload goes in a private folder on this computer, and only for as long as you need it:
- **Delete my files now** on the results page removes it straight away.
- Stopping Retrack (**Ctrl+C**) deletes everything.
- If Retrack crashed last time, it clears the leftovers when it starts.

It only answers this computer, it never talks to any website other than The MLC's API (and Claude, if you turn a check on), and it checks up to 500 songs at a time. With `--mock`, every page says **Sample data, not real results**. Everything works with JavaScript turned off too.

## What the AI costs

Every AI call uses one small model, `claude-haiku-4-5` (`MODEL` in `retrack/ai.py`): these are short yes/no and "which column is which" questions. In the web UI the AI runs only for songs someone has paid for, with two hard limits: at most 5 AI calls per paid song (`MAX_SEARCHES`) for each order, and `RETRACK_AI_DAILY_LIMIT_USD` (default $5) of estimated spend per day for all orders together (counted in memory, so it starts again when Retrack restarts). When a limit is reached the AI is skipped, the song goes to "Check by hand", and nothing crashes. After each order the terminal prints the real token counts and the estimated cost, for example `AI usage for one order (3 songs): 7 calls, 4,210 input + 690 output tokens, about $0.0077. Today: about $0.0412 of $5.00.` It never prints a title, a name or an ISRC. The command line (`--smart-names`, `--ai-search`, `--smart-import`) works as before, with the same model.

## Architecture

```
catalog.csv ─► checker.py ─► mlc_client.py ─► The MLC API
                   │            1. POST /search/recordings  (ISRC → song code)
                   │            2. POST /works              (song code → writers, shares)
                   │            3. POST /search/songcode    (title + writers → songs; --ai-search only)
                   │       ▲
                   │  work_search.py  (variants + "same song?" judging; --ai-search only)
                   ▼
              report.py ─► terminal summary + report.csv (+ report.html)
```

| File | What it does |
|---|---|
| `retrack/mlc_client.py` | Logs in to The MLC and runs lookups. `MockClient` fakes it using `fixtures/mlc_mock.json`. |
| `retrack/checker.py` | The rules that decide red/yellow/green and the fix steps. |
| `retrack/name_matcher.py` | Second-pass name check: `ClaudeNameMatcher` (Claude API) and `MockNameMatcher` (offline rules). |
| `retrack/ai.py` | The one Claude model (`MODEL`), token prices, and the web UI's AI limits and cost log (`MeteredClient`). |
| `retrack/work_search.py` | `--ai-search`: searches by title/writers. `ClaudeWorkSearcher` (Claude API) and `MockWorkSearcher` (offline rules). Only suggests; `checker.py` decides. |
| `retrack/importer.py` | `import`: reads any .csv/.xlsx, maps the columns (offline rules, or Claude with `--smart-import`), cleans the data, writes `catalog.csv` and `import_problems.csv`. |
| `retrack/fixes.py` | The fix pack: plans which songs get which fix, checks the attestation, fills a copy of your MLC template, writes the other files. |
| `retrack/compliance.py` | The disclaimer, the "never" text and the attestation wording, in one place. |
| `retrack/report.py` | Prints the summary, writes the CSV and builds the HTML report. |
| `retrack/report.css` | The HTML report's styles. They're pasted into the HTML file, so it needs nothing else. |
| `retrack/web.py` | The web UI's routes: upload, columns, the check in the background, results, fix pack download, delete. |
| `retrack/web_pages.py` | The HTML for each step of the web UI. |
| `retrack/sessions.py` | Where uploads are kept while you work, and how they're deleted. |
| `retrack/payments.py` | Payments: one Stripe Checkout per order, the check that it's really paid, demo mode, and the live-key guard. |
| `retrack/env.py` | Reads `.env` when Retrack starts. |
| `retrack/privacy.py` | What leaves your computer for each setting. Every privacy sentence comes from here. |
| `retrack/home.css`, `retrack/home.js` | The web UI's styles and its small script (drag and drop, live progress, the running total). Every page still works with JavaScript off. |
| `retrack/preview.py` | `--serve`: a tiny local web server (Python's built-in `http.server`) for the HTML report. |
| `tests/test_checker.py` | One test per verdict, plus name and ISRC cleanup. |
| `tests/test_payments.py` | Payments with Stripe faked: unpaid sessions see nothing, tampered return links, one charge per order, live keys refused, demo mode offline, the fix page and its downloads, `.env` loading. |
| `tests/test_web.py` | The web UI end to end: upload → columns → check → fix pack download, attestation refusal, every error, file deletion, and privacy text that matches what's actually sent. |
| `tests/test_ai.py` | The one shared model, the per-order and daily AI limits, the cost log, and AI only after checkout for paid songs. |
| `tests/test_work_search.py` | `--ai-search`: the song-code search, the search limits, both searchers, and the two red cases. |
| `tests/test_importer.py` | `import`: reading messy files, both mappers, the confirmation step, every cleanup rule, and a test proving only the header + 5 rows reach Claude. |
| `tests/test_fixes.py` | The fix pack and the compliance rules: no files without attestation, ownership never guessed, one price for every verdict. |
| `tests/test_html_report.py` | The HTML report (order, counts, escaping, offline, print) and the preview server. |

## Known limits (v1)

- **Payments are confirmed three ways:** the signed webhook (`/stripe/webhook`), the return page after checkout, and a re-check whenever you open the results or fix page. Locally the webhook needs `stripe listen` (see [Setup](SETUP.md#test-the-stripe-webhook-on-your-computer)); a hosted version needs its own webhook endpoint and secret.
- The fix pack page lives for 60 minutes. Nothing is kept on a server.
- Only checks The MLC. SoundExchange and ASCAP/BMI Songview have no public API, so the report links to them for a manual check.
- Name matching is basic by default. "J. Okafor" won't match "Jide Okafor" unless you add `--smart-names`, which asks the Claude API about names that don't match exactly. Only the two writer names are sent. It needs `ANTHROPIC_API_KEY` and `pip install anthropic`. `--mock` uses offline rules instead, so it needs no key.
- If one ISRC links to several works, only the first is checked.
- How The MLC's `/search/songcode` matches titles and writers (exact or fuzzy) isn't documented. The mock assumes exact titles, which is why trying variants helps.

## Roadmap

1. ✅ CLI script + mock mode
2. ✅ Smarter writer-name matching with the Claude API (`--smart-names`)
3. ✅ Plain-English HTML report (`--html`, `--serve`); print it to PDF from the browser
4. ✅ Find registered-but-unlinked songs by title and writers (`--ai-search`)
5. ✅ Local web UI, end to end: upload → columns → check → report → fix pack
6. ✅ Stripe test payments for the fix pack (one checkout per order)
7. Hosted web app (Next.js + Supabase + Vercel), with a Stripe webhook
8. Multi-artist label dashboard + monthly re-scan alerts
