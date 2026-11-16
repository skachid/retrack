"""Talks to The MLC's Public Search API.

Spec: https://public-api.themlc.com/api/doc (v1.2.0)

How the lookup works (the API has no "give me the work for this ISRC" call):
  1. POST /search/recordings with the ISRC  -> recording(s), each with an mlcsongCode
  2. POST /works with those song codes      -> full works: writers, publishers, shares

Two clients live here:
  - MLCClient:  the real thing. Needs MLC_USERNAME / MLC_PASSWORD (you get these
                after The MLC approves your Public Search API access).
  - MockClient: reads canned JSON from fixtures/ so you can build and test
                without API access.
"""

import json
import os
from pathlib import Path

import requests

BASE_URL = "https://public-api.themlc.com"


class MLCError(Exception):
    """Anything that went wrong talking to The MLC."""


class MLCClient:
    def __init__(self, username=None, password=None, base_url=BASE_URL, timeout=20):
        self.username = username or os.environ.get("MLC_USERNAME")
        self.password = password or os.environ.get("MLC_PASSWORD")
        if not self.username or not self.password:
            raise MLCError(
                "Missing MLC credentials. Set MLC_USERNAME and MLC_PASSWORD "
                "(see .env.example), or run with --mock."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._token = None

    def _login(self):
        resp = self.session.post(
            f"{self.base_url}/oauth/token",
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise MLCError(f"Login failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        if data.get("error"):
            raise MLCError(f"Login failed: {data.get('errorDescription') or data['error']}")
        self._token = data["accessToken"]

    def _post(self, path, body):
        if self._token is None:
            self._login()
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = self.session.post(f"{self.base_url}{path}", json=body, headers=headers, timeout=self.timeout)
        if resp.status_code == 401:  # token expired -> log in again once
            self._login()
            headers = {"Authorization": f"Bearer {self._token}"}
            resp = self.session.post(f"{self.base_url}{path}", json=body, headers=headers, timeout=self.timeout)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise MLCError(f"POST {path} failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json() or []

    def search_recordings(self, isrc):
        return self._post("/search/recordings", {"isrc": isrc})

    def get_works(self, song_codes):
        if not song_codes:
            return []
        # Note the spec's lowercase "s": mlcsongCode
        return self._post("/works", [{"mlcsongCode": code} for code in song_codes])


class MockClient:
    """Fake MLC using fixtures/mlc_mock.json. Same methods as MLCClient."""

    def __init__(self, fixture_path=None):
        path = Path(fixture_path or Path(__file__).parent.parent / "fixtures" / "mlc_mock.json")
        data = json.loads(path.read_text())
        self.recordings = data["recordings"]  # isrc -> list of recordings
        self.works = data["works"]  # song code -> work

    def search_recordings(self, isrc):
        return self.recordings.get(isrc, [])

    def get_works(self, song_codes):
        return [self.works[c] for c in song_codes if c in self.works]
