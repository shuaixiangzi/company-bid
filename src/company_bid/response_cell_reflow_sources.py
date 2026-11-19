"""Scope a reviewed attachment label to a single native PDF table cell."""
from __future__ import annotations

import re


RESPONSE_CELL_REFLOW_CONFIG_KEY = "response_cell_reflow_source_resolution"
RESPONSE_CELL_REFLOW_RULE = "native_pdf_reviewed_cell_lf_relocation_v1"
_HASHES = {"source_sha256", "raw_candidate_sha256", "source_proof_sha256"}
_IDENTITIES = {"source_id", "window_id", "block_id"}
_STRINGS = _HASHES | _IDENTITIES | {"locator"}
_POSITIONS = {"page", "row", "column", "candidate_index", "label_index"}


def normalize_response_cell_reflow_config(value):
    if (not isinstance(value, dict) or set(value) != _STRINGS | _POSITIONS or
            any(not isinstance(value[key], str) or not 1 <= len(value[key]) <= 120 for key in _STRINGS) or
            any(not re.fullmatch(r"[a-f0-9]{64}", value[key]) or value[key] == "0" * 64 for key in _HASHES) or
            any(type(value[key]) is not int or value[key] < 0 for key in _POSITIONS) or value["page"] < 1 or
            not re.fullmatch(rf"pdf/page\[{value['page']}\]/table\[\d+\]", value["locator"])):
        raise ValueError("RESPONSE_CELL_REFLOW_CONFIGURATION_INVALID")
    return dict(value)


def reflow_lf_offsets(text: str) -> list[int] | None:
    """Return LF positions in the contiguous character stream of one cell."""
    if "\r" in text or "\t" in text:
        return None
    positions = []
    for index, char in enumerate(text):
        if char == "\n":
            if not 0 < index < len(text) - 1 or not (text[index - 1].isalnum() and text[index + 1].isalnum()):
                return None
            positions.append(index - len(positions))
    return positions
