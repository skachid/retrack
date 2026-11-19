"""Tests for payments (payments.py and the pay steps in web.py) and .env loading (env.py).

Stripe is always faked here: no test calls the real Stripe API.
"""

import io
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from retrack import payments as payments_mod
from retrack import web
from retrack.env import load_env, parse_env
from retrack.fixes import FIX_PRICE_USD
from retrack.mlc_client import MockClient
from retrack.payments import (DEMO, LIVE, TEST, TEST_BANNER, DemoPayments, PaymentConfigError, StripePayments,
                              payments_from_env)
from retrack.sessions import SESSION_SECONDS, SessionStore
from retrack.web_pages import keep_text, time_left_text

from test_web import call, fixable_ids, form, open_fixes, scan, text, upload_ok, wait_done

ROOT = Path(__file__).parent.parent


# ---- A fake Stripe: the same calls StripeClient.v1.checkout.sessions has --------------------

class FakeSessions:
    def __init__(self):
        self.created, self.expired, self.store, self.keys = [], [], {}, {}
        self.fail = False
        self.reject = False  # Stripe understood the request and said no

    def create(self, params=None, options=None):
        if self.fail:
            raise ConnectionError("network down, key sk_test_SECRET")
        if self.reject:
            import stripe
            raise stripe.InvalidRequestError("Invalid integer: abc (key sk_test_SECRET)",
                                             "line_items[0][price_data][unit_amount]", code="parameter_invalid")
        key = (options or {}).get("idempotency_key")
        if key in self.keys:  # like Stripe: the same idempotency key returns the same session
            return self.store[self.keys[key]]
        n = len(self.created) + 1
        total = sum(li["quantity"] * li["price_data"]["unit_amount"] for li in params["line_items"])  # like Stripe
        s = SimpleNamespace(id=f"cs_test_{n}", url=f"https://checkout.stripe.test/pay/cs_test_{n}", status="open",
                            payment_status="unpaid", client_reference_id=params["client_reference_id"],
                            amount_total=total)
        self.created.append((params, options))
        self.store[s.id], self.keys[key] = s, s.id
        return s

    def retrieve(self, checkout_id, params=None, options=None):
        if checkout_id not in self.store:
            raise LookupError("No such checkout.session")
        return self.store[checkout_id]

    def expire(self, checkout_id, params=None, options=None):
        self.expired.append(checkout_id)
        self.store[checkout_id].status = "expired"

    # what the customer does on Stripe's page
    def customer_pays(self, checkout_id):
        s = self.store[checkout_id]
        s.status, s.payment_status = "complete", "paid"


def fake_stripe(log=None):
    sessions = FakeSessions()
    client = SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=sessions)))
    return StripePayments("sk_test_SECRET", TEST, client=client, log=log), sessions


# ---- Choosing the mode from the environment -------------------------------------------------

def test_no_key_or_mock_means_demo():
    assert payments_from_env({}).mode == DEMO
    assert payments_from_env({"STRIPE_SECRET_KEY": "sk_test_abc"}, mock=True).mode == DEMO


@pytest.mark.parametrize("key", ["sk_test_abc", "rk_test_abc"])
def test_test_keys_mean_test_mode(key):
    assert payments_from_env({"STRIPE_SECRET_KEY": key}, client=object()).mode == TEST


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc"])
def test_a_live_key_is_refused_without_the_flag(key):
    with pytest.raises(PaymentConfigError, match="RETRACK_PAYMENTS_LIVE=1"):
        payments_from_env({"STRIPE_SECRET_KEY": key}, client=object())
    assert payments_from_env({"STRIPE_SECRET_KEY": key, "RETRACK_PAYMENTS_LIVE": "1"}, client=object()).mode == LIVE


def test_mixed_or_wrong_keys_are_refused():
    with pytest.raises(PaymentConfigError, match="different modes"):
        payments_from_env({"STRIPE_SECRET_KEY": "sk_test_a", "STRIPE_PUBLISHABLE_KEY": "pk_live_b"}, client=object())
    with pytest.raises(PaymentConfigError, match="doesn’t look like|doesn't look like"):
        payments_from_env({"STRIPE_SECRET_KEY": "pk_test_oops"}, client=object())


def test_web_refuses_to_start_with_a_live_key(monkeypatch, capsys):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    monkeypatch.setenv("MLC_USERNAME", "u")  # so the MLC login check passes and the key check is reached
    monkeypatch.setenv("MLC_PASSWORD", "p")
    assert web.main(["--port", "0"], open_browser=None) == 1
    err = capsys.readouterr().err
    assert "live Stripe key" in err and "sk_live_abc" not in err


# ---- Talking to Stripe ----------------------------------------------------------------------

def test_one_checkout_session_per_order_with_quantity_n():
    payments, sessions = fake_stripe()
    payments.create_checkout(reference="R", quantity=4, unit_amount_cents=500, success_url="s", cancel_url="c",
                             idempotency_key="k1")
    assert len(sessions.created) == 1
    params, options = sessions.created[0]
    assert params["line_items"] == [{"quantity": 4, "price_data": {
        "currency": "usd", "unit_amount": 500, "product_data": {"name": payments_mod.PRODUCT_NAME}}}]
    assert params["mode"] == "payment" and params["client_reference_id"] == "R"
    assert "payment_method_types" not in params  # Stripe shows the methods enabled in the Dashboard
    assert "customer" not in params  # guest checkout, no accounts
    # Managed Payments off: Stripe would otherwise demand a tax code and could add tax on top
    assert params["managed_payments"] == {"enabled": False}
    assert options == {"idempotency_key": "k1"}


def test_keys_never_show_in_errors_or_logs():
    payments, sessions = fake_stripe()
    assert "SECRET" not in repr(payments)
    sessions.fail = True
    with pytest.raises(payments_mod.PaymentError) as err:
        payments.create_checkout(reference="R", quantity=1, unit_amount_cents=500, success_url="s",
                                 cancel_url="c", idempotency_key="k")
    assert "SECRET" not in str(err.value) and "sk_test" not in str(err.value)


