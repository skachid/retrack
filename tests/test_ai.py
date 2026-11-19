"""Tests for the AI rules: one cheap model, AI only after checkout and only for paid
songs, the per-order and daily limits, and the cost log.

Claude is always faked here: no test calls the real API.
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from retrack import ai, web
from retrack.ai import (AIConfigError, AILimitReached, DailySpend, MeteredClient, OrderBudget, cost_usd,
                        daily_limit_from_env)
from retrack.checker import RED, YELLOW, Result
from retrack.fixes import ALL_SET, UNRESOLVED, plan_fixes, sold_kind
from retrack.importer import ClaudeColumnMapper
from retrack.name_matcher import ClaudeNameMatcher
from retrack.work_search import ClaudeWorkSearcher

from test_web import call, fixable_ids, form, open_fixes, pay_all, scanned, start, text


class FakeClaude:
    """Answers 'no' to everything, says how many tokens it used, and remembers each request."""

    def __init__(self, tokens_in=1000, tokens_out=100):
        self.requests = []
        self.tokens = (tokens_in, tokens_out)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        system = kw["system"]
        if "same person" in system:
            answer = {"same_person": False, "confidence": 0.9, "reason": "different"}
        elif "Suggest other searches" in system:
            answer = {"searches": []}
        elif "same song" in system.lower() or "MLC record" in kw["messages"][0]["content"]:
            answer = {"same_song": False, "confidence": 0.9, "reason": "different"}
        else:
            answer = {}
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=self.tokens[0],
                                                                              output_tokens=self.tokens[1]),
                               content=[SimpleNamespace(type="text", text=json.dumps(answer))])

    def sent_text(self):
        return " ".join(r["messages"][0]["content"] for r in self.requests)


# ---- One shared, cheap model ----------------------------------------------------------------

def test_every_claude_feature_uses_the_one_shared_model():
    assert ai.MODEL == "claude-haiku-4-5"
    assert ClaudeNameMatcher(client=object()).model == ai.MODEL
    assert ClaudeWorkSearcher(client=object()).model == ai.MODEL
    assert ClaudeColumnMapper(client=object()).model == ai.MODEL
    assert ai.MODEL in ai.PRICES_PER_MTOK  # so its cost is estimated at its real price


def test_requests_use_only_what_the_model_accepts():
    claude = FakeClaude()
    ClaudeNameMatcher(client=claude).same_person("Jide Okafor", "J. Okafor")
    ClaudeWorkSearcher(client=claude).suggest_queries("Blue Hour", ["Jide Okafor"])
    ClaudeColumnMapper(client=claude).map_columns(["Title", "ISRC"], [["Blue Hour", "QZABC2400005"]])
    assert len(claude.requests) == 3
    for kw in claude.requests:
        assert kw["model"] == "claude-haiku-4-5"
        # Haiku 4.5 rejects an effort level and has no server-side fallback.
        assert "effort" not in kw.get("output_config", {}) and "fallbacks" not in kw and "betas" not in kw
        assert kw["output_config"]["format"]["type"] == "json_schema"  # answers always parse


# ---- Cost and limits --------------------------------------------------------------------------

def test_cost_comes_from_the_token_counts():
    assert cost_usd("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.00)
    assert cost_usd("claude-haiku-4-5", 0, 1_000_000) == pytest.approx(5.00)
    assert cost_usd("some-new-model", 1_000_000, 0) == pytest.approx(ai.FALLBACK_PRICES[0])  # never undercounted


def test_metered_client_counts_tokens_and_dollars():
    budget, daily = OrderBudget(), DailySpend(5.0)
    budget.add_songs(1, 5)
    client = MeteredClient(FakeClaude(tokens_in=2000, tokens_out=300), budget, daily)
    client.messages.create(model=ai.MODEL, system="same person", messages=[{"role": "user", "content": "x"}])
    u = budget.usage
    assert (u.calls, u.input_tokens, u.output_tokens) == (1, 2000, 300)
    assert u.cost_usd == pytest.approx(cost_usd(ai.MODEL, 2000, 300))
    assert daily.spent_usd == pytest.approx(u.cost_usd)
    assert budget.calls_left == 4


def test_the_order_limit_refuses_calls_once_used_up():
    budget, daily = OrderBudget(), DailySpend(5.0)
    budget.add_songs(1, 2)  # two calls allowed
    claude = FakeClaude()
    client = MeteredClient(claude, budget, daily)
    for _ in range(2):
        client.messages.create(model=ai.MODEL, system="same person", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(AILimitReached):
        client.messages.create(model=ai.MODEL, system="same person", messages=[{"role": "user", "content": "x"}])
    assert len(claude.requests) == 2  # the third never reached the API
    assert budget.usage.skipped == 1 and budget.limit_hit


def test_the_daily_limit_refuses_calls_and_starts_again_the_next_day():
    day = [datetime.date(2026, 11, 18)]
    daily = DailySpend(0.001, today=lambda: day[0])
    budget = OrderBudget()
    budget.add_songs(10, 5)
    client = MeteredClient(FakeClaude(tokens_in=1000, tokens_out=0), budget, daily)  # $0.001 a call
    client.messages.create(model=ai.MODEL, system="same person", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(AILimitReached):
        client.messages.create(model=ai.MODEL, system="same person", messages=[{"role": "user", "content": "x"}])
    day[0] = datetime.date(2026, 11, 19)
    client.messages.create(model=ai.MODEL, system="same person", messages=[{"role": "user", "content": "x"}])


def test_a_limit_hit_is_handled_like_no_answer():
    budget = OrderBudget()  # no calls allowed
    client = MeteredClient(FakeClaude(), budget, DailySpend(5.0))
    v = ClaudeNameMatcher(client=client).same_person("Jide Okafor", "J. Okafor")
    assert v.same_person is False and "unavailable" in v.reason  # the writer is still flagged
    assert ClaudeWorkSearcher(client=client).suggest_queries("Blue Hour", []) is None


@pytest.mark.parametrize("raw,expected", [("", 5.0), ("2.50", 2.5), ("0", 0.0), (" 10 ", 10.0)])
def test_daily_limit_from_the_environment(raw, expected):
    assert daily_limit_from_env({"RETRACK_AI_DAILY_LIMIT_USD": raw}) == expected


@pytest.mark.parametrize("raw", ["five", "-1", "$5"])
def test_a_bad_daily_limit_stops_retrack_with_a_clear_message(raw):
    with pytest.raises(AIConfigError):
        daily_limit_from_env({"RETRACK_AI_DAILY_LIMIT_USD": raw})


# ---- What gets sold, and the command line ----------------------------------------------------

def _result(status, red_case="", missing=()):
    row = SimpleNamespace(title="T", artist="A", isrc="QZABC2400001", writers=["W"])
    r = Result(row, status, red_case=red_case)
    r.missing_writers = list(missing)
    return r


def test_an_unplaced_red_song_is_sold_and_worked_out_later():
    red = _result(RED)
    assert sold_kind(red) == UNRESOLVED
    plan = plan_fixes([red], sell_red=True)
    assert plan.unresolved == [red] and plan.songs == [red]


def test_the_command_line_still_skips_unsearched_red_songs():
    plan = plan_fixes([_result(RED)])  # the CLI: sell_red is False
    assert plan.songs == [] and len(plan.skipped) == 1 and not plan.unresolved


def test_a_paid_song_found_fine_is_shown_as_all_set():
    green = _result("GREEN")
    plan = plan_fixes([green], sell_red=True)
    assert plan.all_set == [green]
    from retrack.fixes import price_line
    assert price_line(plan.songs).startswith("1 song ")  # still counted as paid


# ---- The web flow: no AI before checkout, only paid songs after ------------------------------

@pytest.fixture
def real(tmp_path):
    """Real mode (no sample data) with a fake Claude and a log we can read."""
    claude, lines = FakeClaude(), []
    srv = start(tmp_path, mock=False, make_claude=lambda: claude, log=lines.append,
                daily=DailySpend(5.0))
    srv.claude, srv.lines = claude, lines
    yield srv
    srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_the_free_check_never_calls_the_ai(real):
    sid = scanned(real, smart_names=True, ai_search=True)
    assert real.claude.requests == []
    _, page = text(real, f"/s/{sid}/results")
    assert "Needs fixing: we’ll work out exactly how after checkout." in page


def test_after_checkout_the_ai_runs_only_on_the_paid_songs(real):
    sid = scanned(real, ai_search=True)  # Blue Hour is red: AI search is the add-on it can use
    results = real.sessions.get(sid).results
    blue = str(next(i for i, r in enumerate(results) if r.row.title == "Blue Hour"))
    pay_all(real, sid, [blue])
    status, headers, _ = call(real, f"/s/{sid}/fixes")
    assert status == 303 and headers["Location"].endswith("/progress")  # "Working out…" first
    status, page = open_fixes(real, sid)
    assert status == 200
    sent = real.claude.sent_text()
    assert "Blue Hour" in sent
    for unpaid in ("Ghost Signal", "Paper Crowns", "Concrete Garden", "Nova Reyes", "Marcus Bell", "Sam Lee"):
        assert unpaid not in sent, unpaid  # never a song that wasn't paid for
    # the song price doesn't change; the add-on is a flat $2 for the order
    paid = page.split('class="results-meta paid-price"')[1].split("</p>")[0]
    assert '1 song × $5.00 = $5.00</span><span class="price-part">AI search $2.00</span>' in paid
    assert "Total $7.00" in paid


def test_the_progress_page_says_it_is_working_out_the_fixes():
    from retrack import web_pages as pages
    s = SimpleNamespace(total=3, done=1, phase="finish")
    assert pages.progress_text(s) == "Working out how to fix song 2 of 3…"
    s.phase = "scan"
    assert pages.progress_text(s) == "Checking 2 of 3…"


def test_the_order_limit_is_max_searches_per_paid_song(real):
    sid = scanned(real, ai_search=True)
    pay_all(real, sid)
    open_fixes(real, sid)
    paid = len(real.sessions.get(sid).paid_songs)
    assert 0 < len(real.claude.requests) <= web.MAX_SEARCHES * paid


def test_when_a_limit_is_hit_the_song_is_checked_by_hand_and_nothing_crashes(tmp_path):
    claude, lines = FakeClaude(), []
    srv = start(tmp_path, mock=False, make_claude=lambda: claude, log=lines.append,
                daily=DailySpend(0.0))  # the day's limit is already used up
    try:
        sid = scanned(srv, smart_names=True, ai_search=True)
        pay_all(srv, sid)
        status, page = open_fixes(srv, sid)
        assert status == 200 and claude.requests == []
        group = page.split('data-status="unresolved"')[1].split("</section>")[0]
        assert "Couldn’t finish the automatic check; here’s how to check by hand." in group
        assert "Ghost Signal" in group and "Blue Hour" in group
        assert any("skipped" in line and "limit" in line for line in lines)
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_the_cost_of_each_order_is_logged_without_catalog_data(real):
    sid = scanned(real, smart_names=True, ai_search=True)
    pay_all(real, sid)
    open_fixes(real, sid)
    usage = [line for line in real.lines if line.startswith("AI usage for one order")]
    assert len(usage) == 1
    line = usage[0]
    n = len(real.claude.requests)
    assert f"{n} calls" in line and f"{n * 1000:,} input" in line and f"{n * 100:,} output" in line
    assert "$" in line and "Today:" in line
    session = real.sessions.get(sid)
    for r in session.results:  # no titles, artists, writers or ISRCs
        for value in (r.row.title, r.row.artist, r.row.isrc, *r.row.writers):
            assert value not in line


def test_smart_names_cant_be_bought_for_songs_that_dont_need_it(real):
    sid = scanned(real)
    results = real.sessions.get(sid).results
    ghost = str(next(i for i, r in enumerate(results) if r.row.title == "Ghost Signal"))
    # A red song has no writers to compare: Smart names is greyed out, and sending it anyway is refused.
    status, _, body = call(real, f"/s/{sid}/pay", *form([("song", ghost), ("smart_names", "on"), ("attest", "yes")]))
    assert status == 400 and "Smart names isn’t needed for these songs" in body.decode()
    assert real.sessions.get(sid).paid_songs == [] and real.claude.requests == []  # nothing charged, no AI
