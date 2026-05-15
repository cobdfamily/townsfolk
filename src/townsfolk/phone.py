"""Phone-number normalisation.

The full /v1/lookup phone path:
  1. Parse the input string into E.164.
  2. If it isn't a Canadian number, return the Bowen
     Island fallback (sane default per the design
     discussion).
  3. Extract NPA + NXX (the area code + central
     office code) and hand them to the SQL lookup.

We don't pull in libphonenumber for v1 -- a tight
NANP regex covers the actual use case (Canadian phone
numbers) without the 8 MB dependency. If we ever
need international parsing this becomes a one-line
swap.

A "Canadian" number means area code in the
CANADIAN_NPAS set. The NANP-shared area codes are
nominally country-code +1 for both Canada and the
US; the only way to know it's Canadian is the area
code itself (and a few overlay codes can serve both
countries -- we accept the small false-positive
rate as a cost of not joining LIDB).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Canonical Canadian area codes per CNAC, as of
# 2026-05. Reviewed against the published assignment
# list; bump on each NANPA release.
CANADIAN_NPAS: frozenset[str] = frozenset(
    [
        "204", "226", "236", "249", "250", "263", "289",
        "306", "343", "354", "365", "367", "368", "382",
        "403", "416", "418", "428", "431", "437", "438",
        "450", "468", "474", "506", "514", "519", "548",
        "579", "581", "584", "587", "604", "613", "639",
        "647", "672", "683", "705", "709", "742", "753",
        "778", "780", "782", "807", "819", "825", "867",
        "873", "879", "902", "905",
    ],
)


# E.164 style: + then digits, or NANP plain digits.
_RE_DIGITS = re.compile(r"\D+")


@dataclass(frozen=True)
class ParsedPhone:
    e164: str
    npa: str
    nxx: str
    is_canadian: bool


def parse(raw: str) -> ParsedPhone | None:
    """Normalise raw input to an E.164 NANP number.
    Returns None if the input can't be parsed as a
    10- or 11-digit NANP number.
    """
    digits = _RE_DIGITS.sub("", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    npa = digits[:3]
    nxx = digits[3:6]
    return ParsedPhone(
        e164=f"+1{digits}",
        npa=npa,
        nxx=nxx,
        is_canadian=npa in CANADIAN_NPAS,
    )