def test_rejected_request_logs_stripes_details_but_no_key():
    lines = []
    payments, sessions = fake_stripe(log=lines.append)
    sessions.reject = True
    with pytest.raises(payments_mod.PaymentError) as err:
        payments.create_checkout(reference="R", quantity=1, unit_amount_cents=500, success_url="s",
                                 cancel_url="c", idempotency_key="k")
    assert err.value.retry is False  # pressing again won't help
    assert str(err.value) == "Stripe rejected the payment setup."
    assert len(lines) == 1
    line = lines[0]
    assert "InvalidRequestError" in line and "code=parameter_invalid" in line
    assert "param=line_items[0][price_data][unit_amount]" in line and "Invalid integer: abc" in line
    assert "SECRET" not in line and "sk_test" not in line


def test_network_error_can_be_retried_and_is_logged():
    lines = []
    payments, sessions = fake_stripe(log=lines.append)
    sessions.fail = True
    with pytest.raises(payments_mod.PaymentError) as err:
        payments.create_checkout(reference="R", quantity=1, unit_amount_cents=500, success_url="s",
                                 cancel_url="c", idempotency_key="k")
    assert err.value.retry is True
    assert len(lines) == 1 and "ConnectionError" in lines[0] and "sk_test" not in lines[0]


def test_retrack_never_handles_card_numbers():
    for name in ("payments.py", "web.py", "web_pages.py"):
        src = (ROOT / "retrack" / name).read_text().lower()
        for word in ("card_number", "cvc\"", "exp_month", "payment_method_data"):
            assert word not in src, (name, word)


# ---- The pay steps, end to end, with the fake Stripe --------------------------------------------

def start(tmp_path, payments, **kw):
    srv = web.start_server(MockClient, mock=True, port=0, sessions=SessionStore(tmp_path / "sessions", **kw),
                           payments=payments)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def stripe_server(tmp_path):
    payments, sessions = fake_stripe()
    srv = start(tmp_path, payments)
    srv.fake = sessions
    yield srv
    srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def ready_to_pay(srv, **addons):
    """Scanned, with every song on offer. addons (ai_search=True...) go with each pay()."""
    sid = upload_ok(srv)
    scan(srv, sid, **addons)
    wait_done(srv, sid)
    ids = fixable_ids(text(srv, f"/s/{sid}/results")[1])
    return sid, ids


def pay(srv, sid, ids, attest=True):
    extra = getattr(srv, "checks", {}).get(sid, [])  # the extra checks chosen with scan()
    return call(srv, f"/s/{sid}/pay", *form([("song", i) for i in ids] + extra
                                           + ([("attest", "yes")] if attest else [])))


