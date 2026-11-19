# Retrack

Finds songs in a music catalog that aren't properly registered with **The MLC** (the US body that pays streaming mechanical royalties), so the money stops sitting unclaimed.

Give it a CSV of songs, and every song comes back as:

| | Meaning |
|---|---|
| 🔴 RED | The MLC has no record of the recording, or it isn't linked to a song. Money is probably stuck. |
| 🟡 YELLOW | The song is there, but shares don't add to 100%, nobody's collecting, or writers are missing. |
| 🟢 GREEN | Registered, fully claimed, writers match. |

With `--ai-search`, a red song is split into one of two cases:

| Red case | Meaning | Fix |
|---|---|---|
| Registered but not linked to your recording | The song is at The MLC (found by title and writers, maybe spelled a bit differently) but not linked to your ISRC. | Confirm it's yours, claim it, attach the ISRC. Don't register it again. |
| Not found anywhere | No recording for the ISRC, and no song under the title or writer names either. | Register it from scratch. |

## Bring your own spreadsheet (`import`)

You don't need to reformat your catalog. Retrack can read the spreadsheet you already have (a DistroKid or TuneCore export, or your own sheet) and turn it into its four-column CSV:

```bash
python -m retrack import my_catalog.xlsx --out catalog.csv                  # offline rules, no API key
python -m retrack import my_catalog.xlsx --out catalog.csv --smart-import   # let Claude read the columns
python -m retrack catalog.csv --mock                                        # then check it as usual
```

It takes `.csv` and `.xlsx` with any columns, title rows above the headers, blank rows, merged cells, and semicolon-separated "European" CSVs.

**Step 1: which column is which.** By default Retrack works this out offline, from the header names (and from the values, for an unlabelled ISRC column). Nothing leaves your computer. You see the result and confirm it, edit it, or cancel:

```
Song Name (B) → title (90%)
Performer (C) → artist (80%)
ISRC code (D) → isrc (95%)
Composer(s) (E) → writers (90%)
Use this? [y]es / [e]dit / [n]o:
```

Pass `--yes` in scripts to skip the question.

If the offline rules can't find a column, or aren't sure about one, Retrack says so and suggests `--smart-import`. Like `--smart-names` and `--ai-search`, it's opt-in: Claude is sent **only the header row and the first 5 rows** and replies with which column is which and how sure it is. It needs `ANTHROPIC_API_KEY`, and you still confirm the result. If Claude doesn't answer, Retrack falls back to the offline rules. `--smart-import --mock` stays offline.

**Step 2: converting.** Plain Python reads every row using that mapping. The AI never sees or changes the rest of your data. While converting, it:

- **Cleans ISRCs**: removes dashes, spaces and dots and upper-cases them. A code that isn't 12 characters (country, registrant, year, number) is flagged, and that row isn't imported.
- **Splits writers** on `;`, `/`, `&`, `and` and line breaks, and on commas only when each piece is a full name ("John Smith, Ana Lopez"). `Smith, John` could be one person or two, so it's left as it is and flagged.
- **Drops duplicates**: rows that are exactly the same after mapping, like an earnings export listing one song per store.
- **Flags, never silently drops**, rows with no ISRC (not imported) or no writers (imported, so you can check them).

At the end you get a summary (rows in, rows out, each problem with its spreadsheet row number), and the problems are saved to `import_problems.csv` next to your catalog.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# With fake data (no API access needed):
python -m retrack sample_catalog.csv --mock

