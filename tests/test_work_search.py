"""Tests for --ai-search: the /search/songcode client, work_search.py, and the
RED split in checker.py (NOT_LINKED vs NOT_FOUND).

No test here needs an API key or the network: the MLC and Claude are faked.
"""

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from retrack.__main__ import main
from retrack.checker import (NOT_FOUND, NOT_LINKED, RED, YELLOW, GREEN, CatalogRow,
                             check_catalog, check_row, load_catalog)
from retrack.mlc_client import MLCClient, MLCError, MockClient, search_writers, split_name
from retrack.report import render_html, write_csv
from retrack.work_search import (MAX_CANDIDATES, MAX_SEARCHES, ClaudeWorkSearcher, MockWorkSearcher,
                                 SearchQuery, WorkVerdict, search_for_work)

ROOT = Path(__file__).parent.parent


@pytest.fixture
def ai_results():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    return {r.row.title: r for r in check_catalog(rows, MockClient(), work_searcher=MockWorkSearcher())}


# ---- mlc_client: POST /search/songcode -------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Jide Okafor", ("Jide", "Okafor")),
    ("Mary Jo Smith", ("Mary Jo", "Smith")),
    ("Cher", ("", "Cher")),
    ("", ("", "")),
])
def test_split_name(name, expected):
    assert split_name(name) == expected


def test_search_writers_uses_the_spec_field_names():
    assert search_writers(["J. Okafor", "  "]) == [{"writerFirstName": "J.", "writerLastName": "Okafor"}]


def test_real_client_posts_title_and_search_writers():
    sent = {}

    class FakeSession:
        def post(self, url, json, headers=None, timeout=None):
            sent["url"], sent["body"] = url, json
            return SimpleNamespace(status_code=200, json=lambda: [{"mlcSongCode": "X1"}], text="")

    client = MLCClient(username="u", password="p")
    client.session, client._token = FakeSession(), "token"  # skip the real login
    assert client.search_songcode("Blue Hour", ["Jide Okafor"]) == [{"mlcSongCode": "X1"}]
    assert sent["url"].endswith("/search/songcode")
    assert sent["body"] == {"title": "Blue Hour",
                            "writers": [{"writerFirstName": "Jide", "writerLastName": "Okafor"}]}


def test_mock_search_needs_the_exact_title():
    assert MockClient().search_songcode("Blue Hour", ["Jide Okafor"]) == []  # it's "THE BLUE HOUR"
    found = MockClient().search_songcode("The Blue Hour", ["Okafor"])
    assert [s["mlcSongCode"] for s in found] == ["BH3456"]


def test_mock_search_by_title_only_finds_look_alikes_too():
    codes = {s["mlcSongCode"] for s in MockClient().search_songcode("Blue Hour")}
    assert codes == {"BH7777"}  # the other "BLUE HOUR", by a different writer


# ---- work_search.py: the search loop ---------------------------------------------

class FakeMLC:
    """Returns the same songs for every search, and records what was searched."""

    def __init__(self, songs=(), error_on=()):
        self.songs, self.error_on, self.searched = list(songs), set(error_on), []

    def search_songcode(self, title, writers=()):
        self.searched.append((title, tuple(writers)))
        if title in self.error_on:
            raise MLCError("down")
        return self.songs


class FakeSearcher:
    def __init__(self, queries=(), verdict=WorkVerdict(True, 0.95, "fits")):
        self.queries, self.verdict, self.judged = queries, verdict, []

    def suggest_queries(self, title, writers):
        return None if self.queries is None else list(self.queries)

    def judge(self, title, writers, candidate):
        self.judged.append(candidate.song_code)
        return self.verdict


def song(code, title="SONG", first="Jide", last="Okafor"):
    return {"mlcSongCode": code, "workTitle": title, "iswc": "",
            "writers": [{"writerFirstName": first, "writerLastName": last}]}


