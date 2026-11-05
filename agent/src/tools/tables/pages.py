"""Page-selection parsing shared by the table tool.

Deliberately separate from ``doc_reader_tool._parse_pages``: that one needs the
document's page count to clamp ranges, while this one only parses the *request*
and leaves clamping to the caller (which knows the page count). Keeping them
apart avoids a circular import between ``doc_reader_tool`` and this package.
"""

from __future__ import annotations

import re

_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def parse_page_selection(text: str) -> list[int] | None:
    """Parse ``"1-10,12"`` into sorted 1-based page numbers.

    Args:
        text: User-supplied selection. Word/LLM paste often uses en/em dashes,
            so those are normalised to ASCII hyphens first.

    Returns:
        Sorted unique 1-based page numbers, or ``None`` when ``text`` is empty
        (meaning "all pages").

    Raises:
        ValueError: on an inverted range or a token that is neither a number
            nor a range.
    """
    if not text or not text.strip():
        return None

    normalised = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    pages: list[int] = []
    for part in normalised.split(","):
        token = part.strip()
        if not token:
            continue
        match = _RANGE_RE.match(token)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                raise ValueError(f"inverted page range: {token!r}")
            if start < 1:
                raise ValueError(f"page numbers are 1-based: {token!r}")
            pages.extend(range(start, end + 1))
            continue
        if not token.isdigit():
            raise ValueError(f"invalid page selection: {token!r}")
        if int(token) < 1:
            raise ValueError(f"page numbers are 1-based: {token!r}")
        pages.append(int(token))

    return sorted(set(pages)) or None