# With the real MLC API (after you're approved):
cp .env.example .env   # then fill in your MLC login
python -m retrack your_catalog.csv --out report.csv
```

Retrack reads `.env` by itself when it starts, so there's nothing to `export`. A variable you set in the terminal wins over `.env`. `.env` is git-ignored; never put real values in `.env.example`, which is committed.

Run the tests: `pytest`. They never read your `.env` and never call Stripe.

## Set up `.env`

`.env.example` lists every setting, all blank. Copy it to `.env` and fill in only what you use:

| Variable | What it's for | Needed? |
|---|---|---|
| `MLC_USERNAME`, `MLC_PASSWORD` | Your Public Search API login from The MLC | For real checks |
| `ANTHROPIC_API_KEY` | The AI features (Smart names, AI search, `--smart-import`) | Optional |
| `RETRACK_AI_DAILY_LIMIT_USD` | The most the web UI's AI checks may cost per day, in dollars (estimated from token counts) | Optional: blank means 5; 0 turns the AI off |
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | Taking payment for the fix pack in the web UI | Optional: without them, "Pay (demo)" |
| `STRIPE_WEBHOOK_SECRET` | Lets Stripe confirm payments directly (`whsec_...`) | Optional, recommended |
| `RETRACK_PAYMENTS_LIVE` | Set to `1` only to allow a live Stripe key (real money) | Leave blank |

### Getting Stripe test keys

1. Make a free account at [stripe.com](https://stripe.com), or a sandbox with the Stripe CLI (`stripe sandbox create`), which needs no sign-up.
2. In the Dashboard, make sure you're in **test mode** or a **sandbox** (not live), then go to **Developers → API keys**.
3. Copy the **Publishable key** (`pk_test_...`) into `STRIPE_PUBLISHABLE_KEY`.
4. For the secret key, use a **restricted key** (recommended). It only lets Retrack do what it needs, so a leaked key can do little harm. In **Developers → API keys**, choose **Create restricted key**, name it "Retrack", set **Checkout Sessions** to **Write**, and leave everything else at **None**. Write covers the three calls Retrack makes: create a checkout, look one up, and close an old one. Copy the key (`rk_test_...`) into `STRIPE_SECRET_KEY`. A full secret key (`sk_test_...`) works too, but can do far more than Retrack needs.
5. Start Retrack. It prints "Payments: Stripe test mode". On the pay step, use card **4242 4242 4242 4242**, any future date and any CVC.

Retrack refuses to start with a live key (`sk_live_...` or `rk_live_...`) unless `RETRACK_PAYMENTS_LIVE=1` is also set, and it never prints your keys.

**Try payments before you have MLC access:** `python -m retrack.web --mock-mlc` uses sample MLC data and offline AI (like `--mock`, with the "Sample data, not real results" banner on every page), but a real Stripe test checkout when your test keys are set. `--mock` stays fully offline with Pay (demo).

### Test the Stripe webhook on your computer

Besides sending customers back to Retrack, Stripe tells Retrack directly when a payment succeeds, with a signed message called a *webhook*. That way a payment still unlocks the fixes even if the customer closes the tab on Stripe's page. Retrack only trusts webhooks signed with your `STRIPE_WEBHOOK_SECRET`.

Stripe can't reach `localhost` by itself, so locally the Stripe CLI forwards webhooks to you:

1. Install the Stripe CLI: on a Mac, `brew install stripe/stripe-cli/stripe`; otherwise see [docs.stripe.com/stripe-cli](https://docs.stripe.com/stripe-cli).
2. Log in: `stripe login` (it opens your browser once).
3. In a second terminal window, start forwarding (use Retrack's port, 8000 unless you changed it):

   ```bash
   stripe listen --forward-to localhost:8000/stripe/webhook
   ```

4. It prints `Your webhook signing secret is whsec_...`. Copy that into `.env` as `STRIPE_WEBHOOK_SECRET=whsec_...`, then restart Retrack.
5. Pay with the 4242 card. In the `stripe listen` window you'll see `checkout.session.completed` and `[200]`.

Keep `stripe listen` running while you test. Its secret stays the same between runs.

**A hosted version** gets its own webhook secret: in the Stripe Dashboard go to **Developers → Webhooks → Add endpoint**, enter `https://<your domain>/stripe/webhook`, choose the events `checkout.session.completed` and `checkout.session.async_payment_succeeded`, and put that endpoint's `whsec_...` signing secret in the server's environment. Don't reuse the CLI's secret.

## Find songs the ISRC doesn't lead to (`--ai-search`)

Sometimes a song *is* registered at The MLC, just under "The Blue Hour" instead of "Blue Hour", or with "Jyde" instead of "Jide", and nobody linked your recording's ISRC to it. The ISRC lookup can't see that, so the song shows as red and you might register it a second time.

```bash
python -m retrack sample_catalog.csv --mock --ai-search   # offline version, no key needed
python -m retrack your_catalog.csv --ai-search            # needs ANTHROPIC_API_KEY + pip install anthropic
```

For each red song, and each song with writers missing from its MLC record, Retrack:

1. Searches The MLC (`POST /search/songcode`) by the title and writers exactly as in your CSV.
2. Asks the Claude API for a few likely variants (a leading "The", initials, last name only…) and searches those. **At most 5 searches per song.**
3. Asks Claude to judge each song that came back: same song or not, and how sure. At most 5 are judged per song.

