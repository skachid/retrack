"""Payments for the fix pack: one Stripe Checkout payment per order.

The customer pays on Stripe's own page (hosted Checkout, guest checkout, no
accounts), so Retrack never sees or stores a card number. Retrack only:
  1. creates a Checkout Session for the whole order (one payment, never one
     per song): one line for N songs at FIX_PRICE_USD each, plus one line per
     add-on at ADDON_PRICE_USD, and
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

Stripe also tells Retrack directly, with a signed webhook (POST
/stripe/webhook, checked with STRIPE_WEBHOOK_SECRET): customers don't always
come back to the return page. The return page and web.py's _sync_order()
stay as backups.
"""

import re
import sys
from dataclasses import dataclass

DEMO, TEST, LIVE = "demo", "test", "live"
TEST_BANNER = "Test payments only. Use card 4242 4242 4242 4242, any future date, any CVC."
DEMO_BANNER = "Demo payment: Pay (demo) skips Stripe, so nothing is charged."
# Tags these sessions in the Stripe Dashboard (Stripe asks for a label ending in 8 random letters).
INTEGRATION_ID = "retrack_fix_pack_kqvzhmwd"
PRODUCT_NAME = "Retrack fix (per song)"
ADDON_PRODUCT_NAMES = {"ai_search": "Retrack add-on: AI search (per order)",
                       "smart_names": "Retrack add-on: Smart names (per order)"}


class PaymentConfigError(Exception):
    """The Stripe settings can't be used. Shown at startup; Retrack doesn't start."""


class PaymentError(Exception):
    """Stripe didn't answer or refused a request. Shown to the user in plain words.

    retry=False means Stripe rejected the request itself (InvalidRequestError), so
    pressing the button again would only get the same "no".
    """

    def __init__(self, message, retry=True):
        super().__init__(message)
        self.retry = retry


# Anything shaped like a Stripe key or webhook secret. Stripe's error messages don't
# contain keys, but this makes sure a log line never can.
_KEY_PATTERN = re.compile(r"\b(sk|rk|pk|whsec)_[A-Za-z0-9_*]+")


def _redact(text):
    return _KEY_PATTERN.sub("[key removed]", str(text))


def _print_to_terminal(line):
    print(line, file=sys.stderr)


class WebhookError(Exception):
    """A webhook request we won't act on: unsigned, badly signed, or not set up."""


# The events that can mean "paid". async_payment_succeeded covers payment methods that
# settle later: their checkout.session.completed arrives while still unpaid.
WEBHOOK_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")


def verify_webhook(payload, signature, secret):
    """Check Stripe's signature on a webhook and return the event. Raises WebhookError.

    The signature (the Stripe-Signature header) is an HMAC of the raw body made with
    the webhook secret, plus a timestamp so an old request can't be replayed. Only
    Stripe and this computer know the secret, so a valid signature means Stripe sent it.
    """
    if not secret:
        raise WebhookError("STRIPE_WEBHOOK_SECRET isn't set, so no webhook can be trusted.")
    if not signature:
        raise WebhookError("Unsigned request.")
    import stripe
    try:
        return stripe.Webhook.construct_event(payload, signature, secret)
    except Exception:  # a bad signature, a stale timestamp or a broken body: all just "no"
        raise WebhookError("Bad signature.") from None


def checkout_from_event(event):
    """(event type, Checkout) for a Checkout Session event."""
    return event.type, _checkout(event.data.object)


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
    def __init__(self, secret_key, mode, client=None, log=None):
        if client is None:
            import stripe  # imported here so demo mode works without the stripe package

            client = stripe.StripeClient(secret_key)
        self._client = client  # holds the key; this object never shows it
        self.mode = mode
        self.log = log or _print_to_terminal  # injected so tests can read what was logged

    def _failed(self, action, e):
        """Turn a Stripe exception into a PaymentError, and log what Stripe said.

        Stripe's errors carry a code (e.g. "parameter_invalid"), the param it didn't
        like (e.g. "line_items[0][price_data][unit_amount]") and a message. Those
        describe our request, never the key or the catalog (we send Stripe no catalog
        data), so they're safe to log and are what you need to fix the request.
        """
        code = getattr(e, "code", None)
        param = getattr(e, "param", None)
        message = getattr(e, "user_message", None) or str(e)
        self.log(_redact(f"Stripe error while {action}: {type(e).__name__} "
                         f"code={code or '-'} param={param or '-'} message={message or '-'}"))
        if _is_invalid_request(e):
            return PaymentError("Stripe rejected the payment setup.", retry=False)
        return PaymentError(f"Stripe didn’t answer ({type(e).__name__}).")

    def __repr__(self):
        return f"StripePayments(mode={self.mode!r})"  # no key in logs or tracebacks

    def create_checkout(self, reference, quantity, unit_amount_cents, success_url, cancel_url, idempotency_key,
                        addons=()):
        """One Checkout Session for the whole order: the songs as one line (quantity N),
        plus one line (quantity 1) for each add-on, given as (name, cents) pairs. An order
        of add-ons only (bought later, on the fix page) has no songs line."""
        line_items = []
        if quantity:
            line_items.append({"quantity": quantity,
                               "price_data": {"currency": "usd", "unit_amount": unit_amount_cents,
                                              "product_data": {"name": PRODUCT_NAME}}})
        for name, cents in addons:
            line_items.append({"quantity": 1,
                               "price_data": {"currency": "usd", "unit_amount": cents,
                                              "product_data": {"name": ADDON_PRODUCT_NAMES[name]}}})
        params = {
            "mode": "payment",
            "client_reference_id": reference,
            "line_items": line_items,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"retrack_session": reference},
            # Managed Payments (Stripe as merchant of record, adding tax) is on by default on
            # new accounts and needs a product tax code. It's off here, so the customer pays
            # exactly the line items' total and the amount_total check in web.py stays exact.
            "managed_payments": {"enabled": False},
            "integration_identifier": INTEGRATION_ID,
        }
        try:
            # The idempotency key makes a retried request return the same session,
            # instead of creating (and charging for) a second one.
            session = self._client.v1.checkout.sessions.create(
                params=params, options={"idempotency_key": idempotency_key})
        except Exception as e:  # network, bad key, Stripe outage, a rejected request
            raise self._failed("creating a checkout", e) from None
        return _checkout(session)

    def retrieve(self, checkout_id):
        try:
            return _checkout(self._client.v1.checkout.sessions.retrieve(checkout_id))
        except Exception as e:
            raise self._failed("looking up a checkout", e) from None


    def expire(self, checkout_id):
        """Close an open Checkout Session so it can't be paid (used when an order is replaced)."""
        try:
            self._client.v1.checkout.sessions.expire(checkout_id)
        except Exception:
            pass  # already completed or expired: nothing to close


def _is_invalid_request(e):
    """True for stripe.InvalidRequestError (Stripe understood us and said no)."""
    try:
        import stripe
    except ImportError:
        return False
    return isinstance(e, stripe.InvalidRequestError)


def _checkout(s):
    return Checkout(id=s.id, url=s.url or "", status=s.status, payment_status=s.payment_status,
                    client_reference_id=s.client_reference_id or "", amount_total=s.amount_total or 0)
