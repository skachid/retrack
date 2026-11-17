"""What leaves this computer, for a given set of scan settings.

data_flows() is the single source of truth. Every privacy sentence in the web
UI is built from it, and tests/test_web.py runs real scans with each toggle
combination and checks that what is actually sent matches it exactly. So if
someone changes what a check sends, that test fails until this file (and so
the copy) is updated too.
"""

from dataclasses import dataclass

# What each kind of request carries, in the order we list them.
ISRCS, SONG_CODES, TITLES, WRITER_NAMES = "ISRCs", "MLC song codes", "song titles", "writer names"
ORDER = (ISRCS, SONG_CODES, TITLES, WRITER_NAMES)


@dataclass(frozen=True)
class Flows:
    mlc: frozenset     # what goes to The MLC's Public Search API
    claude: frozenset  # what goes to the Claude API


def data_flows(smart_names=False, ai_search=False, mock=False):
    """What a scan with these settings sends, and to whom.

    - Every scan: ISRCs (POST /search/recordings) and MLC song codes (POST /works).
    - AI search: song titles and writer names to The MLC's song search
      (POST /search/songcode), and to Claude (variants, then "same song?").
    - Smart names: pairs of writer names to Claude ("same person?").
    - Sample data mode (--mock): nothing leaves the computer at all.
    """
    if mock:
        return Flows(frozenset(), frozenset())
    mlc, claude = {ISRCS, SONG_CODES}, set()
    if ai_search:
        mlc |= {TITLES, WRITER_NAMES}
        claude |= {TITLES, WRITER_NAMES}
    if smart_names:
        claude |= {WRITER_NAMES}
    return Flows(frozenset(mlc), frozenset(claude))


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
        claude = ("Nothing goes to Claude.", "Sample data mode uses offline stand-ins for the "
                                             "extra checks.")
    else:
        mlc = (f"To The MLC: {_listed(flows.mlc)}.",
               "Only those, through its Public Search API. Never the whole file.")
        if flows.claude:
            claude = (f"To Claude: {_listed(flows.claude)}.",
                      "Only for the extra checks you turned on. No ISRCs, no audio, no money figures.")
        else:
            claude = ("Nothing goes to Claude.", "The extra checks are off.")
    return [KEPT, mlc, claude, NO_AUDIO]


def privacy_text(smart_names=False, ai_search=False, mock=False):
    """The same, as one string (for tests and plain-text use)."""
    return " ".join(f"{h} {s}" for h, s in privacy_items(smart_names, ai_search, mock))