def test_first_search_is_the_catalog_title_and_writers_as_given():
    mlc = FakeMLC()
    search_for_work("Blue Hour", ["Jide Okafor"], mlc, FakeSearcher([SearchQuery("The Blue Hour")]))
    assert mlc.searched[0] == ("Blue Hour", ("Jide Okafor",))
    assert mlc.searched[1] == ("The Blue Hour", ())


def test_never_more_than_max_searches_per_song():
    many = [SearchQuery(f"Title {i}") for i in range(20)]
    mlc = FakeMLC()
    search_for_work("Song", [], mlc, FakeSearcher(many))
    assert len(mlc.searched) == MAX_SEARCHES


def test_repeated_variants_are_searched_once():
    mlc = FakeMLC()
    same = SearchQuery("Song", ("A B",))
    search_for_work("Song", ["A B"], mlc, FakeSearcher([same, SearchQuery("Other"), SearchQuery("Other")]))
    assert len(mlc.searched) == 2


def test_a_song_found_twice_is_judged_once_and_known_codes_are_skipped():
    searcher = FakeSearcher([SearchQuery("Other")])
    mlc = FakeMLC([song("A1"), song("LINKED")])
    outcome = search_for_work("Song", [], mlc, searcher, exclude_codes=["LINKED"])
    assert searcher.judged == ["A1"]
    assert [c.song_code for c in outcome.candidates] == ["A1"]


def test_never_more_than_max_candidates_judged():
    searcher = FakeSearcher()
    search_for_work("Song", [], FakeMLC([song(f"C{i}") for i in range(12)]), searcher)
    assert len(searcher.judged) == MAX_CANDIDATES


def test_ai_unavailable_means_search_incomplete():
    outcome = search_for_work("Song", [], FakeMLC(), FakeSearcher(queries=None))
    assert outcome.complete is False


def test_mlc_error_on_one_search_means_incomplete_but_others_still_run():
    mlc = FakeMLC(error_on={"Song"})
    outcome = search_for_work("Song", [], mlc, FakeSearcher([SearchQuery("Other")]))
    assert outcome.complete is False
    assert outcome.queries == [SearchQuery("Other")]


# ---- MockWorkSearcher ------------------------------------------------------------

def test_mock_variants_try_the_and_last_names_and_title_only():
    qs = MockWorkSearcher().suggest_queries("Blue Hour", ["Jide Okafor"])
    assert SearchQuery("The Blue Hour", ("okafor",)) in qs
    assert SearchQuery("Blue Hour", ()) in qs


@pytest.mark.parametrize("title,writers,expected", [
    ("THE BLUE HOUR", ["Jyde Okafor"], True),         # title and spelling differ: still the same song
    ("BLUE HOUR", ["Priya Natarajan"], False),        # same title, different writer: different song
    ("RED HOUR", ["Jide Okafor"], False),             # same writer, different song
    ("Blue Hour (feat. Someone)", ["J. Okafor"], True),
])
def test_mock_judge(title, writers, expected):
    from retrack.work_search import Candidate
    c = Candidate("X", title, "", writers)
    assert MockWorkSearcher().judge("Blue Hour", ["Jide Okafor"], c).same_song is expected


# ---- ClaudeWorkSearcher (fake API client) ----------------------------------------

class FakeAnthropic:
    def __init__(self, answer=None, stop_reason="end_turn", error=None):
        self.calls, self.sent = 0, []
        self.answer, self.stop_reason, self.error = answer, stop_reason, error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        self.sent.append(kwargs)
        if self.error:
            raise self.error
        text = SimpleNamespace(type="text", text=json.dumps(self.answer))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])


def candidate():
    from retrack.work_search import Candidate
    return Candidate("BH3456", "THE BLUE HOUR", "", ["Jyde Okafor"])


def test_claude_variants_are_read_and_capped_at_four():
    searches = [{"title": f"T{i}", "writers": ["Okafor", " "]} for i in range(6)]
    qs = ClaudeWorkSearcher(client=FakeAnthropic({"searches": searches})).suggest_queries("Blue Hour", ["Jide Okafor"])
    assert len(qs) == 4
    assert qs[0] == SearchQuery("T0", ("Okafor",))


