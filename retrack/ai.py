"""The AI service: which model, what it costs, and the limits on spending.

Every Claude call in Retrack (name matching, work search, column mapping) uses
MODEL below, so changing model is a one-line change. These are short yes/no and
"which column is which" questions, so a small, cheap model is enough.

In the web UI, every call goes through a MeteredClient, which:
  1. checks two limits *before* the call, and refuses it (AILimitReached) if
     either is used up:
       - the order's limit: MAX_SEARCHES AI calls per paid song, and
       - the daily limit: RETRACK_AI_DAILY_LIMIT_USD of estimated spend per
         day (UTC), for all orders together;
  2. after the call, reads the token counts Claude returns and adds them up,
     so the terminal can show what an order really cost.

A refused call is handled like a call that failed: the matchers already treat
"no answer" as "no match", so nothing crashes and the song is flagged for a
person to check instead. The logs carry counts and dollars only: never a title,
a writer name or any other catalog data.

The command line (python -m retrack) doesn't use any of this: it calls Claude
directly, with the same MODEL.
"""

import datetime
import threading
from dataclasses import dataclass

MODEL = "claude-haiku-4-5"

# Dollars per million tokens: (input, output). From Anthropic's price list; if
# MODEL changes, add its prices here or costs are estimated at the fallback.
PRICES_PER_MTOK = {"claude-haiku-4-5": (1.00, 5.00)}
FALLBACK_PRICES = (5.00, 25.00)  # deliberately high: an unknown model is never under-counted

DEFAULT_DAILY_LIMIT_USD = 5.00
DAILY_LIMIT_ENV = "RETRACK_AI_DAILY_LIMIT_USD"


class AILimitReached(Exception):
    """An AI call was skipped because a limit is used up. Handled like a failed call."""


class AIConfigError(Exception):
    """RETRACK_AI_DAILY_LIMIT_USD can't be read. Shown at startup."""


def cost_usd(model, input_tokens, output_tokens):
    price_in, price_out = PRICES_PER_MTOK.get(model, FALLBACK_PRICES)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def daily_limit_from_env(env):
    raw = (env.get(DAILY_LIMIT_ENV) or "").strip()
    if not raw:
        return DEFAULT_DAILY_LIMIT_USD
    try:
        limit = float(raw)
    except ValueError:
        raise AIConfigError(f"{DAILY_LIMIT_ENV} must be a number of dollars, like 5 or 2.50.") from None
    if limit < 0:
        raise AIConfigError(f"{DAILY_LIMIT_ENV} can't be negative. Use 0 to turn the AI checks off.")
    return limit


@dataclass
class Usage:
    """What a batch of AI calls used. Numbers only, never catalog data."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped: int = 0  # calls refused because a limit was used up

    def summary(self):
        text = (f"{self.calls} {'call' if self.calls == 1 else 'calls'}, "
                f"{self.input_tokens:,} input + {self.output_tokens:,} output tokens, "
                f"about ${self.cost_usd:.4f}")
        if self.skipped:
            text += f"; {self.skipped} skipped (limit reached)"
        return text


class DailySpend:
    """Estimated AI spend today (UTC), for every order this Retrack has run.

    It lives in memory, so it starts again at $0 when Retrack restarts.
    The clock is passed in (dependency injection) so tests can change the day.
    """

    def __init__(self, limit_usd, today=None):
        self.limit_usd = limit_usd
        self._today = today or (lambda: datetime.datetime.now(datetime.timezone.utc).date())
        self._day = self._today()
        self._spent = 0.0
        self._lock = threading.Lock()

    def _roll(self):
        day = self._today()
        if day != self._day:  # a new day: start counting again
            self._day, self._spent = day, 0.0

    @property
    def spent_usd(self):
        with self._lock:
            self._roll()
            return self._spent

    def allows_another_call(self):
        with self._lock:
            self._roll()
            return self._spent < self.limit_usd

    def add(self, dollars):
        with self._lock:
            self._roll()
            self._spent += dollars


class OrderBudget:
    """The AI calls one session may still make: MAX_SEARCHES for each song paid for."""

    def __init__(self):
        self.calls_left = 0
        self.usage = Usage()  # this run's usage; reset by start_run()
        self.limit_hit = False
        self._lock = threading.Lock()

    def add_songs(self, n, calls_per_song):
        with self._lock:
            self.calls_left += n * calls_per_song

    def start_run(self):
        self.usage, self.limit_hit = Usage(), False

    def take_call(self):
        with self._lock:
            if self.calls_left <= 0:
                return False
            self.calls_left -= 1
            return True


class MeteredClient:
    """Looks like anthropic.Anthropic for the one call Retrack makes
    (client.messages.create), but checks the limits first and counts tokens after.

    This is the "wrapper" (or decorator) pattern: same method, extra checks around it.
    """

    def __init__(self, inner, budget, daily):
        self._inner, self._budget, self._daily = inner, budget, daily
        self.messages = self  # so callers can write client.messages.create(...)

    def create(self, **kwargs):
        if not self._daily.allows_another_call() or not self._budget.take_call():
            self._budget.usage.skipped += 1
            self._budget.limit_hit = True
            raise AILimitReached("AI limit reached")
        response = self._inner.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        dollars = cost_usd(kwargs.get("model", MODEL), tokens_in, tokens_out)
        u = self._budget.usage
        u.calls += 1
        u.input_tokens += tokens_in
        u.output_tokens += tokens_out
        u.cost_usd += dollars
        self._daily.add(dollars)
        return response
