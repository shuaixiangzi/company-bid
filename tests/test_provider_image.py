import hashlib
import io
import json

from PIL import Image

from company_bid.diagnostics import inspect_diagnostics
from company_bid.provider import ChatConfig, ChatProposalProvider
from company_bid.runtime import Executor, NodeSpec


def test_local_image_uses_same_call_budget_trace_and_private_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_IMAGE_KEY", "test-image-active-canary")
    buffer = io.BytesIO(); Image.new("RGB", (50, 80), "white").save(buffer, format="PNG")
    data = buffer.getvalue()
    image = {"data": data, "media_type": "image/png", "sha256": hashlib.sha256(data).hexdigest()}
    calls = []
    def transport(endpoint, payload, secret, timeout):
        content = payload["messages"][1]["content"]
        assert json.loads(content[0]["text"])["page"] == 3
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        calls.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"page":3,"text":"read"}'}}]}
    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test", auth_env="TEST_IMAGE_KEY",
                                               max_run_usd=1, max_run_calls=3), transport=transport)
    def node(ctx, value):
        return ctx.run_unit("page-3", value, lambda: provider.generate_json(ctx, value, image=image, unit_id="page-3",
                           task_type="ocr", prompt_version="ocr-v1", system_prompt="Read image to JSON", validate=lambda x: x))
    executor = Executor(tmp_path, [NodeSpec("N1", "1", node)], {})
    rid = executor.create_run({"page": 3})
    assert executor.execute(rid)["status"] == "succeeded"
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 1 and report["calls"][0]["operation"] == "ocr"
    assert report["unknown_usage_attempts"] == 1
    assert "data:image" not in json.dumps(report)
    assert len(calls) == 1


def test_wrong_image_hash_is_rejected_before_http_or_budget_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_IMAGE_KEY", "test-image-active-canary")
    def transport(*args):
        raise AssertionError("no network attempt allowed")
    provider = ChatProposalProvider(ChatConfig(endpoint="https://example.invalid/chat", model="test", auth_env="TEST_IMAGE_KEY"), transport=transport)
    def node(ctx, value):
        return provider.generate_json(ctx, {}, image={"data": b"bad", "media_type": "image/png", "sha256": "0"*64}, unit_id="page-1",
                                      task_type="ocr", prompt_version="1", system_prompt="read JSON", validate=lambda x: x)
    executor = Executor(tmp_path, [NodeSpec("N1", "1", node)], {})
    rid = executor.create_run({})
    assert executor.execute(rid)["error_code"] == "PROVIDER_IMAGE_INVALID"
    report = inspect_diagnostics(tmp_path, rid)
    assert report["actual_call_attempts"] == 0 and report["call_reservations"] == {}
