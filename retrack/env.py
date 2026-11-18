"""Load settings from a .env file, so nobody has to `export` them by hand.

    MLC_USERNAME=me@label.com
    STRIPE_SECRET_KEY=sk_test_...

Rules:
  - A variable already set in the environment wins over .env (so a one-off
    `STRIPE_SECRET_KEY=... python -m retrack.web` still works).
  - Values are never printed or logged.
  - The file is .env in the folder you run Retrack from; RETRACK_ENV_FILE can
    point somewhere else (the tests use that to make sure your real .env is
    never read during a test run).

No new dependency: the format is simple enough to read ourselves.
"""

import os
from pathlib import Path


def env_file_path():
    return Path(os.environ.get("RETRACK_ENV_FILE") or Path.cwd() / ".env")


def parse_env(text):
    """KEY=VALUE lines -> dict. Skips blank lines and # comments; allows `export KEY=...`
    and quoted values."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # a comment after an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_env(path=None):
    """Put .env's values into os.environ (without replacing ones already set).
    Returns the names it loaded (names only, never values)."""
    path = Path(path) if path else env_file_path()
    if not path.is_file():
        return []
    loaded = []
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        if key not in os.environ and value != "":
            os.environ[key] = value
            loaded.append(key)
    return loaded
