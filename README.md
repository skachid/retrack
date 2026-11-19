# Retrack

Retrack checks a music catalog against **The MLC** (the US body that pays streaming mechanical royalties) and finds the songs that aren't properly registered, so the money stops sitting unclaimed. It tells you what's wrong with each song and gives you the files to fix it yourself.

## Who it's for

Small indie labels, artist managers, producers and publishers who have a catalog spreadsheet and no time to check every song in The MLC Portal by hand.

## How it works

1. **Upload** your spreadsheet as it is: a `.csv` or `.xlsx` from DistroKid, TuneCore or your own sheet. Retrack shows which column it read as the title, artist, ISRC and writers, and you correct it if needed.
2. **Free check.** Every song is looked up at The MLC by its ISRC (no AI is used) and comes back as:

   | | Meaning |
   |---|---|
   | 🔴 Red | The MLC has no record of the recording, or it isn't linked to a song. Money is probably stuck. |
   | 🟡 Yellow | The song is there, but shares don't add up to 100%, nobody's collecting, or writers are missing. |
   | 🟢 Green | Registered, fully claimed, writers match. |

3. **Pay for fixes.** Tick the songs you want fixed. It's one flat price per song, whatever the colour. You can turn on two optional extra checks that run after checkout, on the paid songs only. You confirm that you reviewed the data and control the shares, then pay once on Stripe's checkout page.
4. **Fix page.** Each paid song is grouped by what to do (Register, Link or Claim), with what Retrack filled in, the fields you must fill in yourself, short steps, and a spreadsheet to download. Where it applies, you also get the file in The MLC's own format. Retrack never submits anything for you.

## Privacy and your data

- **Your file stays on the computer running Retrack.** It's deleted after 60 minutes, when you press *Delete my files now*, or when Retrack stops.
- **Only what's needed goes to The MLC:** the ISRCs and MLC song codes, never the whole file.
- **The AI is opt-in and runs only after checkout.** The free check never uses it. If you turn on Smart names or AI search, only the titles and writer names of the songs you pay for are sent. ISRCs, audio and money figures never are.
- **Retrack never sees your card.** Stripe's own page takes it.
- **No audio, no training.** Retrack never touches your recordings and never learns from your catalog.
- Retrack never logs into your MLC Portal and never asks for your Portal password.

## Not affiliated with The MLC

Retrack is not affiliated with or endorsed by The MLC. Data comes from The MLC’s Public Search API and may not reflect actual copyright ownership. You are responsible for reviewing all data before uploading.

## Status

**In testing.** Retrack runs locally, payments use Stripe test mode, and the price is a placeholder. It isn't a hosted service yet.

## For developers

- [docs/SETUP.md](docs/SETUP.md): install, `.env`, Stripe test keys, webhooks, MLC API access, hosting notes
- [docs/CLI.md](docs/CLI.md): the command line: `import`, `--ai-search`, `--fix`, the HTML report, the CSV format
- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md): the web app step by step, architecture, AI costs, known limits, roadmap

Quick start with sample data (nothing leaves your computer):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m retrack.web --mock
```

Run the tests with `pytest`. They never read your `.env` and never call Stripe or the AI service.
