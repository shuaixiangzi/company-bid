"""Conservative template markers; never rewrite protected labels or units."""
from __future__ import annotations

import re


_EMPTY = r"(?:[_＿]{2,}|[（(]\s*[）)])"
_MARKER = re.compile(_EMPTY + r"|[（(](?:请?填(?:写|入)?[^（）()\n]{0,40}|(?:响应人|投标人|供应商|单位|公司|项目|工程)名称)[）)]")


def whole_cell_slot(text: str) -> bool:
    """Only a whole blank target may be replaced with one known fact value."""
    return not text.strip() or re.fullmatch(r"\s*" + _EMPTY + r"\s*", text) is not None


def has_fill_marker(text: str) -> bool:
    """Unknown composite slots remain original text plus an explicit Issue."""
    return _MARKER.search(text) is not None
