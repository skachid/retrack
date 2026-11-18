"""Payments for the fix pack: one Stripe Checkout payment per order.

The customer pays on Stripe's own page (hosted Checkout, guest checkout, no
accounts), so Retrack never sees or stores a card number. Retrack only:
  1. creates a Checkout Session for N songs at FIX_PRICE_USD each (one
     payment for the whole order, never one per song), and
  2. when the customer comes back, looks that session up with Stripe's API
     and unlocks the fixes only if Stripe says it's paid (web.py checks it
     also belongs to this visitor's Retrack session). The return URL alone
     proves nothing.

Three modes:
  demo - --mock, or no STRIPE_SECRET_KEY: a "Pay (demo)" button, no Stripe at all
  test - a test key (sk_test_... or rk_test_...): real Stripe, fake money
  live - a live key: refused unless RETRACK_PAYMENTS_LIVE=1 is also set

Keys come only from the environment (STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY)
and are never printed, logged or put in an error message.

Before Retrack runs on a public server, add a Stripe webhook handler
(checkout.session.completed and checkout.session.async_payment_succeeded):
customers don't always come back to the return page. Locally, web.py
re-checks the stored Checkout Session whenever the results or fixes page
loads, which covers a lost redirect.
"""

from dataclasses import dataclass

DEMO, TEST, LIVE = "demo", "test", "live"
TEST_BANNER = "Test payments only. Use card 4242 4242 4242 4242, any future date, any CVC."
DEMO_BANNER = "Demo payment: Pay (demo) skips Stripe, so nothing is charged."
# Tags these sessions in the Stripe Dashboard (Stripe asks for a label ending in 8 random letters).
INTEGRATION_ID = "retrack_fix_pack_kqvzhmwd"
PRODUCT_NAME = "Retrack fix (per song)"


class PaymentConfigError(Exception):
    """The Stripe settings can't be used. Shown at startup; Retrack doesn't start."""


class PaymentError(Exception):
    """Stripe didn't answer or refused a request. Shown to the user in plain words."""


@dataclass
class Checkout:
    id: str
    url: str
    status: str          # "open", "complete" or "expired"
    payment_status: str  # "paid", "unpaid" or "no_payment_required"
    client_reference_id: str
    amount_total: int = 0


def key_mode(key, kind="secret"):
    """'sk_test_...' -> TEST, 'sk_live_...' -> LIVE. Restricted keys (rk_) work too."""
    prefixes = ("sk_", "rk_") if kind == "secret" else ("pk_",)
    for prefix in prefixes:
        if key.startswith(prefix + "test_"):
            return TEST
        if key.startswith(prefix + "live_"):
            return LIVE
    raise PaymentConfigError(f"STRIPE_{kind.upper()}_KEY doesn't look like a Stripe {kind} key. "
                             "Copy it again from the Stripe Dashboard (Developers, API keys).")


def payments_from_env(env, mock=False, client=None):
    """Pick the payment mode from the environment. Raises PaymentConfigError if the keys can't be used."""
    secret = (env.get("STRIPE_SECRET_KEY") or "").strip()
    if mock or not secret:
        return DemoPayments()
    mode = key_mode(secret)
    publishable = (env.get("STRIPE_PUBLISHABLE_KEY") or "").strip()
    if publishable and key_mode(publishable, "publishable") != mode:
        raise PaymentConfigError("STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY are from different modes "
                                 "(one test, one live). Use the pair from the same page of the Dashboard.")
    if mode == LIVE and env.get("RETRACK_PAYMENTS_LIVE") != "1":
        raise PaymentConfigError("That's a live Stripe key, which takes real money. Retrack refuses to start "
                                 "with it unless RETRACK_PAYMENTS_LIVE=1 is also set. For testing, use your "
                                 "test key (sk_test_...).")
    return StripePayments(secret, mode, client)


class DemoPayments:
    """No Stripe: the order is marked paid straight away, and nothing is charged."""
    mode = DEMO


class StripePayments:
    def __init__(self, secret_key, mode, client=None):
        if client is None:
            import stripe  # imported here so demo mode works without the stripe package

            client = stripe.StripeClient(secret_key)
        self._client = client  # holds the key; this object never shows it
        self.mode = mode

    def __repr__(self):
        return f"StripePayments(mode={self.mode!r})"  # no key in logs or tracebacks

    def create_checkout(self, reference, quantity, unit_amount_cents, success_url, cancel_url, idempotency_key):
        """One Checkout Session for the whole order: one line, quantity N."""
        params = {
            "mode": "payment",
            "client_reference_id": reference,
            "line_items": [{
                "quantity": quantity,
                "price_data": {"currency": "usd", "unit_amount": unit_amount_cents,
                               "product_data": {"name": PRODUCT_NAME}},
            }],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"retrack_session": reference},
            "integration_identifier": INTEGRATION_ID,
        }
        try:
            # The idempotency key makes a retried request return the same session,
            # instead of creating (and charging for) a second one.
            session = self._client.v1.checkout.sessions.create(
                params=params, options={"idempotency_key": idempotency_key})
        except Exception as e:  # network, bad key, Stripe outage: never echo details that could hold a key
            raise PaymentError(f"Stripe didn’t accept the request ({type(e).__name__}).") from None
        return _checkout(session)

    def retrieve(self, checkout_id):
        try:
            return _checkout(self._client.v1.checkout.sessions.retrieve(checkout_id))
        except Exception as e:
            raise PaymentError(f"Stripe didn’t answer ({type(e).__name__}).") from None


    def expire(self, checkout_id):
        """Close an open Checkout Session so it can't be paid (used when an order is replaced)."""
        try:
            self._client.v1.checkout.sessions.expire(checkout_id)
        except Exception:
            pass  # already completed or expired: nothing to close


def _checkout(s):
    return Checkout(id=s.id, url=s.url or "", status=s.status, payment_status=s.payment_status,
                    client_reference_id=s.client_reference_id or "", amount_total=s.amount_total or 0)
