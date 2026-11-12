"""The production executor preserves the prescribed cover across restart and publication."""
from docx import Document

from company_bid.diagnostics import inspect_diagnostics
from company_bid.pipeline import build_executor
from company_bid.runtime import call_budget_summary
from test_prescribed_cover_rendering import COVER, prepared_data


def test_prescribed_cover_survives_n4_restart_and_final_single_step(tmp_path):
    data = prepared_data(tmp_path)
    config = {"debug_capture": True, "provider": {"mode": "prepared"}}
    root = tmp_path / "runtime"
    first = build_executor(root, config)
    run = first.create_run({"prepared": data, "proposals": {}})
    state = first.execute(run, stop_after="N4")
    assert state["status"] == "paused_debug"
    n4 = next(node for node in state["nodes"] if node["key"] == "N4")
    package = first._checkpoint_data(n4)["package"]
    assert package["prescribed_cover_section_id"] == "COVER"
    cover = next(section for section in package["sections"] if section["section_id"] == "COVER")
    assert [block["text"] for block in cover["blocks"] if block["kind"] == "paragraph"] == COVER[1:]
    frozen_n4 = n4["output_ref"]

    restarted = build_executor(root, config)
    finished = restarted.resume(run, stop_after="N6")
    assert finished["status"] == "succeeded"
    assert next(node for node in finished["nodes"] if node["key"] == "N4")["output_ref"] == frozen_n4
    files = restarted.output(run)["files"]
    text = [p.text for p in Document(root / files["bid_docx"]["path"]).paragraphs]
    assert text[:5] == COVER and text.count("响应文件") == 1 and "投 标 文 件" not in text
    assert "合成测试样本 仅用于系统验证 不可用于投标" in text
    for i in range(1, 5):
        assert "\n".join(text).count(f"【待补 M-{i:03d}】") == 1
    diagnostics = inspect_diagnostics(root, run)
    assert diagnostics["gaps"] == [] and diagnostics["actual_call_attempts"] == 0
    assert call_budget_summary(finished.get("call_budget", {}))["effective_budget_charge_usd"] == "0"
