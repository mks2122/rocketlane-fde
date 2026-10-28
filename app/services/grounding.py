"""Grounding: is a quoted piece of text really in the source? Pure.

Used for intake evidence (G4) and the tier verifier's quote (G5). Comparison ignores case,
punctuation and spacing, so "jane.doe@acme.com" matches "Jane.Doe@ACME.com," but a value
that isn't in the source never matches.
"""

from __future__ import annotations

import re
from typing import Final

MIN_QUOTE_CHARS: Final = 3
_NON_WORD = re.compile(r"[^\w]+")


def normalise(text: str) -> str:
    return _NON_WORD.sub(" ", text.casefold()).strip()


def is_grounded(quote: str, source: str) -> bool:
    needle = normalise(quote)
    return len(needle) >= MIN_QUOTE_CHARS and needle in normalise(source)
