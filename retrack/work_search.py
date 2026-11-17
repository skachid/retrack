"""Work search: look for a song at The MLC by title and writers when the ISRC
didn't lead to it (opt-in with --ai-search).

Why this exists: a song can be registered at The MLC but never linked to the
recording's ISRC. Then the ISRC lookup in checker.py finds nothing, even though
the song is there, often under a slightly different title ("The Blue Hour"
instead of "Blue Hour") or writer spelling ("Jyde" instead of "Jide").

How one search goes (search_for_work below):
  1. Search The MLC with the title and writers exactly as in the catalog.
  2. Ask a "work searcher" for a few variants of the title and writer names,
     and search those too. At most MAX_SEARCHES searches per song in total.
  3. Ask the searcher to judge each song that came back: is it the same song?
     How sure (0..1)?

This file only *suggests* matches. checker.py decides the verdict, using its
own confidence threshold, and labels every suggestion "please confirm".

There are two searchers with the same two methods, suggest_queries() and judge():
  ClaudeWorkSearcher - asks the Claude API (needs an Anthropic API key).
  MockWorkSearcher   - simple offline rules, used with --mock and in tests.

What is sent to the API: the song title, writer names, and the title and writer
names of each song The MLC returned. No audio, no ISRC, no artist, no money figures.
"""

import json
import re
from dataclasses import dataclass, field

from .mlc_client import MLCError
from .name_matcher import MODEL

# Keeps API costs sane: at most this many MLC searches per song (including
# the first, exact one), and at most this many results judged per song.
MAX_SEARCHES = 5
MAX_CANDIDATES = 5


@dataclass(frozen=True)
class SearchQuery:
    title: str
    writers: tuple = ()  # writer names; empty means "search by title only"


@dataclass
class Candidate:
    """One song The MLC returned, plus the searcher's opinion of it."""
    song_code: str
    title: str
    iswc: str
    writers: list
    same_song: bool = False
    confidence: float = 0.0  # 0..1, how sure the searcher is of its answer
    reason: str = ""


@dataclass
class SearchOutcome:
    queries: list = field(default_factory=list)     # the searches that were run
    candidates: list = field(default_factory=list)  # judged Candidates
    complete: bool = True  # False if a search or the AI failed, so "found nothing" isn't proven


@dataclass
class WorkVerdict:
    same_song: bool
    confidence: float
    reason: str = ""


# ---- The search itself (same for both searchers) --------------------------------

def _writer_names(song):
    names = []
    for w in song.get("writers") or []:
        full = f"{w.get('writerFirstName') or ''} {w.get('writerLastName') or ''}".strip()
        if full:
            names.append(full)
    return names


def search_for_work(title, writers, mlc_client, searcher, exclude_codes=()):
    """Run up to MAX_SEARCHES searches for one song and judge what comes back.

    Both the MLC client and the searcher are passed in rather than created here
    (dependency injection), so tests can hand in fakes.
    exclude_codes: song codes we already know about (e.g. the one the ISRC is
    linked to), which shouldn't come back as "new" suggestions.
    """
    outcome = SearchOutcome()

    queries = [SearchQuery(title, tuple(writers))]
    suggested = searcher.suggest_queries(title, writers)
    if suggested is None:  # the AI couldn't answer: we only tried the exact search
        outcome.complete = False
    for q in suggested or []:
        if q not in queries and q.title.strip():
            queries.append(q)
    queries = queries[:MAX_SEARCHES]

    found = {}  # song code -> song, so a song found by two searches is judged once
    for q in queries:
        try:
            songs = mlc_client.search_songcode(q.title, list(q.writers))
        except MLCError:
            outcome.complete = False
            continue
        outcome.queries.append(q)
        for song in songs:
            code = song.get("mlcSongCode")
            if code and code not in exclude_codes and code not in found:
                found[code] = song

    for code, song in list(found.items())[:MAX_CANDIDATES]:
        c = Candidate(code, song.get("workTitle") or "", song.get("iswc") or "", _writer_names(song))
        verdict = searcher.judge(title, writers, c)
        c.same_song, c.confidence, c.reason = verdict.same_song, verdict.confidence, verdict.reason
        outcome.candidates.append(c)
    return outcome


# ---- Claude version -------------------------------------------------------------

VARIANTS_PROMPT = """You help find a song's registration in The MLC's database, \
which pays US mechanical royalties. A search by exact title and writer names found \
nothing, or found a record missing some writers. Suggest other searches that a \
registration of the same song might be filed under.

Registrations often differ from the release by: a leading "The"/"A" added or \
dropped, "&" vs "and", punctuation and apostrophes, "(feat. ...)" or "(Remix)" tags, \
parenthetical subtitles, typos and alternate spellings, accents, and writers \
entered with initials, only a last name, or a slightly different spelling.

Rules:
- Only rework the names you are given. Never invent new writers or guess legal names.
- A search with an empty writers list searches by title only; include one if it helps.
- Give at most 4 searches, most likely first. Don't repeat the exact original."""

