"""Defense-in-depth output sanitizer for the team_duncan_contacts plugin.

Primary protection is structural: raw handles are never stored or returned.
This module is secondary: it scans output values for phone-like patterns
and redacts them before they can reach agent-visible surfaces.
"""

from __future__ import annotations

import re
from typing import Any

# US phone patterns: (123) 456-7890, 123-456-7890, +1 123 456 7890, etc.
# Also catches international 10+ digit sequences with common formatting chars.
_PHONE_RE = re.compile(
    r"""
    (?:
        \+?1[\s\-\.]?          # optional US country code
    )?
    (?:
        \(?\d{3}\)?            # area code, optional parens
        [\s\-\.]               # separator
        \d{3}                  # prefix
        [\s\-\.]               # separator
        \d{4}                  # line number
    )
    |
    (?:\+?\d[\d\s\-\.\(\)]{8,}\d)   # generic 10+ digit formatted string
    """,
    re.VERBOSE,
)

_REDACTED = "[PHONE REDACTED]"


def sanitize_output(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace phone-like values with a redaction marker.

    Only strings are pattern-matched; numeric types, booleans, and None pass
    through unchanged.  Recursion depth is capped at 20 to prevent pathological
    inputs from causing a stack overflow.
    """
    if _depth > 20:
        return value
    if isinstance(value, str):
        return _PHONE_RE.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {k: sanitize_output(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        sanitized = [sanitize_output(item, _depth=_depth + 1) for item in value]
        return type(value)(sanitized)
    return value


def contains_phone_like(text: str) -> bool:
    """Return True if *text* contains a phone-like sequence."""
    return bool(_PHONE_RE.search(text))