A suggestion only counts if Claude says "same song" and is at least 85% sure, and a matching title by different writers doesn't count. The verdict itself is decided by Retrack's own rules (`checker.py`), not by the AI. Every suggestion is shown as **"please confirm"**: it's a lead for you to check in The MLC Portal, not a fact. If Claude or The MLC doesn't answer, Retrack says the search couldn't finish rather than claiming "not found".

For a song with missing writers, a match is shown as a possible *second registration* of the same song (which The MLC can merge).

Only song titles and writer names are sent to Claude: no audio, no ISRCs, no artist names, no money figures. `--mock --ai-search` uses simple offline rules instead and sends nothing anywhere.

## Fix pack (`--fix`)

The scan is free. The fix pack is the paid part: a flat **$5.00 per fixed song** (a placeholder price, `FIX_PRICE_USD` in `retrack/fixes.py`), the same price whatever the verdict. The web UI takes payment through Stripe (below). The command line takes no payment; it's for you, the owner.

```bash
# Every song that can be fixed:
python -m retrack sample_catalog.csv --mock --ai-search --fix

# Only some songs, filling your own MLC template:
python -m retrack your_catalog.csv --ai-search --fix-songs "Blue Hour,Ghost Signal" --template mlc_template.xlsx
```

Retrack shows which songs go in the pack and the price ("4 songs × $5.00 = $20.00"). Then it asks you to confirm that **you reviewed the data and control the shares being registered**. Type `yes`, or pass `--i-confirm` in scripts. No confirmation, no files. Your confirmation and the time are saved in the pack.

The pack goes in `fix_pack/`, with a README explaining each file and how to upload it:

| Song | File | What you do with it |
|---|---|---|
| Not found anywhere | `bulk_work_registration.xlsx` (a filled copy of **your** template), or `registration_worksheet.csv` without `--template` | Fill the yellow cells, upload in your MLC Portal: Works Registration → Bulk Registration Tool. |
| Registered but not linked | `match_suggestions.csv` (to review) and `BulkMatchSuggestions_YYYYMMDD.csv` (The MLC's exact two-column format) | Confirm each match, then use the Portal's Matching Tool, or email the bulk file (unconfirmed rows deleted) for The MLC's Bulk Match Submission. |
| Partial shares or missing writers | `claim_checklist.md` | Tick through the steps in your MLC Portal. |

`ATTESTATION.txt` holds your confirmation and the time. The disclaimer is in the pack's README and ATTESTATION. Files whose format The MLC dictates get nothing extra: your template copy only gains the filled rows (no extra tabs), and the bulk match file is exactly `ISRC,MLC Song Code`.

**Get the template** from your own MLC Portal (Works Registration → Bulk Registration Tool → download). Retrack never makes its own copy of The MLC's template: it fills in a copy of yours and leaves your original alone.

**Ownership data is never guessed.** Publisher shares, IPIs, role codes and publisher names are left blank and marked "YOU MUST FILL THIS IN". Retrack only pre-fills what it knows from your catalog and The MLC: titles, writer names, artist, ISRC, and the MLC song code when a work already exists (so no duplicate gets created).

Red songs need `--ai-search` to be in the pack. Without it, Retrack can't tell "not registered" from "registered under another title", and registering the second kind again would create a duplicate.

Retrack never logs into The MLC Portal, never asks for your Portal password, and never uploads anything for you.

## The web UI (the easiest way in)

```bash
python -m retrack.web --mock       # try it with sample data: nothing leaves your computer
python -m retrack.web --mock-mlc   # sample data, but real Stripe test checkout (needs test keys)
python -m retrack.web              # the real thing (needs MLC_USERNAME / MLC_PASSWORD)
```

Your browser opens **http://localhost:8000**, and you go through five steps:

1. **Upload.** Drop in your spreadsheet as it is: a `.csv` or `.xlsx` with any columns (a distributor export, or your own sheet).
2. **Columns.** Retrack shows which column it read as the title, artist, ISRC and writers (several writer columns are fine), how sure it is, and your first 5 rows. Fix anything it got wrong with the dropdowns.
3. **Check.** Choose the two optional extra checks. Both are **off by default**, and each says in one sentence what it sends to an AI service (Claude). They run **only after checkout, and only on the songs you pay for**: the free check never uses the AI.
   - **Smart names**: asks whether two writer names are the same person (paid songs with a missing writer).
   - **AI search**: for paid red songs, tells whether each needs registering or is registered but not linked to your ISRC.
   Below them, "What will leave this computer" updates as you switch them on and off. Then press **Start the check** and watch "Checking 37 of 120…", which updates by itself.
4. **Report.** The red/yellow/green report. **Summary** groups what Retrack found ("1 song is only partly claimed: Paper Crowns"); the other filters show one colour. There are also notes from reading your file. Download it as HTML or print it.
5. **Fix pack.** Tick the songs to fix and see "N songs × $X = $Total". Red songs show as "Needs fixing: we’ll work out exactly how after checkout." and cost the same as any other. Confirm that you reviewed the data and control the shares (without the confirmation, nothing is unlocked), then press **Pay and see your fixes**. You pay once for the whole order on Stripe's own checkout page; Retrack never sees your card number. Back on Retrack, it first says "Working out how to fix song 1 of 3…" while it runs the extra checks you turned on, on the paid songs only. Then the **fix pack page** shows:
   - each paid song, grouped by Register, Link and Claim, with what Retrack filled in, every "You must fill this in" field highlighted, and step-by-step instructions
   - **Check by hand**, for any red song the extra checks couldn't place (AI search was off, the AI didn't answer, or a spending limit was reached), with the steps to search The MLC yourself
   - **one main download**, `Retrack_Fixes_YYYYMMDD.xlsx`, with Summary, Register, Link, Claim and Confirmation tabs
   - **For submitting to The MLC**, when it applies: the bulk match CSV in The MLC's exact format, and an optional step to upload your own MLC Bulk Work template so Retrack fills a copy

   **Your fixes stay there for 60 minutes**, then your files are deleted for your privacy. The page shows how long is left. Print it (Cmd+P), download it, or screenshot it to keep it. Refreshing the page is safe; your browser also asks before you leave. If AI search was off, the paid red songs are listed together under Check by hand, with a checkbox and a button that works out just those songs (no extra charge).

   Without Stripe keys (or with `--mock`), the button is **Pay (demo)**, which skips Stripe and charges nothing, so everything runs locally.

   If you cancel on Stripe's page, you come back to your results with nothing lost. If the payment fails, nothing is charged. Paying for the same order twice is impossible: pressing the button again reuses the same checkout, and songs already paid for are never charged again. After 60 minutes your files are deleted: "Your files were deleted for your privacy. Please upload again."

