# Setup

[← Back to the README](../README.md) · [Command line](CLI.md) · [How it works](HOW_IT_WORKS.md)

## Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# With fake data (no API access needed):
python -m retrack sample_catalog.csv --mock

# With the real MLC API (after you're approved):
cp .env.example .env   # then fill in your MLC login
python -m retrack your_catalog.csv --out report.csv
```

The web UI:

```bash
python -m retrack.web --mock       # try it with sample data: nothing leaves your computer
python -m retrack.web --mock-mlc   # sample data, but real Stripe test checkout (needs test keys)
python -m retrack.web              # the real thing (needs MLC_USERNAME / MLC_PASSWORD)
```

Your browser opens **http://localhost:8000**. Use `--port 8001` if 8000 is taken.

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

## Getting Stripe test keys

1. Make a free account at [stripe.com](https://stripe.com), or a sandbox with the Stripe CLI (`stripe sandbox create`), which needs no sign-up.
2. In the Dashboard, make sure you're in **test mode** or a **sandbox** (not live), then go to **Developers → API keys**.
3. Copy the **Publishable key** (`pk_test_...`) into `STRIPE_PUBLISHABLE_KEY`.
4. For the secret key, use a **restricted key** (see below). Copy it (`rk_test_...`) into `STRIPE_SECRET_KEY`. A full secret key (`sk_test_...`) works too, but can do far more than Retrack needs.
5. Start Retrack. It prints "Payments: Stripe test mode". On the pay step, use card **4242 4242 4242 4242**, any future date and any CVC.

Retrack refuses to start with a live key (`sk_live_...` or `rk_live_...`) unless `RETRACK_PAYMENTS_LIVE=1` is also set, and it never prints your keys.

**Try payments before you have MLC access:** `python -m retrack.web --mock-mlc` uses sample MLC data and offline AI (like `--mock`, with the "Sample data, not real results" banner on every page), but a real Stripe test checkout when your test keys are set. `--mock` stays fully offline with Pay (demo).

### The restricted key

A restricted key only lets Retrack do what it needs, so a leaked key can do little harm. In **Developers → API keys**, choose **Create restricted key**, name it "Retrack", set **Checkout Sessions** to **Write**, and leave everything else at **None**. Write covers the three calls Retrack makes: create a checkout, look one up, and close an old one.

## Test the Stripe webhook on your computer

Besides sending customers back to Retrack, Stripe tells Retrack directly when a payment succeeds, with a signed message called a *webhook*. That way a payment still unlocks the fixes even if the customer closes the tab on Stripe's page. Retrack only trusts webhooks signed with your `STRIPE_WEBHOOK_SECRET`.

Stripe can't reach `localhost` by itself, so locally the Stripe CLI forwards webhooks to you:

1. Install the Stripe CLI: on a Mac, `brew install stripe/stripe-cli/stripe`; otherwise see [docs.stripe.com/stripe-cli](https://docs.stripe.com/stripe-cli).
2. Log in: `stripe login` (it opens your browser once).
3. In a second terminal window, start forwarding the two events Retrack handles (use Retrack's port, 8000 unless you changed it):

   ```bash
   stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded --forward-to localhost:8000/stripe/webhook
   ```

4. It prints `Your webhook signing secret is whsec_...`. Copy that into `.env` as `STRIPE_WEBHOOK_SECRET=whsec_...`, then restart Retrack.
5. Pay with the 4242 card. In the `stripe listen` window you'll see `checkout.session.completed` and `[200]`.

Keep `stripe listen` running while you test. Its secret stays the same between runs.

## Getting MLC API access

Apply for The MLC's **Public Search API** on their Data Programs page: https://www.themlc.com/dqi. Once approved, put the login in `.env` as `MLC_USERNAME` and `MLC_PASSWORD`.

## Hosting notes

Retrack is built to run locally for now: it only answers this computer (127.0.0.1) and keeps uploads in a private temporary folder.

- **A hosted version gets its own webhook secret.** In the Stripe Dashboard go to **Developers → Webhooks → Add endpoint**, enter `https://<your domain>/stripe/webhook`, choose the events `checkout.session.completed` and `checkout.session.async_payment_succeeded`, and put that endpoint's `whsec_...` signing secret in the server's environment. Don't reuse the CLI's secret.
