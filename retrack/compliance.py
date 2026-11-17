"""Wording that must appear word for word in several places.

Kept in one file so the HTML report, the homepage and every fix pack file
always say exactly the same thing. Don't reword these in other files: import
them. (tests/test_fixes.py checks they're present everywhere.)
"""

# On the HTML report, the homepage and every file in a fix pack.
COMPLIANCE_TEXT = (
    "Retrack is not affiliated with or endorsed by The MLC. Data comes from The MLC’s "
    "Public Search API and may not reflect actual copyright ownership. You are responsible "
    "for reviewing all data before uploading."
)

# What Retrack never does. Said in every fix pack, so nobody is surprised.
NEVER_TEXT = (
    "Retrack never logs into The MLC Portal, never asks for your MLC Portal password, "
    "and never uploads anything on your behalf. You upload these files yourself."
)

# What the user must confirm before any fix pack file is written.
ATTESTATION_TEXT = (
    "I have reviewed the data Retrack will put in this fix pack, and I control the "
    "shares being registered or claimed (or am authorized by whoever does)."
)
