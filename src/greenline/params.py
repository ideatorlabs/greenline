"""Template parameter hygiene.

Meta rejects template variables that contain newlines or tabs, or more than three consecutive
spaces, and caps their length. These helpers make any value acceptable and fit a list of values
to the number of variables a template declares.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

PARAM_MAX_CHARS = 900
EMPTY_PARAM = "-"

_BREAKS = re.compile(r"[\r\n\t]+")
_WIDE_SPACE = re.compile(r" {4,}")
_VARIABLE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def clean_param(value: Any, max_chars: int = PARAM_MAX_CHARS) -> str:
    """One value made safe for a template variable.

    Line breaks and tabs become a space, runs of four or more spaces collapse to one, the result
    is trimmed and capped at ``max_chars``. An empty result becomes ``"-"`` because Meta refuses
    empty variables.
    """
    text = "" if value is None else str(value)
    text = _BREAKS.sub(" ", text)
    text = _WIDE_SPACE.sub(" ", text).strip()
    text = text[:max_chars].strip()
    return text or EMPTY_PARAM


def fit_params(values: Iterable[Any] | None, count: int, max_chars: int = PARAM_MAX_CHARS) -> list[str]:
    """Clean every value, then pad with ``"-"`` or truncate to exactly ``count`` items."""
    cleaned = [clean_param(v, max_chars) for v in list(values or [])[: max(count, 0)]]
    return cleaned + [EMPTY_PARAM] * (count - len(cleaned))


def count_variables(body: str) -> int:
    """How many distinct ``{{n}}`` variables a template body declares."""
    return len(set(_VARIABLE.findall(body or "")))
