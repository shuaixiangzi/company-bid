"""Authenticate offline recovery of a stopped numbered contract clause window."""

import re

from .contract_clause_resolution import RESOLUTION_KEY, WINDOW_CONTEXT_KEY, verify_contract_clause_predecessor
from .diagnostic_replay import _one, _require
from .extraction_failed_scoring_replay import (
    FAILED_NUMBERED_CONTRACT_CLAUSE_REPLAY, _allow_cold_candidate, _StoppedResponseReplay, _TASK,
)
from .provider import ProviderFailure
from .storage import digest


class FailedNumberedContractClauseReplay(_StoppedResponseReplay):
    replay_key = proof_key = FAILED_NUMBERED_CONTRACT_CLAUSE_REPLAY
    extra_option_keys = frozenset({"offline_proof"})
    config_error = "FAILED_NUMBERED_CONTRACT_CLAUSE_CONFIG_INVALID"
    source_error = "FAILED_NUMBERED_CONTRACT_CLAUSE_SOURCE_INVALID"
    proof_error = "FAILED_NUMBERED_CONTRACT_CLAUSE_PROOF_MISMATCH"
    cold_error = "FAILED_NUMBERED_CONTRACT_CLAUSE"
    historical_failure = "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    lineage_basis = "authenticated_numbered_contract_clause_source_resolution"

    def __init__(self, replay, ctx):
        super().__init__(replay, ctx)
        proof = ctx.execution_config["extraction"][self.replay_key]["offline_proof"]
        _require(isinstance(proof, dict) and
                 set(proof) == {"raw_candidate_sha256", "predecessor_result_sha256", "result_sha256", "index_sha256"} and
                 all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                     for value in proof.values()), self.config_error)
        self.offline_proof = dict(proof)

    @staticmethod
    def previous_gate(replay):
        return replay._failed_contract_insurance_acceptance

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(self.predecessor.unit_id in replay.windows and self.unit_id in replay.windows, self.config_error)
        start = order.index(self.predecessor.unit_id) + 1
        stop = order.index(self.unit_id) + 1
        actual = order[start:stop]
        _require(len(actual) >= 2 and all(replay.windows[unit]["blocks"] for unit in actual), self.config_error)
        return actual

    def read_failure(self, replay, binding, task, full_window):
        from .extraction import (
            PlanningError, _validate_normalized_window_result, extraction_window_payload, run_extraction_window,
        )

        unit_id = binding["unit_id"]
        unit, call, context = self.read_original_failure(replay, unit_id, task, full_window)
        raw = call["candidate"]
        proof = self.offline_proof
        _require(digest(raw) == proof["raw_candidate_sha256"], self.proof_error)
        window, window_config = self.rebuild_window(replay, full_window)

        previous_id = self.prefix[-2]
        previous_window = replay.windows[previous_id]
        previous_task = {**extraction_window_payload(previous_window, replay.window_config),
                         "task_type": "extract_requirements"}
        # Load the previous actual call before using its result, regardless of
        # the order in which the bindings were written.
        previous_entry = replay._load_entry(replay.config["bindings"][_TASK + previous_id],
                                            previous_task, previous_window)
        previous_call = _one(previous_entry["origin"]["calls"], self.source_error)
        _, previous_unit, _, previous_result, _ = self.run.unit("N2", previous_id)
        _require(previous_call.get("run_id") == self.run.run_id and
                 self.prior_checkpoint_hashes.get(previous_id) == previous_unit["output_hash"], self.source_error)
        _require(digest(previous_result) == proof["predecessor_result_sha256"], self.proof_error)

        try:
            def saved_response(_ctx, _task, _unit, _payload, validate):
                return validate(raw)

            result = run_extraction_window(None, window, window_config, saved_response,
                                           predecessor_result=previous_result)
            _validate_normalized_window_result(result, window, predecessor_result=previous_result)
        except PlanningError as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_" + self.proof_error) from error
        resolution = result.get(RESOLUTION_KEY)
        _require(isinstance(resolution, dict) and digest(result) == proof["result_sha256"] and
                 result["raw_model_candidate"] == raw and isinstance(context, dict) and
                 context.get("field") == "unresolved_block_ids" and
                 resolution["resolved_block_ids"] == context.get("block_ids") and
                 resolution["raw_candidate_sha256"] == proof["raw_candidate_sha256"] and
                 {item["index_sha256"] for item in resolution["items"]} == {proof["index_sha256"]} and
                 verify_contract_clause_predecessor(previous_result, resolution), self.proof_error)

        self.window = window
        entry = self.failure_entry(unit, call, context, task, resolution)
        entry["origin"]["calls"].append(previous_call)
        entry["origin"]["lineage_notes"][0].update(
            offline_proof=proof, authenticated_predecessor={"unit_id": previous_id,
                "checkpoint_sha256": previous_unit["output_hash"],
                "candidate_sha256": previous_entry["origin"]["candidate_sha256"]})
        return entry

    def rebuild_window(self, replay, full_window):
        from .extraction import (
            _index, _windows, build_contract_clause_indexes, extraction_window_config, extraction_window_payload,
        )

        value = replay.current
        blocks, _, documents = _index(value["parsed"], value["manifest"])
        window_config = extraction_window_config(replay._target_extraction)
        indexes = build_contract_clause_indexes(documents, blocks)
        windows, _ = _windows(documents, blocks, window_config, manifest=value["manifest"],
                              contract_clause_indexes=indexes)
        _require(tuple(window["window_id"] for window in windows) == tuple(replay.windows), self.proof_error)
        for window in windows:
            key = _TASK + window["window_id"]
            task = {**extraction_window_payload(window, window_config), "task_type": "extract_requirements"}
            _require(digest(task) == replay.current_requests[key], "REQUEST_MISMATCH")
        target = _one(window for window in windows if window["window_id"] == self.unit_id)
        _require(target.get(WINDOW_CONTEXT_KEY) and
                 digest({key: value for key, value in target.items() if key != WINDOW_CONTEXT_KEY}) ==
                 digest(full_window), self.proof_error)
        return target, window_config

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()

    def allow_candidate(self, replay, ctx, task_type, unit_id):
        _allow_cold_candidate(self, replay, ctx, task_type, unit_id, self.enriched_windows(), self.cold_error,
                              self.prior_checkpoint_hashes, verify_cold_identity=True,
                              terminal_result_hash=self.offline_proof["result_sha256"])
