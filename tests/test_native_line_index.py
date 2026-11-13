"""Equivalence and workload bounds for unit-local native geometry indexing."""
import copy
import random

import pytest

from company_bid import extraction


def previous_edges(document, blocks):
    """Frozen pre-optimization algorithm for differential checks."""
    if document.get("format") != "pdf":
        return []
    edges = []
    for left, right in zip(blocks, blocks[1:]):
        box = left.get("bbox")
        if not extraction._valid_box(box):
            continue
        height = box[3] - box[1]
        peers = [b["bbox"][2] for b in blocks if b.get("unit_id") == left.get("unit_id") and
                 b.get("kind") == "paragraph" and b.get("origin") == "native" and extraction._valid_box(b.get("bbox")) and
                 abs(b["bbox"][0] - box[0]) <= height * 2]
        edge = extraction._native_pdf_line_edge(left, right, max(peers, default=box[2]),
                                                document["sha256"], document.get("format"))
        if edge:
            edges.append(edge)
    return edges


def source(pages=8, lines=24):
    return [{"block_id": f"b-{page}-{line}", "source_id": "s", "unit_id": f"p{page}",
             "page": page, "ordinal": (page - 1) * lines + line, "kind": "paragraph", "origin": "native",
             "locator": f"pdf/page[{page}]/line[{line}]", "text": "合成连续原生文本用于几何校验",
             "bbox": [50, line * 12, 500, line * 12 + 10]}
            for page in range(1, pages + 1) for line in range(lines)]


@pytest.mark.parametrize("seed", range(6))
def test_grouped_geometry_preserves_exact_edges_and_hashes(seed):
    blocks = source()
    rng = random.Random(seed)
    for block in blocks[24:]:  # Keep a known positive page as well as negatives.
        change = rng.randrange(9)
        if change == 0:
            block["kind"] = "table"
        elif change == 1:
            block["origin"] = "ocr"
        elif change == 2:
            block.pop("bbox")
        elif change == 3:
            block["bbox"] = [0, 0, float("nan"), 1]
        elif change == 4:
            block["bbox"][0] = 300  # Another column cannot lend its geometry.
        elif change == 5:
            block["bbox"][2] = 420.0
        elif change == 6:
            block["text"] += "。"
        elif change == 7:
            block["bbox"][3] += 4  # Per-left-line height remains significant.
    document = {"format": "pdf", "sha256": "a" * 64}
    expected = previous_edges(document, blocks)
    assert expected
    actual = extraction._native_pdf_line_edges(document, blocks)
    assert actual == expected


def test_geometry_box_validation_scales_with_lines_not_document_squared(monkeypatch):
    blocks = source(pages=20, lines=20)
    valid_box = extraction._valid_box
    calls = []

    def counted(box):
        calls.append(1)
        return valid_box(box)

    monkeypatch.setattr(extraction, "_valid_box", counted)
    result = extraction._native_pdf_line_edges({"format": "pdf", "sha256": "a" * 64}, blocks)
    assert len(result) == 20 * 19
    assert len(calls) <= 4 * len(blocks)


def test_geometry_keeps_page_boundary_and_non_pdf_exclusion():
    blocks = source(pages=2, lines=2)
    before = copy.deepcopy(blocks)
    result = extraction._native_pdf_line_edges({"format": "pdf", "sha256": "a" * 64}, blocks)
    assert [edge["blocks"] for edge in result] == [["b-1-0", "b-1-1"], ["b-2-0", "b-2-1"]]
    assert extraction._native_pdf_line_edges({"format": "docx"}, blocks) == []
    assert blocks == before
