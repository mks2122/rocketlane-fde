"""Wrapping untrusted text (email bodies, call transcripts) before it goes into a prompt.

The prompt tells the model that everything between the markers is data, never
instructions. Markers inside the text are defused so it can't close the block early.
"""

from __future__ import annotations

import re
from typing import Final

_MARKER = re.compile(r"<<<\s*(?:BEGIN|END)\s+UNTRUSTED[^>]*>>>", re.IGNORECASE)
REDACTED_MARKER: Final = "[marker removed]"


def wrap_untrusted(label: str, text: str) -> str:
    safe = _MARKER.sub(REDACTED_MARKER, text)
    return f"<<<BEGIN UNTRUSTED {label}>>>\n{safe}\n<<<END UNTRUSTED {label}>>>"
