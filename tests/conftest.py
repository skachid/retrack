"""Settings for every test.

No test may read your real .env, or use real Stripe keys from your terminal:
the environment is cleaned before each test.
"""

import pytest


@pytest.fixture(autouse=True)
def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRACK_ENV_FILE", str(tmp_path / "no-such.env"))  # never your real .env
    for name in ("STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "RETRACK_PAYMENTS_LIVE"):
        monkeypatch.delenv(name, raising=False)
