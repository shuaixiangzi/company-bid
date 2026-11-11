"""Integrity failures identify only safe metadata in controlled diagnostics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_bid.runtime import CheckpointInvalidError, Executor, NodeSpec, _error_diagnostic
from company_bid.storage import ArtifactIntegrityError, LocalStore


def stored(tmp_path):
    store = LocalStore(tmp_path)
    reference = store.put_bytes("a" * 32, "synthetic-source", b"original")
    return store, reference


@pytest.mark.parametrize("failure, reason", [
    ("missing", "missing"), ("unreadable", "unreadable"), ("resolve_unreadable", "unreadable"), ("invalid_ref", "invalid_ref"),
    ("size", "size_mismatch"), ("hash", "hash_mismatch"), ("json", "invalid_json"),
])
def test_integrity_reason_and_safe_identity_preserve_original_cause(tmp_path, monkeypatch, failure, reason):
    store, reference = stored(tmp_path)
    original_reference = dict(reference)
    underlying = None
    if failure == "missing":
        reference["path"] = "runs/" + "a" * 32 + "/artifacts/not-created"
    elif failure in ("unreadable", "resolve_unreadable"):
        target = store.resolve(reference["path"])
        underlying = PermissionError("SECRET_CANARY_PRIVATE_SOURCE", str(target))
        if failure == "resolve_unreadable":
            def denied_resolution(path):
                raise underlying
            monkeypatch.setattr(store, "resolve", denied_resolution)
        else:
            original_read = Path.read_bytes
            def denied(path):
                if path == target:
                    raise underlying
                return original_read(path)
            monkeypatch.setattr(Path, "read_bytes", denied)
    elif failure == "invalid_ref":
        reference["path"] = "../SECRET_CANARY_PRIVATE_SOURCE"
    elif failure == "size":
        reference["size_bytes"] += 1
    elif failure == "hash":
        reference["sha256"] = "0" * 64

    with pytest.raises(ArtifactIntegrityError) as caught:
        (store.read_json if failure == "json" else store.read_bytes)(reference)
    error = caught.value
    context = _error_diagnostic(error)["artifact_integrity"]
    assert error.code == "CHECKPOINT_INVALID"
    assert context == {"artifact_id": reference["artifact_id"], "reason": reason, "schema_path": None}
    encoded = json.dumps(_error_diagnostic(error))
    assert "SECRET_CANARY" not in encoded
    assert reference["path"] not in encoded and original_reference["sha256"] not in encoded
    if underlying is not None:
        assert error.__cause__ is underlying


@pytest.mark.parametrize("identifier", [None, 12, "SOURCE_CANARY_PRIVATE", "A" * 32, "a" * 31, "a" * 33])
def test_untrusted_artifact_identifiers_are_null_in_capture(tmp_path, identifier):
    store, reference = stored(tmp_path)
    reference.update(artifact_id=identifier, sha256="0" * 64)
    with pytest.raises(ArtifactIntegrityError) as caught:
        store.read_bytes(reference)
    assert _error_diagnostic(caught.value)["artifact_integrity"]["artifact_id"] is None
    assert "SOURCE_CANARY_PRIVATE" not in json.dumps(_error_diagnostic(caught.value))


def error_capture(executor, state, name="node-error"):
    captures = state["nodes"][0]["attempts"][-1]["captures"]
    return executor.store.read_json(next(item["ref"] for item in captures if item["name"] == name))


def test_multiple_references_name_the_failed_artifact_without_source_content(tmp_path):
    identities = []
    def handler(ctx, value):
        references = [ctx.store_bytes("proof", b"synthetic-one"), ctx.store_bytes("proof", b"synthetic-two")]
        identities.extend(item["artifact_id"] for item in references)
        references[1]["sha256"] = "0" * 64
        return {"source_artifacts": references}
    executor = Executor(tmp_path, [NodeSpec("LIBRARY", "1", handler)], {})
    run = executor.create_run({})
    state = executor.execute(run)
    assert state["status"] == "failed" and state["error_code"] == "CHECKPOINT_INVALID"
    assert state["cursor"] == 0 and "output_ref" not in state["nodes"][0]
    diagnostic = error_capture(executor, state)
    assert diagnostic["artifact_integrity"] == {
        "artifact_id": identities[1], "reason": "hash_mismatch", "schema_path": "$.source_artifacts[1]"}
    ordinary = "".join(path.read_text() for path in executor.store.run_dir(run).glob("*.jsonl"))
    assert "artifact_integrity" not in ordinary and "schema_path" not in ordinary
    assert "synthetic-one" not in ordinary and "synthetic-two" not in ordinary


@pytest.mark.parametrize("failure, reason", [("hash", "hash_mismatch"), ("redaction_collision", "invalid_ref")])
def test_changed_embedded_source_hash_is_locatable_before_output_capture(tmp_path, failure, reason):
    identity = None
    def handler(ctx, value):
        nonlocal identity
        reference = ctx.store_bytes("source", b"synthetic-original")
        identity = reference["artifact_id"]
        if failure == "hash":
            reference["sha256"] = "0" * 64
            return {"source": reference}
        # Reproduce a credential-shaped domain field corrupting repeated hashes
        # during output redaction, without using any real secret or material.
        return {"authorization_source_sha256": reference["sha256"], "source": reference}
    executor = Executor(tmp_path, [NodeSpec("LIBRARY", "1", handler)], {})
    state = executor.execute(executor.create_run({}))
    assert state["error_code"] == "CHECKPOINT_INVALID"
    assert error_capture(executor, state)["artifact_integrity"] == {
        "artifact_id": identity, "reason": reason, "schema_path": "$.source"}
    assert not any(item["name"] == "node-output" for item in state["nodes"][0]["attempts"][0]["captures"])


def test_dynamic_mapping_keys_are_omitted_and_wrapped_errors_keep_safe_context(tmp_path):
    store, reference = stored(tmp_path)
    reference["sha256"] = "0" * 64
    try:
        try:
            store.validate_embedded_artifacts({"source_artifacts": [{"SOURCE_CANARY_PRIVATE": reference}]})
        except ArtifactIntegrityError as error:
            raise CheckpointInvalidError("N1") from error
    except CheckpointInvalidError as error:
        diagnostic = _error_diagnostic(error)
    assert diagnostic["artifact_integrity"] == {
        "artifact_id": reference["artifact_id"], "reason": "hash_mismatch", "schema_path": None}
    assert "SOURCE_CANARY_PRIVATE" not in json.dumps(diagnostic)


def test_failed_integrity_attempt_does_not_discard_successful_unit(tmp_path):
    counts = {"good": 0, "bad": 0}
    def handler(ctx, value):
        def good():
            counts["good"] += 1
            return {"read": True}
        first = ctx.run_unit("good", {}, good)
        def bad():
            counts["bad"] += 1
            reference = ctx.store_bytes("synthetic", b"unchanged")
            if counts["bad"] == 1:
                reference["sha256"] = "0" * 64
            return {"artifact": reference}
        return {"first": first, "second": ctx.run_unit("bad", {}, bad)}
    executor = Executor(tmp_path, [NodeSpec("N1", "1", handler)], {})
    run = executor.create_run({})
    failed = executor.execute(run)
    good_ref = failed["nodes"][0]["units"]["good"]["output_ref"]
    assert failed["status"] == "failed" and failed["error_code"] == "CHECKPOINT_INVALID"
    assert error_capture(executor, failed, "unit-error")["artifact_integrity"]["schema_path"] == "$.artifact"
    resumed = executor.resume(run)
    assert resumed["status"] == "succeeded" and counts == {"good": 1, "bad": 2}
    assert resumed["nodes"][0]["units"]["good"]["output_ref"] == good_ref
