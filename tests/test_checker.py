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