def test_pay_redirects_to_stripe_checkout(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    status, headers, _ = pay(stripe_server, sid, ids)
    assert status == 303 and headers["Location"].startswith("https://checkout.stripe.test/pay/cs_test_1")
    params, _ = stripe_server.fake.created[0]
    assert params["line_items"][0]["quantity"] == len(ids)
    assert params["line_items"][0]["price_data"]["unit_amount"] == round(FIX_PRICE_USD * 100)
    assert params["success_url"].endswith(f"/s/{sid}/paid?checkout_session_id={{CHECKOUT_SESSION_ID}}")
    assert params["cancel_url"].endswith(f"/s/{sid}/results?checkout=canceled")


def test_rejected_payment_setup_says_so_and_not_to_retry(tmp_path):
    lines = []
    payments, sessions = fake_stripe(log=lines.append)
    srv = start(tmp_path, payments)
    try:
        sid, ids = ready_to_pay(srv)
        sessions.reject = True
        status, _, body = pay(srv, sid, ids)
        page = body.decode()
        assert status == 502
        assert "Stripe rejected the payment setup. Nothing was charged." in page
        assert "press the button again" not in page
        assert lines and "code=parameter_invalid" in lines[0]
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_test_mode_banner_on_the_pay_step(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    _, page = text(stripe_server, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                   content_type="application/x-www-form-urlencoded")
    assert TEST_BANNER in page and "Pay and see your fixes" in page and "Pay (demo)" not in page
    assert "Payments are handled by Stripe. Retrack never sees your card number." in page


def test_unpaid_sessions_cannot_see_fixes(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)  # went to Stripe, but didn't pay
    for path in ("fixes", "fixes.xlsx", "bulk_match.csv", "bulk_work_registration.xlsx"):
        assert call(stripe_server, f"/s/{sid}/{path}")[0] == 402, path
    assert call(stripe_server, f"/s/{sid}/template", *form([]))[0] == 402


def test_paying_unlocks_the_fixes(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)
    stripe_server.fake.customer_pays("cs_test_1")
    status, headers, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
    assert status == 303 and headers["Location"].endswith("/fixes")
    assert open_fixes(stripe_server, sid)[0] == 200
    assert call(stripe_server, f"/s/{sid}/fixes.xlsx")[0] == 200


@pytest.mark.parametrize("tamper", ["other_id", "someone_elses_session", "not_paid"])
def test_a_tampered_return_url_is_rejected(stripe_server, tamper):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)
    if tamper == "other_id":  # a made-up checkout id in the URL
        status, _, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_999")
        assert status == 400
    elif tamper == "someone_elses_session":  # a real, paid checkout, but for a different Retrack session
        stripe_server.fake.customer_pays("cs_test_1")
        stripe_server.fake.store["cs_test_1"].client_reference_id = "someone-else"
        status, _, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
        assert status == 400
    else:  # the right id, but Stripe says it isn't paid
        status, headers, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
        assert status == 303 and "checkout=failed" in headers["Location"]
    assert stripe_server.sessions.get(sid).paid_songs == []
    assert call(stripe_server, f"/s/{sid}/fixes")[0] == 402


def test_paying_twice_for_the_same_order_charges_once(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    first = pay(stripe_server, sid, ids)[1]["Location"]
    second = pay(stripe_server, sid, ids)[1]["Location"]  # back button, double click...
    assert first == second and len(stripe_server.fake.created) == 1
    stripe_server.fake.customer_pays("cs_test_1")
    status, headers, _ = pay(stripe_server, sid, ids)  # paid in the other tab: straight to the fixes
    assert headers["Location"].endswith("/fixes") and len(stripe_server.fake.created) == 1
    call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")  # returning again is harmless
    assert stripe_server.sessions.get(sid).paid_songs == sorted(int(i) for i in ids)


def test_paid_songs_are_never_charged_again(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids[:1])
    stripe_server.fake.customer_pays("cs_test_1")
    call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
    pay(stripe_server, sid, ids)  # ticking everything again, including the paid one
    params, _ = stripe_server.fake.created[-1]
    assert params["line_items"][0]["quantity"] == len(ids) - 1


def test_changing_the_order_closes_the_old_checkout(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)
    pay(stripe_server, sid, ids[:1])  # a different order
    assert stripe_server.fake.expired == ["cs_test_1"]  # the old one can no longer be paid
    assert len(stripe_server.fake.created) == 2


def test_a_lost_redirect_is_recovered_on_the_next_visit(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)
    stripe_server.fake.customer_pays("cs_test_1")  # paid, but never came back to /paid
    _, page = text(stripe_server, f"/s/{sid}/results")
    assert "See your fixes" in page
    assert open_fixes(stripe_server, sid)[0] == 200


def test_canceled_checkout_goes_back_to_results_with_nothing_lost(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)
    status, page = text(stripe_server, f"/s/{sid}/results?checkout=canceled")
    assert status == 200 and "Payment canceled. Nothing was charged" in page
    assert len(fixable_ids(page)) == len(ids)


def test_stripe_down_is_explained_and_charges_nothing(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    stripe_server.fake.fail = True
    status, _, body = pay(stripe_server, sid, ids)
    assert status == 502 and "Nothing was charged" in body.decode() and "SECRET" not in body.decode()


def test_an_expired_session_says_why(tmp_path):
    now = [1000.0]
    srv = start(tmp_path, DemoPayments(), clock=lambda: now[0])
    try:
        sid, ids = ready_to_pay(srv)
        folder = srv.sessions.get(sid).folder
        now[0] += 60 * 60  # an hour later
        status, page = text(srv, f"/s/{sid}/results")
        assert status == 410
        assert "Your files were deleted for your privacy. Please upload again." in page
        assert not folder.exists()
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


# ---- Demo mode: no keys, no network ------------------------------------------------------------

def test_demo_mode_works_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(payments_mod.StripePayments, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Stripe")))
    srv = start(tmp_path, None)  # payments=None: chosen from the (empty) environment
    try:
        assert srv.payments.mode == DEMO
        sid, ids = ready_to_pay(srv)
        _, page = text(srv, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                       content_type="application/x-www-form-urlencoded")
        assert "Pay (demo)" in page and payments_mod.DEMO_BANNER in page
        status, headers, _ = pay(srv, sid, ids)
        assert status == 303 and headers["Location"].endswith("/fixes")
        assert open_fixes(srv, sid)[0] == 200
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_privacy_page_says_how_payments_work(stripe_server, tmp_path):
    _, page = text(stripe_server, "/privacy")
    assert "Payments are handled by Stripe. Retrack never sees your card number." in page.replace("</strong><span>", " ")
    demo = start(tmp_path / "d", DemoPayments())
    try:
        assert "No payment is taken in demo mode." in text(demo, "/privacy")[1]
    finally:
        demo.shutdown(); demo.server_close(); demo.sessions.delete_all()


# ---- The fix page and its downloads ------------------------------------------------------------

@pytest.fixture
def paid(tmp_path):
    srv = start(tmp_path, DemoPayments())
    sid, ids = ready_to_pay(srv, ai_search=True)  # the AI search add-on places the red songs
    pay(srv, sid, ids)
    open_fixes(srv, sid)  # like a customer: the first visit runs the after-checkout checks
    yield srv, sid
    srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def test_fix_page_groups_by_type_with_prefilled_and_must_fill(paid):
    srv, sid = paid
    _, page = open_fixes(srv, sid)
    for head in ('id="group-register">Register', 'id="group-link">Link', 'id="group-claim">Claim'):
        assert head in page
    assert page.count(keep_text()) == 1  # once, at the top, in the box with the downloads
    assert "What Retrack filled in" in page and '<div class="must">' in page
    assert "data-warn-leave" in page
    visible = re.sub(r"<[^>]+>", " ", page.split("<main>")[1].split("</main>")[0])
    assert not re.search(r"(^|\s)--[a-z]", visible)  # no command-line flags for web users


def test_fix_page_has_tabs_and_short_rows_like_the_results_page(paid):
    srv, sid = paid
    _, page = open_fixes(srv, sid)
    # Tabs: hidden until the script runs (with JS off every group just shows), one per kind of fix.
    assert '<div class="filters" role="group" aria-label="Show fixes" hidden>' in page
    for kind in ("all", "register", "link", "claim"):
        assert f'data-filter="{kind}"' in page
        if kind != "all":
            assert f'data-status="{kind}"' in page  # the group that tab shows
    assert "data-expand>Open all" in page
    # Each song is a closed <details> row; opening it shows every field.
    assert page.count('<details class="song fix-card">') == 4
    assert "<details class=\"song fix-card\" open" not in page
    # The keep warning sits with the main download, at the top, before the songs.
    keep = page.index(keep_text())
    assert keep < page.index("fixes.xlsx") < page.index('<details class="song fix-card">')
    # Each tab holds its own file for The MLC.
    register = page.split('data-status="register"')[1].split("</section>")[0]
    link = page.split('data-status="link"')[1].split("</section>")[0]
    assert 'action="/s/' in register and "/template" in register
    assert "bulk_match.csv" in link


def test_each_fix_group_shows_songs_then_steps_then_the_file(paid):
    srv, sid = paid
    _, page = open_fixes(srv, sid)
    for kind in ("register", "link"):
        group = page.split(f'data-status="{kind}"')[1].split("</section>")[0]
        songs, steps, file_ = (group.index('<details class="song fix-card">'), group.index("How to do it"),
                               group.index("File for The MLC"))
        assert songs < steps < file_, kind


def test_steps_are_short_lines_with_the_full_text_under_more_detail(paid):
    from html import escape
    from retrack.web_pages import STEPS_FOR
    srv, sid = paid
    _, page = open_fixes(srv, sid)
    assert page.count('<details class="more"><summary>More detail</summary>') == 3  # one per group
    for kind, steps in STEPS_FOR.items():
        for verb, rest, detail in steps:
            assert len(verb + rest) <= 60, verb  # short enough for one line, on every kind
            if kind not in ("register", "link", "claim"):
                continue  # this sample order has no songs of the other kinds
            assert f"<li><strong>{escape(verb)}</strong>{escape(rest)}</li>" in page
            assert f"<li>{escape(detail)}</li>" in page  # the full explanation is still there


def test_download_label_is_short_but_the_file_keeps_its_date(paid):
    from retrack.fixes import fixes_workbook_name
    srv, sid = paid
    _, page = open_fixes(srv, sid)
    assert ">Download fixes (.xlsx)</a>" in page
    _, headers, _ = call(srv, f"/s/{sid}/fixes.xlsx")
    assert fixes_workbook_name() in headers["Content-Disposition"]


def test_template_upload_is_an_optional_time_saver(paid):
    srv, sid = paid
    _, page = open_fixes(srv, sid)
    register = page.split('data-status="register"')[1].split("</section>")[0]
    assert "Lots of songs? Upload your MLC template and Retrack fills a copy." in register
    assert 'class="mlc-file mlc-file--optional"' in register
    # Retrack never offers a copy of The MLC's template; only the user's own copy, once filled.
    assert "bulk_work_registration.xlsx" not in page


def test_kinds_with_no_songs_get_no_tile_tab_or_section(paid):
    from retrack import web_pages as pages
    from retrack.fixes import plan_fixes
    srv, sid = paid
    session = srv.sessions.get(sid)
    plan = plan_fixes([session.results[i] for i in session.paid_songs], sell_red=True)
    assert plan.register and plan.link and plan.claim
    plan.link = []
    page = pages.fixes_page(session, plan, mock=True)
    assert 'data-filter="link"' not in page and 'data-status="link"' not in page
    assert "<dt>Link</dt>" not in page and "Registered, not linked" not in page
    for kind, head in (("register", "Register"), ("claim", "Claim")):
        assert f'data-filter="{kind}"' in page and f"<dt>{head}</dt>" in page


def test_bulk_match_csv_is_exactly_the_mlc_format(paid):
    srv, sid = paid
    status, headers, data = call(srv, f"/s/{sid}/bulk_match.csv")
    assert status == 200 and "BulkMatchSuggestions_" in headers["Content-Disposition"]
    assert data == b"ISRC,MLC Song Code\nQZABC2400005,BH3456\n"


def test_template_upload_fills_a_copy_and_leaves_the_rest_alone(paid, tmp_path):
    from openpyxl import load_workbook
    from test_fixes import make_template
    from test_web import multipart
    srv, sid = paid
    template = tmp_path / "mlc_template.xlsx"
    make_template(template)
    original = template.read_bytes()
    body, ctype = multipart([("template", ("mlc_template.xlsx", original))])
    status, headers, _ = call(srv, f"/s/{sid}/template", body, ctype)
    assert status == 303
    assert "Download your filled-in template" in open_fixes(srv, sid)[1]
    status, _, data = call(srv, f"/s/{sid}/bulk_work_registration.xlsx")
    assert status == 200
    copy, before = load_workbook(io.BytesIO(data)), load_workbook(io.BytesIO(original))
    assert copy.sheetnames == before.sheetnames  # no extra tabs: The MLC's format, untouched
    assert [c.value for c in copy["Works"][1]] == [c.value for c in before["Works"][1]]
    assert copy["Works"]["A2"].value == "Ghost Signal"
    assert not (srv.sessions.get(sid).folder / "your_template.xlsx").exists()  # only the copy is kept


# ---- .env --------------------------------------------------------------------------------------

def test_env_file_is_loaded_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nMLC_USERNAME=me@label.com\nexport STRIPE_SECRET_KEY='sk_test_123'\n"
                   "EMPTY=\nALREADY=from-file\nTRAILING=value # note\n")
    monkeypatch.setenv("ALREADY", "from-terminal")
    for name in ("MLC_USERNAME", "EMPTY", "TRAILING"):
        monkeypatch.delenv(name, raising=False)
    loaded = load_env(env)
    assert os.environ["MLC_USERNAME"] == "me@label.com"
    assert os.environ["STRIPE_SECRET_KEY"] == "sk_test_123"
    assert os.environ["TRAILING"] == "value"
    assert os.environ["ALREADY"] == "from-terminal"  # the terminal wins
    assert "EMPTY" not in os.environ  # blank lines in .env.example don't set anything
    assert set(loaded) == {"MLC_USERNAME", "STRIPE_SECRET_KEY", "TRAILING"}  # names only, never values


def test_web_main_loads_the_env_file(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("STRIPE_SECRET_KEY=sk_live_fromfile\nMLC_USERNAME=u\nMLC_PASSWORD=p\n")
    monkeypatch.setenv("RETRACK_ENV_FILE", str(env))
    for name in ("MLC_USERNAME", "MLC_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert web.main(["--port", "0"], open_browser=None) == 1  # it read the live key from .env and refused it
    assert "live Stripe key" in capsys.readouterr().err


def test_env_example_lists_every_variable_blank():
    values = parse_env((ROOT / ".env.example").read_text())
    for name in ("MLC_USERNAME", "MLC_PASSWORD", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY",
                 "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET", "RETRACK_PAYMENTS_LIVE",
                 "RETRACK_AI_DAILY_LIMIT_USD"):
        assert name in values and values[name] == "", name


# ---- The fix page warning tells the truth --------------------------------------------------------

def test_warning_text_matches_session_seconds():
    minutes = SESSION_SECONDS // 60
    assert keep_text() == (f"Your fixes stay here for {minutes} minutes, then your files are deleted for your "
                           "privacy. Print, download, or screenshot this page to keep it.")
    assert "disappears when you leave" not in keep_text()  # a refresh never loses a paid page
    assert keep_text(30 * 60).startswith("Your fixes stay here for 30 minutes")


def test_time_left_is_rendered_by_the_server_and_counts_down(tmp_path):
    now = [0.0]
    srv = start(tmp_path, DemoPayments(), clock=lambda: now[0])
    try:
        sid, ids = ready_to_pay(srv)
        pay(srv, sid, ids)
        page = open_fixes(srv, sid)[1]
        assert time_left_text(SESSION_SECONDS) == "About 60 minutes left."
        assert page.count("About 60 minutes left.") == 1  # in the page itself: works with JS off
        assert 'data-time-left-url="/s/' in page  # for the script's live updates
        now[0] += 15 * 60
        assert "About 45 minutes left." in open_fixes(srv, sid)[1]
        import json
        status, _, body = call(srv, f"/s/{sid}/time_left")
        assert status == 200 and json.loads(body) == {"seconds": 45 * 60, "text": "About 45 minutes left."}
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


@pytest.mark.parametrize("seconds,expected", [(3600, "About 60 minutes left."), (61, "About 2 minutes left."),
                                              (60, "About 1 minute left."), (5, "About 1 minute left."),
                                              (0, "Time’s up: your files are being deleted.")])
def test_time_left_wording(seconds, expected):
    assert time_left_text(seconds) == expected


# ---- The signed Stripe webhook ---------------------------------------------------------------------

import hashlib
import hmac
import json
import time as _time

SECRET = "whsec_test_secret"


def signed(event, secret=SECRET, timestamp=None):
    """A webhook body and Stripe-Signature header, signed the way Stripe signs them."""
    payload = json.dumps(event).encode()
    t = int(timestamp if timestamp is not None else _time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={t},v1={sig}"


def checkout_event(checkout_id, reference, paid=True, amount=None, kind="checkout.session.completed", n=1):
    return {"id": f"evt_{n}", "object": "event", "type": kind, "data": {"object": {
        "id": checkout_id, "object": "checkout.session", "client_reference_id": reference,
        "payment_status": "paid" if paid else "unpaid", "status": "complete",
        "amount_total": amount, "url": None}}}


def webhook(srv, payload, signature=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["Stripe-Signature"] = signature
    if origin:
        headers["Origin"] = origin
    return call(srv, "/stripe/webhook", payload, headers=headers)


@pytest.fixture
def hooked(tmp_path):
    """A server with fake Stripe and a webhook secret; logs collected for checking."""
    payments, sessions = fake_stripe()
    logs = []
    srv = web.start_server(MockClient, mock=True, port=0, sessions=SessionStore(tmp_path / "sessions"),
                           payments=payments, webhook_secret=SECRET, log=logs.append)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.fake, srv.logs = sessions, logs
    yield srv
    srv.shutdown(); srv.server_close(); srv.sessions.delete_all()


def open_order(srv):
    sid, ids = ready_to_pay(srv)
    pay(srv, sid, ids)
    order = srv.sessions.get(sid).order
    return sid, ids, order


def test_unsigned_or_badly_signed_webhooks_are_rejected(hooked):
    sid, _, order = open_order(hooked)
    event = checkout_event(order["checkout_id"], sid, amount=order["amount"])
    payload, good = signed(event)
    assert webhook(hooked, payload)[0] == 400  # no signature at all
    assert webhook(hooked, payload, signed(event, secret="whsec_wrong")[1])[0] == 400  # signed by someone else
    assert webhook(hooked, payload + b" ", good)[0] == 400  # body changed after signing
    assert webhook(hooked, payload, signed(event, timestamp=_time.time() - 3600)[1])[0] == 400  # an old replay
    assert hooked.sessions.get(sid).paid_songs == []


def test_no_webhook_secret_means_no_webhook_is_trusted(stripe_server):
    sid, ids = ready_to_pay(stripe_server)
    pay(stripe_server, sid, ids)
    order = stripe_server.sessions.get(sid).order
    payload, sig = signed(checkout_event(order["checkout_id"], sid, amount=order["amount"]))
    assert webhook(stripe_server, payload, sig)[0] == 400  # this server has no STRIPE_WEBHOOK_SECRET
    assert stripe_server.sessions.get(sid).paid_songs == []


def test_a_valid_webhook_unlocks_the_order_without_the_return_page(hooked):
    sid, ids, order = open_order(hooked)
    payload, sig = signed(checkout_event(order["checkout_id"], sid, amount=order["amount"]))
    status, _, body = webhook(hooked, payload, sig)
    assert status == 200 and json.loads(body)["unlocked"] is True
    assert hooked.sessions.get(sid).paid_songs == sorted(int(i) for i in ids)
    assert open_fixes(hooked, sid)[0] == 200


def test_async_payment_unlocks_only_when_it_succeeds(hooked):
    sid, _, order = open_order(hooked)
    payload, sig = signed(checkout_event(order["checkout_id"], sid, paid=False, amount=order["amount"]))
    assert json.loads(webhook(hooked, payload, sig)[2])["unlocked"] is False  # completed, but still unpaid
    assert hooked.sessions.get(sid).paid_songs == []
    payload, sig = signed(checkout_event(order["checkout_id"], sid, amount=order["amount"], n=2,
                                         kind="checkout.session.async_payment_succeeded"))
    assert json.loads(webhook(hooked, payload, sig)[2])["unlocked"] is True


def test_a_duplicate_webhook_is_harmless(hooked):
    sid, ids, order = open_order(hooked)
    payload, sig = signed(checkout_event(order["checkout_id"], sid, amount=order["amount"]))
    webhook(hooked, payload, sig)
    status, _, body = webhook(hooked, payload, sig)  # Stripe retries; the same event again
    assert status == 200 and json.loads(body)["unlocked"] is False
    assert hooked.sessions.get(sid).paid_songs == sorted(int(i) for i in ids)
    # and the return page after the webhook just goes to the fixes
    status, headers, _ = call(hooked, f"/s/{sid}/paid?checkout_session_id={order['checkout_id']}")
    assert status == 303 and headers["Location"].endswith("/fixes")


def test_webhook_for_an_expired_session_is_acknowledged_and_stores_nothing(hooked):
    gone = "x" * 22  # a session id that no longer exists
    before = list(hooked.sessions.root.iterdir())
    payload, sig = signed(checkout_event("cs_test_old", gone, amount=500))
    status, _, body = webhook(hooked, payload, sig)
    assert status == 200 and json.loads(body) == {"received": True}
    assert list(hooked.sessions.root.iterdir()) == before  # nothing created or stored
    assert hooked.logs == ["Stripe webhook: checkout.session.completed for a Retrack session that's "
                           "already deleted (1 so far)."]  # the type and a count, nothing else


@pytest.mark.parametrize("wrong", ["amount", "other_session", "other_checkout"])
def test_a_mismatched_webhook_unlocks_nothing(hooked, wrong):
    sid, _, order = open_order(hooked)
    other_sid, _, _ = open_order(hooked)
    event = {"amount": checkout_event(order["checkout_id"], sid, amount=order["amount"] - 100),
             "other_session": checkout_event(order["checkout_id"], other_sid, amount=order["amount"]),
             "other_checkout": checkout_event("cs_test_999", sid, amount=order["amount"])}[wrong]
    payload, sig = signed(event)
    status, _, body = webhook(hooked, payload, sig)
    assert status == 200 and json.loads(body)["unlocked"] is False
    assert hooked.sessions.get(sid).paid_songs == [] and hooked.sessions.get(other_sid).paid_songs == []


def test_other_event_types_are_acknowledged_and_ignored(hooked):
    payload, sig = signed({"id": "evt_9", "object": "event", "type": "customer.created",
                           "data": {"object": {"id": "cus_1", "object": "customer"}}})
    status, _, body = webhook(hooked, payload, sig)
    assert status == 200 and json.loads(body)["ignored"] is True


def test_only_the_webhook_skips_the_origin_check(hooked):
    sid, _, order = open_order(hooked)
    payload, sig = signed(checkout_event(order["checkout_id"], sid, amount=order["amount"]))
    # the webhook doesn't care about Origin (Stripe isn't a browser): the signature protects it
    assert webhook(hooked, payload, sig, origin="https://some-other-site.example")[0] == 200
    # every other POST still needs this page as its origin, look-alike paths included
    for path in ("/stripe/webhook/", "/stripe/webhooks", f"/s/{sid}/pay", f"/s/{sid}/delete", "/upload"):
        assert call(hooked, path, b"", "application/x-www-form-urlencoded",
                    {"Origin": "https://some-other-site.example"})[0] == 403, path


# ---- --mock-mlc: sample data, real Stripe test checkout --------------------------------------------

def run_main(monkeypatch, argv):
    """Run web.main up to the point it would serve, and hand back the server it built."""
    seen = {}

    def fake_run(server, open_browser):
        seen["server"] = server
        return 0

    monkeypatch.setattr(web, "run_until_stopped", fake_run)
    code = web.main(argv + ["--port", "0"], open_browser=None)
    return code, seen.get("server")


def test_mock_mlc_uses_stripe_when_test_keys_are_set(monkeypatch, tmp_path):
    monkeypatch.setattr("retrack.sessions.DEFAULT_ROOT", tmp_path / "root")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    code, server = run_main(monkeypatch, ["--mock-mlc"])
    assert code == 0 and server.payments.mode == TEST  # real Stripe test checkout (no API call made here)
    assert isinstance(server.payments, StripePayments)


def test_mock_mlc_shows_the_banner_and_stripe_on_the_pay_step(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    payments, fake = fake_stripe()
    monkeypatch.setattr(web, "payments_from_env", lambda env, mock=False: payments if not mock else DemoPayments())
    started = {}

    def fake_run(server, open_browser):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            sid, ids = ready_to_pay(server)
            started["pages"] = [text(server, "/")[1], text(server, f"/s/{sid}/results")[1],
                                text(server, f"/s/{sid}/fix", data=form([("song", i) for i in ids])[0],
                                     content_type="application/x-www-form-urlencoded")[1]]
            started["pay"] = pay(server, sid, ids)
        finally:
            server.shutdown()
        return 0

    monkeypatch.setattr("retrack.sessions.DEFAULT_ROOT", tmp_path / "root")
    monkeypatch.setattr(web, "run_until_stopped", fake_run)
    assert web.main(["--mock-mlc", "--port", "0"], open_browser=None) == 0
    for page in started["pages"]:
        assert "Sample data, not real results." in page
    assert TEST_BANNER in started["pages"][2] and "Pay and see your fixes" in started["pages"][2]
    status, headers, _ = started["pay"]
    assert status == 303 and headers["Location"].startswith("https://checkout.stripe.test/")
    assert len(fake.created) == 1


def test_mock_mlc_without_keys_is_demo_and_mock_stays_demo(monkeypatch, tmp_path):
    monkeypatch.setattr("retrack.sessions.DEFAULT_ROOT", tmp_path / "root")
    assert run_main(monkeypatch, ["--mock-mlc"])[1].payments.mode == DEMO
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
    assert run_main(monkeypatch, ["--mock"])[1].payments.mode == DEMO  # --mock: fully offline, as before


def test_mock_mlc_still_refuses_a_live_key(monkeypatch, capsys):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
    assert web.main(["--mock-mlc", "--port", "0"], open_browser=None) == 1
    assert "RETRACK_PAYMENTS_LIVE=1" in capsys.readouterr().err


# ---- Add-ons: AI search and Smart names, a flat price per order ------------------------------

from retrack import web_pages as pages  # noqa: E402
from retrack.fixes import (ADDON_PRICE_USD, AI_SEARCH, SMART_NAMES, addons_offered,  # noqa: E402
                           order_total, price_line, price_parts, sold_kind)

ADDON_CENTS = round(ADDON_PRICE_USD * 100)
SONG_CENTS = round(FIX_PRICE_USD * 100)


def song_ids(srv, sid, status=None):
    """The songs on offer (as form values), optionally only red or only yellow ones."""
    results = srv.sessions.get(sid).results
    return [str(i) for i, r in enumerate(results) if sold_kind(r) and (status is None or r.status == status)]


def pay_with(srv, sid, ids, addons=(), attest=True):
    fields = [("song", i) for i in ids] + [(a, "on") for a in addons] + ([("attest", "yes")] if attest else [])
    return call(srv, f"/s/{sid}/pay", *form(fields))


def test_add_on_prices_are_constants_and_flat_per_order():
    assert FIX_PRICE_USD == 5.00 and ADDON_PRICE_USD == 2
    songs = [object()] * 7
    assert order_total(songs, [AI_SEARCH]) == 7 * FIX_PRICE_USD + 2  # not per song
    # one entry per order that bought it: two orders with AI search pay for it twice, once each
    assert order_total(songs, [AI_SEARCH, AI_SEARCH]) == 7 * FIX_PRICE_USD + 4


@pytest.mark.parametrize("n", [1, 4])
@pytest.mark.parametrize("addons", [[], [AI_SEARCH], [SMART_NAMES], [AI_SEARCH, SMART_NAMES]])
def test_the_price_line_and_total_for_every_combination(n, addons):
    songs = [object()] * n
    parts = price_parts(songs, addons)
    total = n * FIX_PRICE_USD + 2 * len(addons)
    assert parts[0] == f"{n} {'song' if n == 1 else 'songs'} × $5.00 = ${n * 5:.2f}"
    assert ("AI search $2.00" in parts) == (AI_SEARCH in addons)
    assert ("Smart names $2.00" in parts) == (SMART_NAMES in addons)
    assert order_total(songs, addons) == total
    if addons:
        assert parts[-1] == f"Total ${total:.2f}"
    else:
        assert parts == [f"{n} {'song' if n == 1 else 'songs'} × $5.00 = ${n * 5:.2f}"]  # already the total
    assert price_parts([object()] * 4, [AI_SEARCH, SMART_NAMES]) == [
        "4 songs × $5.00 = $20.00", "AI search $2.00", "Smart names $2.00", "Total $24.00"]
    assert price_line(songs, addons) == ", ".join(parts)  # the same, as one line of text
    assert "·" not in price_line(songs, addons)


def test_the_command_line_uses_the_same_price_line(capsys):
    songs = [object()] * 4
    assert price_line(songs) == "4 songs × $5.00 = $20.00"  # what the CLI prints, like the web pages


def test_stripe_gets_a_line_for_the_songs_and_one_per_add_on(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    ids = song_ids(stripe_server, sid)
    status, headers, _ = pay_with(stripe_server, sid, ids, [AI_SEARCH, SMART_NAMES])
    assert status == 303 and headers["Location"].startswith("https://checkout.stripe.test/")
    params, _ = stripe_server.fake.created[0]
    items = [(li["quantity"], li["price_data"]["unit_amount"], li["price_data"]["product_data"]["name"])
             for li in params["line_items"]]
    assert items == [(len(ids), SONG_CENTS, payments_mod.PRODUCT_NAME),
                     (1, ADDON_CENTS, payments_mod.ADDON_PRODUCT_NAMES[AI_SEARCH]),
                     (1, ADDON_CENTS, payments_mod.ADDON_PRODUCT_NAMES[SMART_NAMES])]
    order = stripe_server.sessions.get(sid).order
    assert order["amount"] == len(ids) * SONG_CENTS + 2 * ADDON_CENTS  # the full total is what's checked


@pytest.mark.parametrize("addons", [[], [AI_SEARCH], [SMART_NAMES], [AI_SEARCH, SMART_NAMES]])
def test_paying_unlocks_exactly_the_add_ons_paid_for(stripe_server, addons):
    sid, _ = ready_to_pay(stripe_server)
    ids = song_ids(stripe_server, sid)
    pay_with(stripe_server, sid, ids, addons)
    stripe_server.fake.customer_pays("cs_test_1")
    status, headers, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
    assert status == 303 and headers["Location"].endswith("/fixes")
    s = stripe_server.sessions.get(sid)
    assert {a for a in (AI_SEARCH, SMART_NAMES) if s.settings[a]} == set(addons)  # only those run after checkout


def test_a_tampered_amount_is_rejected(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    ids = song_ids(stripe_server, sid)
    pay_with(stripe_server, sid, ids, [AI_SEARCH])
    stripe_server.fake.customer_pays("cs_test_1")
    stripe_server.fake.store["cs_test_1"].amount_total -= ADDON_CENTS  # paid for the songs only
    status, headers, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
    assert "/fixes" not in headers.get("Location", "")
    s = stripe_server.sessions.get(sid)
    assert s.paid_songs == [] and s.settings[AI_SEARCH] is False  # nothing unlocked, no AI


def test_changing_the_add_ons_changes_the_order(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    ids = song_ids(stripe_server, sid)
    pay_with(stripe_server, sid, ids)
    pay_with(stripe_server, sid, ids, [AI_SEARCH])  # same songs, one add-on more
    assert stripe_server.fake.expired == ["cs_test_1"] and len(stripe_server.fake.created) == 2
    pay_with(stripe_server, sid, ids, [AI_SEARCH])  # the same order again: reused, not a new charge
    assert len(stripe_server.fake.created) == 2


def test_an_add_on_no_ticked_song_needs_cant_be_bought(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    yellow = song_ids(stripe_server, sid, status="YELLOW")
    red = song_ids(stripe_server, sid, status="RED")
    status, _, body = pay_with(stripe_server, sid, yellow, [AI_SEARCH])
    assert status == 400 and "AI search isn’t needed for these songs" in body.decode()
    status, _, body = pay_with(stripe_server, sid, red, [SMART_NAMES])  # red songs have no writers to compare
    assert status == 400 and "Smart names isn’t needed for these songs" in body.decode()
    assert stripe_server.fake.created == []  # no checkout, nothing charged


def test_disabled_add_ons_say_why():
    session = SimpleNamespace(id="x" * 22, paid_songs=[], settings={AI_SEARCH: False, SMART_NAMES: False},
                              results=[])
    html = pages._addons_html(session, offered=[SMART_NAMES], chosen=[], mock=False)
    ai = html.split('id="ai_search"')[1].split("</label>")[0]
    assert " disabled " in ai.split(">")[0] + " " and "Only needed for red songs." in ai
    assert 'id="ai_search-why" class="addon-why">' in ai  # the reason is shown, not hidden
    names = html.split('id="smart_names"')[1].split("</label>")[0]
    assert "disabled" not in names.split(">")[0] and 'class="addon-why" hidden' in names


def test_add_ons_follow_the_ticked_songs(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    results = stripe_server.sessions.get(sid).results
    yellow = [results[int(i)] for i in song_ids(stripe_server, sid, status="YELLOW")]
    red = [results[int(i)] for i in song_ids(stripe_server, sid, status="RED")]
    assert AI_SEARCH not in addons_offered(yellow) and AI_SEARCH in addons_offered(red)
    assert SMART_NAMES not in addons_offered(red)
    # each song row says which add-ons it can use, so the page's script can grey the others out
    _, page = text(stripe_server, f"/s/{sid}/results")
    for i in song_ids(stripe_server, sid, status="RED"):
        assert f'value="{i}" data-addons="ai_search"' in page


def test_select_all_ticks_every_song_and_never_an_add_on(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    ids = song_ids(stripe_server, sid)
    # With JS off: the "Select all songs" button sends the form back with every song ticked.
    status, _, body = call(stripe_server, f"/s/{sid}/fix", *form([("select_all", "1")]))
    page = body.decode()
    assert status == 200
    for i in ids:
        assert f'name="song" value="{i}" checked' in page
    form_html = page.split('id="fix-form"')[1].split("</form>")[0]
    for addon in (AI_SEARCH, SMART_NAMES):
        assert "checked" not in form_html.split(f'id="{addon}"')[1].split(">")[0]  # add-ons untouched
    assert f'<span class="price-part">{len(ids)} songs × $5.00 = ${len(ids) * 5:.2f}</span>' in page
    # With JS on: the checkbox has no name (it's never sent) and isn't an add-on.
    box = form_html.split('id="select-all"')[0].rsplit("<input", 1)[1] + form_html.split('id="select-all"')[1].split(">")[0]
    assert "name=" not in box and "data-addon" not in box
    assert "Select all songs" in form_html and stripe_server.fake.created == []
    script = (ROOT / "retrack" / "home.js").read_text()
    handler = script.split('all.addEventListener("change"')[1].split("});")[0]
    assert "songs.forEach" in handler and "addons" not in handler  # the script ticks songs only
    assert "indeterminate" in script  # a partial selection shows as mixed


def _paid_without_ai_search(srv):
    sid, _ = ready_to_pay(srv)
    ids = song_ids(srv, sid)
    pay_with(srv, sid, ids)
    srv.fake.customer_pays("cs_test_1")
    call(srv, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
    open_fixes(srv, sid)
    return sid, ids


def test_the_ai_search_add_on_alone_can_be_bought_on_the_fix_page(stripe_server):
    sid, ids = _paid_without_ai_search(stripe_server)
    _, page = open_fixes(stripe_server, sid)
    assert "Add AI search for $2" in page
    status, headers, _ = call(stripe_server, f"/s/{sid}/recheck", *form([("ai_search", "on")]))
    assert status == 303 and headers["Location"] == "https://checkout.stripe.test/pay/cs_test_2"
    params, _ = stripe_server.fake.created[-1]
    assert [(li["quantity"], li["price_data"]["unit_amount"]) for li in params["line_items"]] == [(1, ADDON_CENTS)]
    assert params["cancel_url"].endswith(f"/s/{sid}/fixes")
    s = stripe_server.sessions.get(sid)
    assert s.order["songs"] == [] and s.order["amount"] == ADDON_CENTS
    stripe_server.fake.customer_pays("cs_test_2")
    status, headers, _ = call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_2")
    assert headers["Location"].endswith("/fixes")
    status, page = open_fixes(stripe_server, sid)  # "Working out…", then the red songs are placed
    assert status == 200 and s.settings[AI_SEARCH] is True
    assert 'class="needs-search"' not in page
    assert s.paid_songs == sorted(int(i) for i in ids)  # no song charged or added again


def test_ai_search_is_never_charged_twice(stripe_server):
    sid, ids = _paid_without_ai_search(stripe_server)
    call(stripe_server, f"/s/{sid}/recheck", *form([("ai_search", "on")]))
    stripe_server.fake.customer_pays("cs_test_2")
    call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_2")
    open_fixes(stripe_server, sid)
    created = len(stripe_server.fake.created)
    # Asking again runs it again for free: no new checkout.
    status, headers, _ = call(stripe_server, f"/s/{sid}/recheck", *form([("ai_search", "on")]))
    assert status == 303 and not headers["Location"].startswith("https://")
    assert len(stripe_server.fake.created) == created


def test_add_ons_are_per_order_and_cover_only_that_orders_songs(stripe_server):
    sid, _ = ready_to_pay(stripe_server)
    red = song_ids(stripe_server, sid, status="RED")
    yellow = song_ids(stripe_server, sid, status="YELLOW")
    pay_with(stripe_server, sid, red[:1], [AI_SEARCH])  # order 1: one red song, with AI search
    stripe_server.fake.customer_pays("cs_test_1")
    call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_1")
    s = stripe_server.sessions.get(sid)
    assert s.addon_songs[AI_SEARCH] == {int(red[0])}
    # The results page offers it again, unticked: a new order pays for its own add-ons.
    _, page = text(stripe_server, f"/s/{sid}/results")
    ai = page.split('id="ai_search"')[1].split(">")[0]
    assert "checked" not in ai and "data-paid" not in page
    # Order 2: the other red song, no add-on. AI search from order 1 doesn't cover it.
    pay_with(stripe_server, sid, red[1:] + yellow)
    params, _ = stripe_server.fake.created[-1]
    assert [(li["quantity"], li["price_data"]["unit_amount"]) for li in params["line_items"]] == \
        [(len(red[1:] + yellow), SONG_CENTS)]
    stripe_server.fake.customer_pays("cs_test_2")
    call(stripe_server, f"/s/{sid}/paid?checkout_session_id=cs_test_2")
    assert s.addon_songs[AI_SEARCH] == {int(red[0])}
    _, page = open_fixes(stripe_server, sid)
    assert "Add AI search for $2" in page  # the uncovered red song can still get it, as its own order
    # Order 3, ticking AI search again for a new red song: charged again, once, for that order.
    # (Here: buying it on the fix page for the uncovered song.)
    call(stripe_server, f"/s/{sid}/recheck", *form([("ai_search", "on")]))
    params, _ = stripe_server.fake.created[-1]
    assert [(li["quantity"], li["price_data"]["unit_amount"]) for li in params["line_items"]] == [(1, ADDON_CENTS)]
    assert s.order["covers"] == [int(red[1])]  # only the song no order covered


def test_ai_search_bought_with_one_order_never_runs_on_another_orders_songs(tmp_path):
    from test_web import start as start_web
    from retrack.mlc_client import MockClient as Mock
    searched = []

    class Recording(Mock):
        def search_songcode(self, title, writers=()):
            searched.append(title)
            return super().search_songcode(title, writers)

    srv = start_web(tmp_path, Recording)  # demo payments, offline AI stand-ins
    try:
        sid = upload_ok(srv)
        scan(srv, sid)
        wait_done(srv, sid)
        results = srv.sessions.get(sid).results
        red = [str(i) for i, r in enumerate(results) if r.status == "RED"]
        # Two orders before the fix page opens, so both songs are worked out in the same run:
        pay_with(srv, sid, red[:1], [AI_SEARCH])  # order 1: with AI search
        pay_with(srv, sid, red[1:])               # order 2: without
        open_fixes(srv, sid)
        searched_titles = {t.upper() for t in searched}
        assert any(results[int(red[0])].row.title.upper() in t for t in searched_titles)
        assert not any(results[int(i)].row.title.upper() in t for i in red[1:] for t in searched_titles)
    finally:
        srv.shutdown(); srv.server_close(); srv.sessions.delete_all()
