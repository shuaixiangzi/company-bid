"""Real parser -> extraction gateway -> plan -> writing -> DOCX, offline HTTP."""
import json
from pathlib import Path

from docx import Document
from PIL import Image

from company_bid.diagnostics import inspect_diagnostics
from company_bid.models import file_sha256
from company_bid.pipeline import build_executor


def test_complete_chain_preserves_original_grid_and_trace_and_rerenders_without_calls(tmp_path, monkeypatch):
    tender = tmp_path / "tender.docx"
    document = Document()
    document.add_paragraph("一 公司基本情况表", "Heading 1")
    table = document.add_table(rows=2, cols=4)
    table.cell(0, 0).text = "响应人名称"
    table.cell(0, 1).merge(table.cell(0, 3))
    table.cell(1, 0).text = "注册地址"
    table.cell(1, 2).text = "邮政编码"
    document.add_paragraph("第二章 技术与递交要求", "Heading 1")
    document.add_paragraph("应编制检查、异常复测和记录移交方法。")
    document.add_paragraph("应提供营业执照复印件。")
    document.add_paragraph("响应文件须在截止时间前上传。")
    document.save(tender)
    library = tmp_path / "facts.json"
    library.write_text(json.dumps({"name": "虚构测试公司", "address": "虚构测试路1号"}, ensure_ascii=False), encoding="utf-8")
    license_path = tmp_path / "license.png"
    Image.new("RGB", (120, 80), "white").save(license_path)

    def source(source_id, path, kind):
        return {"source_id": source_id, "title": "合成资料", "path": str(path), "sha256": file_sha256(path),
                "locator": "document", "kind": kind, "provenance": "synthetic", "synthetic": True}
    manifest = {"project": {"project_id": "P", "name": "合成巡检", "source_refs": ["T"]},
                "company": {"company_id": "C", "name": "虚构测试公司", "source_refs": ["F"]},
                "sources": [source("T", tender, "tender"), source("F", library, "manual_input"), source("A", license_path, "attachment")],
                "source_roles": {"T": "tender", "F": "provenance_only", "A": "provenance_only"},
                "facts": [{"fact_id": "NAME", "label": "响应人名称", "value": "虚构测试公司", "source_refs": ["F"]},
                          {"fact_id": "ADDRESS", "label": "注册地址", "value": "虚构测试路1号", "source_refs": ["F"]}],
                "attachments": [{"attachment_id": "LICENSE", "section_id": "unassigned", "title": "营业执照复印件", "description": "合成营业执照图像",
                                 "path": str(license_path), "sha256": file_sha256(license_path), "source_refs": ["A"],
                                 "requirement_refs": ["unassigned"], "synthetic": True}],
                "synthetic": True, "preparation_note": "合成整链测试，不可投标。"}
    monkeypatch.setenv("TEST_CHAIN_API_KEY", "test-credential-canary")
    calls = []

    def transport(endpoint, payload, secret, timeout):
        data = json.loads(payload["messages"][1]["content"])
        calls.append(data.get("task_type", "writing"))
        if data.get("task_type") == "extract_requirements":
            blocks = data["window"]["blocks"]
            def cite(block):
                return {key: block[key] for key in ("source_id", "block_id", "locator")} | {"quote": block["text"]}
            heading = next(b for b in blocks if b["text"] == "一 公司基本情况表")
            grid = next(b for b in blocks if b["kind"] == "table")
            requirements = []
            for block in blocks:
                text = block["text"]
                if text.startswith("应编制"):
                    requirements.append({"citation": cite(block), "category": "technical", "response_mode": "writing", "format_status": "not_prescribed"})
                elif text.startswith("应提供"):
                    requirements.append({"citation": cite(block), "category": "qualification", "response_mode": "attachment", "format_status": "not_prescribed", "attachment_keywords": ["营业执照"]})
                elif text.startswith("响应文件须"):
                    requirements.append({"citation": cite(block), "category": "procedure", "response_mode": "procedure", "format_status": "not_prescribed"})
            result = {"reviewed_block_ids": [b["block_id"] for b in blocks], "requirements": requirements, "format_status": "specified",
                      "format_chapters": [{"title": heading["text"], "level": 1, "citation": cite(heading),
                        "content_block_ids": [heading["block_id"], grid["block_id"]], "requirement_indices": [], "status": "prescribed"}]}
        elif data.get("task_type") == "map_form_fields":
            result = {"grid_sha256": data["grid_sha256"], "fills": [
                {"row": 1, "column": 1, "fact_id": "ADDRESS", "label_row": 1,
                 "label_column": 0, "label": "注册地址"}
            ]}
        else:
            assert "task_type" not in data
            result = {"paragraphs": ["拟按对象编号检查并保存记录，异常在授权范围内复测，复核后移交。"], "steps": [],
                      "coverage": [{"item_index": i, "paragraph_indices": [0], "step_indices": []}
                                   for i in range(len(data["section"]["writing_task"]["must_cover"]))]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    import company_bid.provider as module
    original_init = module.ChatProposalProvider.__init__
    monkeypatch.setattr(module.ChatProposalProvider, "__init__", lambda self, config, **kwargs: original_init(self, config, transport=transport, sleep=lambda _: None))
    config = {"pipeline": "automatic", "debug_capture": True,
              "provider": {"mode": "chat_completions", "endpoint": "https://example.invalid/chat", "model": "test",
                           "auth_env": "TEST_CHAIN_API_KEY", "max_attempts": 1, "max_run_calls": 20, "max_run_usd": 1},
              "extraction": {"window_max_chars": 50000}, "planning": {"map_table_fields": True}}
    root = tmp_path / "runtime"
    executor = build_executor(root, config)
    rid = executor.create_run({"manifest": manifest})
    state = executor.execute(rid, stop_after="N3")
    assert state["status"] == "paused_debug", state.get("error_code")
    assert calls == ["extract_requirements", "map_form_fields"]
    # New executor instance exercises persisted node/unit boundaries.
    executor = build_executor(root, config)
    state = executor.resume(rid)
    assert state["status"] == "succeeded", state.get("error_code")
    assert calls == ["extract_requirements", "map_form_fields", "writing"]
    report = inspect_diagnostics(root, rid)
    assert report["evidence_status"] == "available" and report["actual_call_attempts"] == 3
    assert [n["node_key"] for n in report["nodes"]] == [f"N{i}" for i in range(7)]
    out = executor.output(rid)
    assert out["input_kind"] == "automatic"
    artifact = root / out["files"]["bid_docx"]["path"]
    original_hash = file_sha256(artifact)
    output_doc = Document(artifact)
    assert len(output_doc.tables) == 1 and len(output_doc.inline_shapes) == 1
    rendered_grid = output_doc.tables[0]
    assert len(rendered_grid.rows) == 2  # no invented extra header
    assert rendered_grid.cell(0, 1).text == "虚构测试公司"
    assert rendered_grid.cell(0, 1)._tc.grid_span == 3
    assert rendered_grid.cell(1, 1).text == "虚构测试路1号"
    assert "M-" in rendered_grid.cell(1, 3).text
    assert "响应文件须在截止时间前上传。" not in "\n".join(p.text for p in output_doc.paragraphs)
    child = executor.fork(rid, "N6")
    assert executor.resume(child)["status"] == "succeeded"
    assert len(calls) == 3 and file_sha256(artifact) == original_hash
    assert inspect_diagnostics(root, child)["actual_call_attempts"] == 0
