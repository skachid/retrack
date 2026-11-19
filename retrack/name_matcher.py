"""Second-opinion name matching for writer credits.

checker.py first tries a cheap, local comparison (names_match). Only when that
fails does it ask a "name matcher" from this file whether two names are likely
the same person, e.g. "J. Okafor" vs "Jide Okafor".

There are two matchers with the same method, same_person(a, b):
  ClaudeNameMatcher - asks the Claude API (needs an Anthropic API key).
  MockNameMatcher   - simple offline rules, used with --mock and in tests.

Only the two writer names are sent to the API: no audio, no catalog file, no
money figures.
"""

import json
from dataclasses import dataclass

from .ai import MODEL  # one shared model for every Claude call

SYSTEM_PROMPT = """You compare two songwriter names from music publishing records and \
decide whether they are likely the same person.

Names on royalty registrations often differ because of initials ("J. Okafor"), \
missing middle names, nicknames ("Bob" / "Robert"), accents or transliteration, \
and first/last name order. Those differences can still be the same person.

Different surnames, or first names that clearly don't fit each other, are usually \
different people. When the evidence is thin, answer "same_person": false. A wrong \
"yes" hides a real registration gap from the user, which is worse than a wrong "no".

confidence is how sure you are of your answer, from 0 to 1."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "same_person": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_person", "confidence", "reason"],
    "additionalProperties": False,
}


@dataclass
class MatchVerdict:
    same_person: bool
    confidence: float  # 0..1, how sure the matcher is of its answer
    reason: str = ""


class ClaudeNameMatcher:
    def __init__(self, client=None, model=MODEL):
        if client is None:
            # Imported here so the rest of the tool (and --mock) works without
            # the anthropic package installed.
            import anthropic

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.client = client
        self.model = model
        self._cache = {}  # (name, name) -> MatchVerdict, so each pair costs one call

    def same_person(self, a, b):
        key = tuple(sorted((a, b)))
        if key not in self._cache:
            self._cache[key] = self._ask(a, b)
        return self._cache[key]

    def _ask(self, a, b):
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=512,  # the answer is a short JSON object
                # Structured output: the reply must match ANSWER_SCHEMA, so it always parses.
                output_config={"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f'Name A: "{a}"\nName B: "{b}"'}],
            )
        except Exception as e:  # network down, bad key, rate limit, AI limit reached...
            # Don't crash a whole catalog run over one name: treat it as "not matched",
            # so the writer still gets flagged for a human to look at.
            return MatchVerdict(False, 0.0, f"name check unavailable ({type(e).__name__})")

        if response.stop_reason != "end_turn":  # refused or cut off: no usable answer
            return MatchVerdict(False, 0.0, f"no answer (stop_reason={response.stop_reason})")

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        confidence = min(max(float(data["confidence"]), 0.0), 1.0)
        return MatchVerdict(bool(data["same_person"]), confidence, data["reason"])


class MockNameMatcher:
    """Offline stand-in for ClaudeNameMatcher. Rule: same last name, and the
    first names are equal or one is just the other's initial."""

    def same_person(self, a, b):
        from .checker import normalize_name  # local import avoids a circular import

        pa, pb = normalize_name(a).split(), normalize_name(b).split()
        if len(pa) < 2 or len(pb) < 2 or pa[-1] != pb[-1]:
            return MatchVerdict(False, 0.9, "different last names")
        fa, fb = pa[0], pb[0]
        if fa == fb or (min(len(fa), len(fb)) == 1 and fa[0] == fb[0]):
            return MatchVerdict(True, 0.9, "same last name, first name fits the initial")
        return MatchVerdict(False, 0.7, "same last name, different first names")
