from pathlib import Path

from company_bid.pipeline import build_executor
from company_bid.sample import create_synthetic_sample


def test_prepared_slice_freezes_sources_resumes_and_forks(tmp_path):
    sample = create_synthetic_sample(tmp_path / "inputs")
    initial = {"prepared": sample.model_dump(mode="json"), "proposals": {"S2": {
        "paragraphs": ["拟在作业前核对许可与隔离状态，按测点编号记录基线与实测值；发现异常后保持原始记录，先核对接线与仪器状态，再独立复测，形成异常清单和成果交接记录。"],
        "steps": ["拟建立测点编号与位置对照表。", "拟逐点记录测量条件与异常处理结果。"]}}}
    root = tmp_path / "runtime"
    config = {"provider": {"mode": "prepared"}, "debug_capture": True}
    runner = build_executor(root, config)
    run_id = runner.create_run(initial)
    paused = runner.execute(run_id, stop_after="N4")
    assert paused["status"] == "paused_debug"
    frozen = runner.output(run_id, "N0")
    assert len(frozen["source_artifacts"]) == 3  # proof source and attachment share bytes
    assert frozen["prepared"]["sources"][2]["path"] == frozen["prepared"]["attachments"][0]["path"]
    source = sample.sources[0].path
    source.write_text("changed outside source", encoding="utf-8")
    # N0 already copied and froze bytes; later nodes use that immutable copy.
    result = build_executor(root, config).resume(run_id, stop_after="N6")
    assert result["status"] == "succeeded"
    original = runner.output(run_id)
    old_bytes = (root / original["files"]["bid_docx"]["path"]).read_bytes()
    child_id = runner.fork(run_id, "N6")
    child = runner.execute(child_id, stop_after="N6")
    assert child["status"] == "succeeded"
    assert (root / original["files"]["bid_docx"]["path"]).read_bytes() == old_bytes
    assert runner.output(child_id)["files"]["bid_docx"]["path"] != original["files"]["bid_docx"]["path"]
