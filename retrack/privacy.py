"""What leaves this computer, for a given set of scan settings.

The page copy says "an AI service" rather than naming Claude: users only need
to know that it's an outside AI and exactly what it receives.

data_flows() is the single source of truth. Every privacy sentence in the web
UI is built from it, and tests/test_web.py runs real visits (the free check,
then checkout) with each toggle combination and checks that what is actually
sent at each stage matches it exactly. So if
someone changes what a check sends, that test fails until this file (and so
the copy) is updated too.
"""

from dataclasses import dataclass

# What each kind of request carries, in the order we list them.
ISRCS, SONG_CODES, TITLES, WRITER_NAMES = "ISRCs", "MLC song codes", "song titles", "writer names"
ORDER = (ISRCS, SONG_CODES, TITLES, WRITER_NAMES)


@dataclass(frozen=True)
class Flows:
    mlc: frozenset          # what the free check sends to The MLC's Public Search API
    claude: frozenset       # what the free check sends to the AI service: always nothing now
    mlc_paid: frozenset     # sent to The MLC after checkout, for the paid songs only
    claude_paid: frozenset  # sent to the AI service after checkout, for the paid songs only

    @property
    def mlc_all(self):
        return self.mlc | self.mlc_paid

    @property
    def claude_all(self):
        return self.claude | self.claude_paid


def data_flows(smart_names=False, ai_search=False, mock=False):
    """What a visit with these settings sends, to whom, and when.

    - The free check: ISRCs (POST /search/recordings) and MLC song codes (POST /works)
      to The MLC. Nothing to the AI service, whatever the settings.
    - After checkout, for the paid songs only:
        AI search (red songs): song titles and writer names to The MLC's song search
        (POST /search/songcode), and to Claude (variants, then "same song?").
        Smart names (songs with a missing writer): pairs of writer names to Claude.
    - Sample data mode (--mock): nothing leaves the computer at all.
    """
    if mock:
        return Flows(frozenset(), frozenset(), frozenset(), frozenset())
    mlc_paid, claude_paid = set(), set()
    if ai_search:
        mlc_paid |= {TITLES, WRITER_NAMES}
        claude_paid |= {TITLES, WRITER_NAMES}
    if smart_names:
        claude_paid |= {WRITER_NAMES}
    return Flows(frozenset({ISRCS, SONG_CODES}), frozenset(), frozenset(mlc_paid), frozenset(claude_paid))


def _listed(kinds):
    names = [k for k in ORDER if k in kinds]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


KEPT = ("Kept on this computer, then deleted.",
        "Your file sits in a private folder on this computer while you work. It’s deleted when you "
        "press Delete my files now, or when Retrack stops.")
NO_AUDIO = ("No audio, no training.",
            "Retrack never touches your recordings and never learns from your catalog.")


def privacy_items(smart_names=False, ai_search=False, mock=False):
    """(heading, sentence) pairs describing exactly what these settings send."""
    flows = data_flows(smart_names, ai_search, mock)
    if mock:
        mlc = ("Nothing goes to The MLC.", "Sample data mode: every answer comes from Retrack’s "
                                           "built-in test data.")
        claude = ("Nothing goes to an AI service.", "Sample data mode uses offline stand-ins for the "
                                                    "extra checks.")
    else:
        mlc_sentence = "Only those, through its Public Search API. Never the whole file."
        if flows.mlc_paid:
            mlc_sentence += (f" After checkout, AI search also sends it the {_listed(flows.mlc_paid)} "
                             "of the songs you pay for.")
        mlc = (f"To The MLC: {_listed(flows.mlc)}.", mlc_sentence)
        if flows.claude_paid:
            claude = (f"To an AI service, after checkout: {_listed(flows.claude_paid)}.",
                      "Only for the songs you pay for, and only for the extra checks you turned on. The free "
                      "check sends nothing there. ISRCs, audio and money figures never go there.")
        else:
            claude = ("Nothing goes to an AI service.", "The extra checks are off.")
    return [KEPT, mlc, claude, NO_AUDIO]


def privacy_text(smart_names=False, ai_search=False, mock=False):
    """The same, as one string (for tests and plain-text use)."""
    return " ".join(f"{h} {s}" for h, s in privacy_items(smart_names, ai_search, mock))
