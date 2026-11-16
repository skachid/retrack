from pathlib import Path

import pytest

from retrack.checker import (GREEN, RED, YELLOW, check_catalog, load_catalog, names_match,
                                   normalize_isrc)
from retrack.mlc_client import MockClient

ROOT = Path(__file__).parent.parent


@pytest.fixture
def results():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    return {r.row.title: r for r in check_catalog(rows, MockClient())}


def test_fully_registered_song_is_green(results):
    r = results["Late Night Drive"]
    assert r.status == GREEN
    assert r.claimed_share == 100
    assert r.problems == []


def test_half_claimed_song_is_yellow(results):
    r = results["Paper Crowns"]
    assert r.status == YELLOW
    assert any("50%" in p for p in r.problems)


def test_no_publisher_and_missing_writer_is_yellow(results):
    r = results["Concrete Garden"]
    assert r.status == YELLOW
    assert any("No publisher" in p for p in r.problems)
    assert any("Sam Lee" in p for p in r.problems)


def test_unknown_isrc_is_red(results):
    assert results["Ghost Signal"].status == RED


def test_recording_without_song_link_is_red(results):
    r = results["Blue Hour"]
    assert r.status == RED
    assert "isn't linked" in r.problems[0]


def test_isrc_normalization():
    assert normalize_isrc(" qz-abc-24-00001 ") == "QZABC2400001"


@pytest.mark.parametrize("a,b,expected", [
    ("José O'Neil", "Jose ONeil", True),
    ("Marcus Bell", "marcus  bell", True),
    ("Marcus Bell", "Sam Lee", False),
])
def test_names_match(a, b, expected):
    assert names_match(a, b) is expected


def test_missing_column_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("title,isrc\nSong,QZ1\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_catalog(bad)


# ---- Smarter writer-name matching (second pass) ----------------------------

import json
from types import SimpleNamespace

from retrack.checker import find_writer
from retrack.name_matcher import ClaudeNameMatcher, MatchVerdict, MockNameMatcher


class CountingMatcher:
    """Fake matcher that always gives the same answer and counts how often it's asked."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def same_person(self, a, b):
        self.calls += 1
        return self.verdict


def test_basic_match_skips_the_smart_matcher():
    matcher = CountingMatcher(MatchVerdict(False, 1.0))
    assert find_writer("Marcus Bell", ["marcus bell"], matcher) == (True, "")
    assert matcher.calls == 0  # pass 1 was enough, so no API call


def test_confident_yes_counts_as_found_with_a_note():
    found, note = find_writer("Jide Okafor", ["J. Okafor"], CountingMatcher(MatchVerdict(True, 0.9)))
    assert found
    assert "J. Okafor" in note


def test_unsure_yes_still_counts_as_missing():
    found, note = find_writer("Jide Okafor", ["J. Okafor"], CountingMatcher(MatchVerdict(True, 0.5)))
    assert (found, note) == (False, "")


def test_no_matcher_means_basic_matching_only():
    assert find_writer("Jide Okafor", ["J. Okafor"]) == (False, "")


@pytest.mark.parametrize("a,b,expected", [
    ("Jide Okafor", "J. Okafor", True),
    ("Jide Okafor", "Jide Okafor", True),
    ("Jide Okafor", "Kemi Okafor", False),
    ("Jide Okafor", "J. Adeyemi", False),
    ("Cher", "C. Cher", False),  # one-word names: too little to go on
])
def test_mock_name_matcher(a, b, expected):
    assert MockNameMatcher().same_person(a, b).same_person is expected


def test_mock_mode_matches_initials_on_catalog():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    by_title = {r.row.title: r for r in check_catalog(rows, MockClient(), MockNameMatcher())}
    r = by_title["Concrete Garden"]
    assert not any("Jide Okafor" in p for p in r.problems)  # matched to "J. Okafor"
    assert any("Sam Lee" in p for p in r.problems)          # still genuinely missing
    assert any("J. Okafor" in n for n in r.notes)
    assert r.status == YELLOW


# ClaudeNameMatcher tests use a fake API client, so they need no key and no network.

class FakeAnthropic:
    def __init__(self, answer=None, stop_reason="end_turn", error=None):
        self.calls = 0
        self.answer, self.stop_reason, self.error = answer, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        text = SimpleNamespace(type="text", text=json.dumps(self.answer))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])


def test_claude_matcher_reads_the_answer():
    fake = FakeAnthropic({"same_person": True, "confidence": 0.92, "reason": "initial fits"})
    v = ClaudeNameMatcher(client=fake).same_person("Jide Okafor", "J. Okafor")
    assert (v.same_person, v.confidence) == (True, 0.92)


def test_claude_matcher_caches_each_pair():
    fake = FakeAnthropic({"same_person": True, "confidence": 0.9, "reason": ""})
    m = ClaudeNameMatcher(client=fake)
    m.same_person("Jide Okafor", "J. Okafor")
    m.same_person("J. Okafor", "Jide Okafor")  # same pair, other order
    assert fake.calls == 1


def test_claude_matcher_api_error_means_no_match():
    v = ClaudeNameMatcher(client=FakeAnthropic(error=ConnectionError("offline"))).same_person("A B", "A C")
    assert (v.same_person, v.confidence) == (False, 0.0)


def test_claude_matcher_refusal_means_no_match():
    v = ClaudeNameMatcher(client=FakeAnthropic(stop_reason="refusal")).same_person("A B", "A C")
    assert v.same_person is False
