# The command line

[← Back to the README](../README.md) · [Setup](SETUP.md) · [How it works](HOW_IT_WORKS.md)

The command line does everything the web UI does, for you, the owner. It takes no payment.

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

## Find songs the ISRC doesn't lead to (`--ai-search`)

Sometimes a song *is* registered at The MLC, just under "The Blue Hour" instead of "Blue Hour", or with "Jyde" instead of "Jide", and nobody linked your recording's ISRC to it. The ISRC lookup can't see that, so the song shows as red and you might register it a second time.

```bash
python -m retrack sample_catalog.csv --mock --ai-search   # offline version, no key needed
python -m retrack your_catalog.csv --ai-search            # needs ANTHROPIC_API_KEY + pip install anthropic
```

With `--ai-search`, a red song is split into one of two cases:

| Red case | Meaning | Fix |
|---|---|---|
| Registered but not linked to your recording | The song is at The MLC (found by title and writers, maybe spelled a bit differently) but not linked to your ISRC. | Confirm it's yours, claim it, attach the ISRC. Don't register it again. |
| Not found anywhere | No recording for the ISRC, and no song under the title or writer names either. | Register it from scratch. |

For each red song, and each song with writers missing from its MLC record, Retrack:

1. Searches The MLC (`POST /search/songcode`) by the title and writers exactly as in your CSV.
2. Asks the Claude API for a few likely variants (a leading "The", initials, last name only…) and searches those. **At most 5 searches per song.**
3. Asks Claude to judge each song that came back: same song or not, and how sure. At most 5 are judged per song.

A suggestion only counts if Claude says "same song" and is at least 85% sure, and a matching title by different writers doesn't count. The verdict itself is decided by Retrack's own rules (`checker.py`), not by the AI. Every suggestion is shown as **"please confirm"**: it's a lead for you to check in The MLC Portal, not a fact. If Claude or The MLC doesn't answer, Retrack says the search couldn't finish rather than claiming "not found".

For a song with missing writers, a match is shown as a possible *second registration* of the same song (which The MLC can merge).

Only song titles and writer names are sent to Claude: no audio, no ISRCs, no artist names, no money figures. `--mock --ai-search` uses simple offline rules instead and sends nothing anywhere.

**Smarter name matching (`--smart-names`).** By default "J. Okafor" won't match "Jide Okafor". `--smart-names` asks the Claude API about names that don't match exactly. Only the two writer names are sent. It needs `ANTHROPIC_API_KEY` and `pip install anthropic`; `--mock` uses offline rules instead.

## Fix pack (`--fix`)

The scan is free. The fix pack is the paid part: a flat **$5.00 per fixed song** (a placeholder price, `FIX_PRICE_USD` in `retrack/fixes.py`), the same price whatever the verdict. The web UI takes payment through Stripe. The command line takes no payment; it's for you, the owner.

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