def test_claude_judge_reads_the_answer_and_clamps_confidence():
    fake = FakeAnthropic({"same_song": True, "confidence": 1.7, "reason": "fits"})
    v = ClaudeWorkSearcher(client=fake).judge("Blue Hour", ["Jide Okafor"], candidate())
    assert (v.same_song, v.confidence) == (True, 1.0)


def test_claude_searcher_caches_both_questions():
    fake = FakeAnthropic({"same_song": False, "confidence": 0.9, "reason": "", "searches": []})
    s = ClaudeWorkSearcher(client=fake)
    s.judge("Blue Hour", ["Jide Okafor"], candidate())
    s.judge("Blue Hour", ["Jide Okafor"], candidate())
    assert fake.calls == 1


def test_claude_error_means_no_variants_and_no_match():
    s = ClaudeWorkSearcher(client=FakeAnthropic(error=ConnectionError("offline")))
    assert s.suggest_queries("Blue Hour", []) is None
    assert s.judge("Blue Hour", [], candidate()) == WorkVerdict(False, 0.0, "AI check unavailable")


def test_claude_refusal_means_no_match():
    s = ClaudeWorkSearcher(client=FakeAnthropic({"same_song": True, "confidence": 1}, stop_reason="refusal"))
    assert s.judge("Blue Hour", [], candidate()).same_song is False


def test_claude_is_only_sent_titles_and_writer_names():
    fake = FakeAnthropic({"same_song": False, "confidence": 0.9, "reason": ""})
    ClaudeWorkSearcher(client=fake).judge("Blue Hour", ["Jide Okafor"], candidate())
    text = fake.sent[0]["messages"][0]["content"]
    assert "Blue Hour" in text and "Jyde Okafor" in text
    assert "QZABC" not in text and "Lowlights" not in text  # no ISRC, no artist
    assert fake.sent[0]["output_config"]["format"]["type"] == "json_schema"


# ---- checker.py: splitting RED --------------------------------------------------

def test_registered_but_unlinked_song_is_red_not_linked(ai_results):
    r = ai_results["Blue Hour"]
    assert (r.status, r.red_case) == (RED, NOT_LINKED)
    assert r.suggested_match.song_code == "BH3456"
    assert r.suggested_match.title == "THE BLUE HOUR"
    assert r.suggested_match.writers == ["Jyde Okafor"]
    assert "not linked to your recording" in r.problems[0]


def test_not_linked_fixes_say_confirm_claim_and_attach_isrc_not_register(ai_results):
    fixes = " ".join(ai_results["Blue Hour"].fixes)
    assert "confirm" in fixes
    assert "claim" in fixes
    assert "Attach ISRC QZABC2400005 to song code BH3456" in fixes
    assert "Don't register the song again" in fixes


def test_look_alike_by_another_writer_is_not_suggested(ai_results):
    r = ai_results["Blue Hour"]
    assert r.suggested_match.song_code != "BH7777"
    assert r.other_matches == []


def test_song_found_nowhere_is_red_not_found(ai_results):
    r = ai_results["Ghost Signal"]
    assert (r.status, r.red_case) == (RED, NOT_FOUND)
    assert r.suggested_match is None
    assert "Not found anywhere" in r.problems[0]
    assert any("from scratch" in f for f in r.fixes)


def test_without_ai_search_red_is_not_split():
    rows = load_catalog(ROOT / "sample_catalog.csv")
    for r in check_catalog(rows, MockClient()):
        assert r.red_case == "" and r.suggested_match is None


ROW = CatalogRow("Blue Hour", "The Lowlights", "QZNONE", ["Jide Okafor"])


