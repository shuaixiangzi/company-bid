import pytest

from company_bid.project_answers import ProjectAnswerError, _payment_note, _qualify_deadline


def test_unlocated_deadline_accepts_only_the_question_sources_and_stays_pending():
    sources = {ref: {"kind": "tender", "provenance": "original"} for ref in ("related", "other")}
    question = {"status": "unresolved", "source_refs": ["related"], "value_source_refs": []}
    supplied = {"submitted_before": "2099-12-20T09:30:00+08:00", "source_ref": "related"}
    assert _qualify_deadline(question, supplied, sources) == (supplied, False)
    with pytest.raises(ProjectAnswerError, match="PROJECT_ANSWER_DEADLINE_SOURCE_OUT_OF_SCOPE"):
        _qualify_deadline(question, {**supplied, "source_ref": "other"}, sources)
    sources["related"]["provenance"] = "derived_from_bid"
    with pytest.raises(ProjectAnswerError, match="PROJECT_ANSWER_DEADLINE_SOURCE_INVALID"):
        _qualify_deadline(question, supplied, sources)


def test_located_deadline_accepts_equivalent_source_only_at_the_same_instant():
    sources = {ref: {"kind": "tender", "provenance": "original"} for ref in ("D1", "D2", "related")}
    known = {"submitted_before": "2026-12-20T09:30:00+08:00", "source_ref": "D1"}
    question = {"status": "known", "value": known, "value_source_refs": ["D1", "D2"],
                "source_refs": ["D1", "D2", "related"]}
    supplied = {"submitted_before": "2026-12-20T09:30+08:00", "source_ref": "D2"}
    assert _qualify_deadline(question, supplied, sources) == ({**known, "source_ref": "D2"}, True)
    for changed in ({**supplied, "source_ref": "related"},
                    {**supplied, "submitted_before": "2026-12-20T09:31+08:00"}):
        with pytest.raises(ProjectAnswerError, match="PROJECT_ANSWER_DEADLINE_SOURCE_CONFLICT"):
            _qualify_deadline(question, changed, sources)


@pytest.fixture
def payment_sources():
    register_hash, other_hash, original_payment_hash = (char * 64 for char in "abc")
    frozen_attachments = [
        {"attachment_id": "REG", "source_refs": ["register"], "sha256": register_hash, "title": "参保登记明细",
         "requirement_refs": ["SOC"]},
        {"attachment_id": "OTHER", "source_refs": ["other"], "sha256": other_hash, "title": "无关业绩附件",
         "requirement_refs": ["OTHER"]},
        {"attachment_id": "PAY", "source_refs": ["pay"], "sha256": original_payment_hash, "title": "缴费证明",
         "requirement_refs": ["SOC"]},
    ]
    sources = {ref: {"source_id": ref, "kind": "company_material", "provenance": "original", "sha256": "f" * 64}
               for ref in ("register", "other", "pay")}
    proof_refs = [{"kind": "reviewed_registration", "source_ref": "register", "attachment_id": "REG"},
                  {"kind": "attachment", "source_ref": "pay", "attachment_id": "PAY"}]
    candidates = {row["attachment_id"]: row for row in (frozen_attachments[0], frozen_attachments[2])}
    questions = {"payment_evidence": {"requirement_ids": ["SOC"], "evidence_refs": proof_refs,
                                      "conditions": {"window_days": 120}}}

    def check(proof_hash, *, trusted=True, effective_deadline=None, retained=True, direct_source=None):
        upload_ref = "F-PAYMENT-" + proof_hash
        source = {"source_id": upload_ref, "kind": "attachment", "provenance": "original", "sha256": proof_hash}
        plan = {"requirements": [], "attachments": frozen_attachments if retained else frozen_attachments[:2]}
        answers = {"manager_name": "测试甲", "employer": "测试公司",
                   "payment_evidence": {"person_name": "测试甲", "employer": "测试公司",
                                        "paid_on": "2026-11-20", "source_ref": direct_source or upload_ref}}
        return _payment_note(plan, questions, answers, {**sources, upload_ref: source}, candidates,
                             effective_deadline, frozen_attachments=frozen_attachments,
                             uploaded_payment_source_id=upload_ref if trusted and direct_source is None else None)

    return check


def test_renaming_frozen_payment_images_preserves_the_original_source_disposition(payment_sources):
    note, attached = payment_sources("a" * 64)
    assert attached is None and "参保登记页" in note
    with pytest.raises(ProjectAnswerError, match="PROJECT_ANSWER_PAYMENT_SOURCE_OUT_OF_SCOPE"):
        payment_sources("b" * 64)
    note, attached = payment_sources("c" * 64)
    assert attached is None and "实缴窗口仍待核" in note
    # A frozen input can itself have the new-upload source ID pattern.
    with pytest.raises(ProjectAnswerError, match="PROJECT_ANSWER_PAYMENT_SOURCE_OUT_OF_SCOPE"):
        payment_sources("d" * 64, trusted=False)


@pytest.mark.parametrize("direct_source", [None, "pay"])
def test_frozen_payment_removed_from_personnel_scope_is_reported_as_unadopted(payment_sources, direct_source):
    note, attached = payment_sources("c" * 64, retained=False, direct_source=direct_source)
    assert attached is None and "归属待核" in note and "未作为本次实缴附件采用" in note


def test_new_uploaded_image_remains_pending_even_when_manual_fields_match(payment_sources):
    note, attached = payment_sources("d" * 64)
    assert attached["sha256"] == "d" * 64
    assert "正式递交截止及实缴窗口仍待核" in note
    note, attached = payment_sources("d" * 64, effective_deadline={"submitted_before": "2026-12-20T09:30:00+08:00"})
    assert attached["sha256"] == "d" * 64
    assert "正式截止日 30 日" in note and "实际缴费和资格仍待核" in note