VARIANTS_SCHEMA = {
    "type": "object",
    "properties": {
        "searches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "writers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "writers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["searches"],
    "additionalProperties": False,
}

JUDGE_PROMPT = """You compare a song from an indie catalog with a song record from \
The MLC's database and decide whether they are the same composition.

Small differences are normal: a leading "The", punctuation, feat./remix tags, \
initials, misspelled or partial writer names. Those can still be the same song.

A matching title alone is NOT enough: many different songs share titles. If the \
writers don't overlap at all, they are almost always different songs. When the \
evidence is thin, answer "same_song": false. A wrong "yes" could lead the user to \
claim someone else's song, which is worse than a wrong "no".

confidence is how sure you are of your answer, from 0 to 1."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "same_song": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_song", "confidence", "reason"],
    "additionalProperties": False,
}


def _names(writers):
    return "; ".join(writers) if writers else "(none listed)"


class ClaudeWorkSearcher:
    def __init__(self, client=None, model=MODEL):
        if client is None:
            # Imported here so the rest of the tool (and --mock) works without
            # the anthropic package installed.
            import anthropic

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.client = client
        self.model = model
        # Caches, so asking about the same song twice costs one API call.
        self._query_cache = {}  # (title, writers) -> list of SearchQuery, or None
        self._judge_cache = {}  # (title, writers, song code) -> WorkVerdict

    def suggest_queries(self, title, writers):
        """Up to 4 alternative searches, or None if the AI couldn't answer."""
        key = (title, tuple(writers))
        if key not in self._query_cache:
            data = self._ask(VARIANTS_PROMPT, VARIANTS_SCHEMA,
                             f'Title: "{title}"\nWriters: {_names(writers)}')
            if data is None:
                self._query_cache[key] = None
            else:
                self._query_cache[key] = [
                    SearchQuery(s["title"].strip(), tuple(w.strip() for w in s["writers"] if w.strip()))
                    for s in data["searches"][:4]
                ]
        return self._query_cache[key]

    def judge(self, title, writers, candidate):
        key = (title, tuple(writers), candidate.song_code)
        if key not in self._judge_cache:
            data = self._ask(JUDGE_PROMPT, JUDGE_SCHEMA,
                             f'Catalog song: "{title}"\nCatalog writers: {_names(writers)}\n\n'
                             f'MLC record: "{candidate.title}"\nMLC writers: {_names(candidate.writers)}')
            if data is None:
                # Fail safe: no answer counts as "not the same song".
                self._judge_cache[key] = WorkVerdict(False, 0.0, "AI check unavailable")
            else:
                confidence = min(max(float(data["confidence"]), 0.0), 1.0)
                self._judge_cache[key] = WorkVerdict(bool(data["same_song"]), confidence, data["reason"])
        return self._judge_cache[key]

    def _ask(self, system, schema, text):
        """One structured-JSON question to Claude. Returns a dict, or None on any failure."""
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                output_config={
                    "effort": "low",  # short, well-defined questions don't need deep thinking
                    "format": {"type": "json_schema", "schema": schema},
                },
                # If the main model declines, let the API retry on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": text}],
            )
        except Exception:  # network down, bad key, rate limit...
            # Don't crash a whole catalog run over one song.
            return None
        if response.stop_reason != "end_turn":  # refused or cut off: no usable answer
            return None
        try:
            text_out = next(b.text for b in response.content if b.type == "text")
            return json.loads(text_out)
        except (StopIteration, ValueError):
            return None


# ---- Offline version ------------------------------------------------------------

def _plain_title(title):
    """'The Blue Hour (feat. X)' -> 'blue hour': the bits registrations disagree on, removed."""
    from .checker import normalize_name  # local import avoids a circular import

    t = re.sub(r"[(\[].*?[)\]]", " ", title)  # drop "(feat. X)", "[Remix]"
    t = normalize_name(t.replace("&", " and "))
    return re.sub(r"^(the|a|an) ", "", t)


def _last_name(name):
    from .checker import normalize_name

    parts = normalize_name(name).split()
    return parts[-1] if parts else ""


class MockWorkSearcher:
    """Offline stand-in for ClaudeWorkSearcher.

    Variants: add or drop a leading "The", drop (feat. ...) tags, use writers'
    last names only, and one title-only search.
    Judge: same title once those differences are ignored AND at least one
    writer last name in common.
    """

    def suggest_queries(self, title, writers):
        last_names = tuple(n for n in (_last_name(w) for w in writers) if n)
        base = re.sub(r"\s*[(\[].*?[)\]]", "", title).strip()
        if re.match(r"(?i)the ", base):
            other = base[4:]
        else:
            other = f"The {base}"
        queries = [SearchQuery(other, last_names), SearchQuery(base, last_names), SearchQuery(title, ())]
        return [q for q in queries if q != SearchQuery(title, tuple(writers))]

    def judge(self, title, writers, candidate):
        if _plain_title(title) != _plain_title(candidate.title):
            return WorkVerdict(False, 0.9, "different title")
        ours = {_last_name(w) for w in writers} - {""}
        theirs = {_last_name(w) for w in candidate.writers} - {""}
        if ours & theirs:
            return WorkVerdict(True, 0.9, "same title, and a writer's last name matches")
        return WorkVerdict(False, 0.8, "same title, but none of the writers match")
