"""Reversible checklist projection, source scope and typography boundaries."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from docx.oxml.ns import qn

from company_bid.models import DocumentPackage
from company_bid.review_document import shared_reason_catalog, render_task_list, _excerpt_heading, _note_references
from company_bid.rendering import RenderingError
from test_review_document import review_package


def catalog():
    return {"requirements": {
        "R-A": SimpleNamespace(text="甲项完整要求。后续条件不能省略。", source_refs=["S-A"]),
        "R-B": SimpleNamespace(text="乙项完整要求。另一个来源。", source_refs=["S-B"]),
    }}


def row(identity, rid, note, *, header=True, ending="\n"):
    prefix = f"【原文{1 if rid == 'R-A' else 2:03d}｜要求】词典阶段："
    reason = ending.join(["本项原原因。", prefix + "条件仍待核。", note,
                          "【字段ID示例】机器值740 m³；原图740 m²，保留原响应，不填事实。"])
    return {"issue_id": identity, "original_issue": {"requirement_refs": [rid], "reason": reason},
            "display_reason": reason, "display_transformations": [
                {"rule": "material_requirement_header_source_id_v1", "original_text": prefix, "display_text": prefix,
                 "requirement_id": rid}] if header else []}


def test_exact_note_sharing_keeps_occurrence_order_different_sources_and_round_trip():
    note = "本条候选证明“合成材料”：未取得可用识别文字，需核对原图；不代表原文留空或资料缺失。"
    rows = [row("M-001", "R-A", note), row("M-002", "R-B", note, ending="\r\n")]
    before = deepcopy(rows)
    notes = shared_reason_catalog(rows, catalog())
    assert len(notes) == 1 and notes[0]["text"] == note
    assert [o["requirement_refs"] for o in notes[0]["occurrences"]] == [["R-A"], ["R-B"]]
    assert [o["source_refs"] for o in notes[0]["occurrences"]] == [["S-A"], ["S-B"]]
    for old, current in zip(before, rows):
        assert current["display_reason"] == old["display_reason"]
        assert current["original_issue"] == old["original_issue"]
        assert "".join(p["text"]+p["line_ending"] for p in current["reason_segments"]) == old["display_reason"]
        for part in current["reason_segments"]:
            assert old["display_reason"][part["start"]:part["end"]] == part["text"]
        assert current["rendered_reason"].index("词典阶段") < current["rendered_reason"].index("见核对说明001")
        assert current["rendered_reason"].index("见核对说明001") < current["rendered_reason"].index("机器值740 m³")
        assert "原图740 m²" in current["rendered_reason"]


@pytest.mark.parametrize("change", ["different_text", "no_header", "embedded", "foreign_header", "once"])
def test_unverified_or_different_notes_are_not_coalesced(change):
    note = "本条候选证明“合成”：仅所选区域已转录，未读取全页。"
    rows = [row("M-001", "R-A", note), row("M-002", "R-A", note)]
    if change == "different_text": rows[1]["display_reason"] = rows[1]["display_reason"].replace("未读取全页", "完整页仍待核")
    elif change == "no_header": rows[1]["display_transformations"] = []
    elif change == "embedded": rows[1]["display_reason"] = rows[1]["display_reason"].replace(note, "原文引用："+note)
    elif change == "foreign_header": rows[1]["display_reason"] = rows[1]["display_reason"].replace(note, "【R-FOREIGN｜其他要求】词典阶段：待核。\n"+note)
    else: rows = rows[:1]
    assert shared_reason_catalog(rows, catalog()) == []
    assert all(not r["shared_note_ids"] for r in rows)


def test_same_issue_duplicate_lines_do_not_qualify_as_cross_issue_sharing():
    note = "本条候选证明“合成”：仅所选区域已转录，未读取全页。"
    item = row("M-001", "R-A", note+"\n"+note)
    assert shared_reason_catalog([item], catalog()) == []


def test_identical_display_headers_do_not_merge_distinct_original_requirement_ids():
    note = "本条候选证明“合成”：仅所选区域已转录，未读取全页。"
    display_header = "【原文001｜相同摘要】词典阶段："
    originals = {rid: f"【{rid}｜相同摘要】词典阶段：" for rid in ["R-A", "R-B"]}
    original_reason = "\n".join([originals['R-A'],note,originals['R-B'],note])
    display_reason = "\n".join([display_header,note,display_header,note])
    item = {"issue_id":"M-001", "original_issue":{"reason":original_reason,"requirement_refs":["R-A","R-B"]},
            "display_reason":display_reason, "display_transformations":[
                {"rule":"material_requirement_header_source_id_v1", "original_text":prefix,
                 "display_text":display_header,"requirement_id":rid} for rid,prefix in originals.items()]}
    other = deepcopy(item); other["issue_id"]="M-002"
    rows=[item,other]; notes=shared_reason_catalog(rows,catalog())
    assert len(notes)==1
    for current in rows:
        occurrences=[p for p in current['reason_segments'] if p['kind']=='shared_note']
        assert [p['requirement_refs'] for p in occurrences]==[['R-A'],['R-B']]
        assert [p['source_refs'] for p in occurrences]==[['S-A'],['S-B']]
    assert [p['requirement_refs'] for p in notes[0]['occurrences']]==[['R-A'],['R-B'],['R-A'],['R-B']]


def test_scoped_notes_do_not_move_past_later_requirement_or_global_summary():
    note = "本条候选证明“合成”：未取得可用识别文字，需核对原图；不代表原文留空或资料缺失。"
    rows = [row("M-001", "R-A", note), row("M-002", "R-A", note)]
    for item in rows:
        item["display_reason"] += "\n原件读取：所选材料仍有未读项。\n"+note
    notes = shared_reason_catalog(rows, catalog())
    assert len(notes) == 2
    for item in rows:
        # The trailing candidate note has no authenticated requirement scope.
        assert item["reason_segments"][-1]["kind"] == "inline"
        assert item["rendered_reason"].endswith(note)


def test_complete_generated_page_failure_notes_share_but_machine_values_remain_inline():
    note = "候选证明“合成合同”｜原件第4页：转录与已审原图字段冲突（MATERIAL_OCR_NUMERIC_REFERENCE_MISMATCH）；读取失败状态保留，需核对原图或补齐可读证明。"
    rows = [row('M-001','R-A',note),row('M-002','R-B',note)]
    notes = shared_reason_catalog(rows,catalog())
    assert len(notes)==1 and notes[0]['text']==note
    assert [o['requirement_refs'] for o in notes[0]['occurrences']]==[['R-A'],['R-B']]
    for current in rows:
        assert note not in current['rendered_reason']
        assert '机器值740 m³；原图740 m²' in current['rendered_reason']
    # Similar prose without the registered complete failure suffix stays inline.
    note=note.replace('读取失败状态保留','来源说明')
    assert shared_reason_catalog([row('M-001','R-A',note),row('M-002','R-A',note)],catalog())==[]


def test_note_range_does_not_fill_gaps_sort_or_lose_original_order():
    assert _note_references(['核对说明001','核对说明002','核对说明003','核对说明005','核对说明002'])=='核对说明001～003、核对说明005'
    assert _note_references(['核对说明005','核对说明003','核对说明004'])=='核对说明005、核对说明003～004'


def test_real_renderer_preserves_originals_and_prints_shared_text_once(tmp_path):
    package = review_package(tmp_path)
    data = package.model_dump(mode="json")
    assert len(data["issues"]) >= 2
    note = "原件读取：合成样本未取得可用识别文字，原图与失败记录保持；这不是原文留空或资料缺失。"
    for item in data["issues"][:2]: item["reason"] += "\n"+note
    package = DocumentPackage.model_validate(data)
    before = deepcopy(package.model_dump(mode="json"))
    doc, index = render_task_list(package)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert text.count(note) == 1 and len(index["shared_reason_notes"]) == 1
    assert len(index["issues"]) == len(package.issues)
    assert [r["action_id"] for r in index["actions"]] == [a.action_id for a in package.review_actions]
    assert package.model_dump(mode="json") == before
    for item in package.issues:
        projected = next(r for r in index["issues"] if r["issue_id"] == item.issue_id)
        assert projected["original_issue"] == item.model_dump(mode="json")
        assert item.issue_id in text and projected["display_action"] in text
    paragraphs = [p for p in doc.paragraphs if p.text.startswith("原因：") or p.text == note]
    for p in paragraphs:
        assert p.paragraph_format.line_spacing == package.style.body_line_spacing
        assert p.paragraph_format.space_after.pt == p.paragraph_format.space_before.pt == 0
        assert p.paragraph_format.widow_control is True
        assert p.paragraph_format.keep_together is True
        indent = p._p.find(qn("w:pPr")).find(qn("w:ind"))
        assert indent.get(qn("w:firstLineChars")) == "200"


def test_excerpt_marker_requires_a_strict_actual_source_prefix():
    c = catalog()
    assert _excerpt_heading("甲项完整要求。", ["R-A"], c).endswith("…（摘要，完整要求见原文附录）")
    assert _excerpt_heading("甲项完整要求。后续条件不能省略。", ["R-A"], c) == c["requirements"]["R-A"].text
    assert _excerpt_heading("自拟章节", ["R-A"], c) == "自拟章节"
    assert _excerpt_heading("甲项完整要求。", ["R-B"], c) == "甲项完整要求。"


def test_failed_native_source_validation_cannot_be_published_as_layout_pending(tmp_path, monkeypatch):
    monkeypatch.setattr("company_bid.source_format_rendering.recover_review_source_tables",
                        lambda *args: {"status": "failed", "reason": "source_hash_mismatch"})
    with pytest.raises(RenderingError, match="REVIEW_SOURCE_TABLE_SOURCE_VALIDATION_FAILED"):
        render_task_list(review_package(tmp_path))


def test_partial_quote_layout_marks_unrestored_fragment_without_losing_original(tmp_path, monkeypatch):
    def partial(text, *args):
        return {"status":"partially_restored", "reason":"aggregate_child_layout_unresolved",
                "proofs":[], "fragments":[{"kind":"paragraph","text":text,
                                            "layout_status":"unsupported","layout_reason":"scope_unresolved"}]}
    monkeypatch.setattr("company_bid.source_format_rendering.recover_review_source_tables",partial)
    package=review_package(tmp_path);doc,index=render_task_list(package)
    text="\n".join(p.text for p in doc.paragraphs)
    assert "以下片段的表格布局待核" in text
    for quote in index['source_quotes']:
        assert quote['text'] in text
        layout=next(row for row in index['source_quote_layout'] if row['display_id']==quote['display_id'])
        assert layout['status']=='partially_restored'
        assert layout['fragments'][0]['text']==quote['text']