def test_unsure_match_is_ignored_so_song_is_not_found():
    mlc = FakeMLC([song("X1")])
    mlc.search_recordings = lambda isrc: []
    r = check_row(ROW, mlc, work_searcher=FakeSearcher(verdict=WorkVerdict(True, 0.7, "maybe")))
    assert (r.status, r.red_case, r.suggested_match) == (RED, NOT_FOUND, None)


def test_incomplete_search_never_claims_not_found():
    mlc = FakeMLC()
    mlc.search_recordings = lambda isrc: []
    r = check_row(ROW, mlc, work_searcher=FakeSearcher(queries=None))
    assert (r.status, r.red_case) == (RED, "")
    assert any("couldn't finish" in n for n in r.notes)


def test_several_confident_matches_pick_the_surest_and_flag_duplicates():
    class Picky(FakeSearcher):
        def judge(self, title, writers, c):
            return WorkVerdict(True, {"A1": 0.9, "B2": 0.97}[c.song_code], "")

    mlc = FakeMLC([song("A1"), song("B2")])
    mlc.search_recordings = lambda isrc: []
    r = check_row(ROW, mlc, work_searcher=Picky())
    assert r.suggested_match.song_code == "B2"
    assert [c.song_code for c in r.other_matches] == ["A1"]
    assert any("duplicates" in p for p in r.problems)


def test_missing_writer_song_gets_second_registration_suggestion_and_stays_yellow(ai_results):
    r = ai_results["Concrete Garden"]
    assert r.status == YELLOW and r.red_case == ""
    assert r.suggested_match.song_code == "CG9013"  # not CG9012, the one the ISRC already links to
    assert any("merge" in f for f in r.fixes)


def test_songs_without_problems_needing_search_are_never_searched():
    searcher = FakeSearcher()
    rows = load_catalog(ROOT / "sample_catalog.csv")
    results = {r.row.title: r for r in check_catalog(rows, MockClient(), work_searcher=searcher)}
    assert results["Late Night Drive"].status == GREEN
    assert results["Paper Crowns"].suggested_match is None  # yellow, but no missing writers
    # judged: only candidates for Concrete Garden, Ghost Signal and Blue Hour
    assert "LA1234" not in searcher.judged and "PB5678" not in searcher.judged


# ---- report + CLI ----------------------------------------------------------------

def test_html_shows_both_red_cases_and_please_confirm(ai_results):
    html = render_html(list(ai_results.values()), mock=True)
    assert '<span class="case">Registered but not linked to your recording</span>' in html
    assert '<span class="case">Not found anywhere</span>' in html
    assert "Possible match at The MLC, please confirm" in html
    assert "Possible second registration, please confirm" in html
    assert "<code>BH3456</code>" in html and "Jyde Okafor" in html


def test_html_escapes_suggested_titles():
    from retrack.checker import Result
    from retrack.work_search import Candidate
    r = Result(ROW, RED, red_case=NOT_LINKED,
               suggested_match=Candidate("X<1>", "<b>Hit</b>", "", ["<i>A</i>"], True, 0.9, "<script>"))
    html = render_html([r])
    assert "<b>Hit</b>" not in html and "&lt;b&gt;Hit&lt;/b&gt;" in html
    assert "<script>" not in html.split("<style>")[0] + html.split("</style>")[1]


def test_csv_has_red_case_and_suggestion_columns(ai_results, tmp_path):
    out = tmp_path / "r.csv"
    write_csv(list(ai_results.values()), out)
    rows = {r["title"]: r for r in csv.DictReader(out.open(encoding="utf-8"))}
    assert rows["Blue Hour"]["red_case"] == "Registered but not linked to your recording"
    assert rows["Blue Hour"]["suggested_song_code"] == "BH3456"
    assert rows["Ghost Signal"]["red_case"] == "Not found anywhere"


def test_cli_mock_ai_search_runs_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "r.csv"
    assert main([str(ROOT / "sample_catalog.csv"), "--mock", "--ai-search", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Registered but not linked to your recording" in printed
    assert "please confirm" in printed