Every error says what went wrong and what to do next: the wrong kind of file, no ISRC column, The MLC turning down the login or not answering, or Claude not being set up.

**Your files.** The upload goes in a private folder on this computer, and only for as long as you need it:
- **Delete my files now** on the results page removes it straight away.
- Stopping Retrack (**Ctrl+C**) deletes everything.
- If Retrack crashed last time, it clears the leftovers when it starts.

It only answers this computer, it never talks to any website other than The MLC's API (and Claude, if you turn a check on), and it checks up to 500 songs at a time.

**What the AI costs.** Every AI call uses one small model, `claude-haiku-4-5` (`MODEL` in `retrack/ai.py`): these are short yes/no and "which column is which" questions. In the web UI the AI runs only for songs someone has paid for, with two hard limits: at most 5 AI calls per paid song (`MAX_SEARCHES`) for each order, and `RETRACK_AI_DAILY_LIMIT_USD` (default $5) of estimated spend per day for all orders together (counted in memory, so it starts again when Retrack restarts). When a limit is reached the AI is skipped, the song goes to "Check by hand", and nothing crashes. After each order the terminal prints the real token counts and the estimated cost, for example `AI usage for one order (3 songs): 7 calls, 4,210 input + 690 output tokens, about $0.0077. Today: about $0.0412 of $5.00.` It never prints a title, a name or an ISRC. The command line (`--smart-names`, `--ai-search`, `--smart-import`) works as before, with the same model. With `--mock`, every page says **Sample data, not real results**. Use `--port 8001` if 8000 is taken. Everything works with JavaScript turned off too.

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

## Not affiliated with The MLC

Retrack is not affiliated with or endorsed by The MLC. Data comes from The MLC’s Public Search API and may not reflect actual copyright ownership. You are responsible for reviewing all data before uploading.

## Getting real API access

Apply for The MLC's **Public Search API** on their Data Programs page: https://www.themlc.com/dqi

## Known limits (v1)

- **Payments are confirmed three ways:** the signed webhook (`/stripe/webhook`), the return page after checkout, and a re-check whenever you open the results or fix page. Locally the webhook needs `stripe listen` (above); a hosted version needs its own webhook endpoint and secret.
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
