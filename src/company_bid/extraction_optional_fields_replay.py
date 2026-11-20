"""Authenticate recovery of a stopped native contract optional-field window."""

import re

from .contract_clause_resolution import WINDOW_CONTEXT_KEY as CLAUSE_CONTEXT_KEY
from .diagnostic_replay import _require
from .extraction_failed_scoring_replay import (
    FAILED_CONTRACT_OPTIONAL_FIELDS_REPLAY, _allow_cold_candidate, _StoppedResponseReplay, _TASK,
    _verify_replayed_checkpoint,
)
from .extraction_numbered_clause_replay import rebuild_frozen_contract_windows
from .provider import ProviderFailure
from .storage import digest


class FailedContractOptionalFieldsReplay(_StoppedResponseReplay):
    replay_key = proof_key = FAILED_CONTRACT_OPTIONAL_FIELDS_REPLAY
    extra_option_keys = frozenset({"offline_proof"})
    config_error = "FAILED_CONTRACT_OPTIONAL_FIELDS_CONFIG_INVALID"
    source_error = "FAILED_CONTRACT_OPTIONAL_FIELDS_SOURCE_INVALID"
    proof_error = "FAILED_CONTRACT_OPTIONAL_FIELDS_PROOF_MISMATCH"
    cold_error = "FAILED_CONTRACT_OPTIONAL_FIELDS"
    historical_failure = "LIVE_REQUIRED_BLOCK_UNRESOLVED"
    lineage_basis = "authenticated_native_contract_optional_field_resolution"

    def __init__(self, replay, ctx):
        super().__init__(replay, ctx)
        proof = ctx.execution_config["extraction"][self.replay_key]["offline_proof"]
        _require(isinstance(proof, dict) and
                 set(proof) == {"raw_candidate_sha256", "source_resolution_sha256", "result_sha256"} and
                 all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                     for value in proof.values()), self.config_error)
        self.offline_proof = dict(proof)

    @staticmethod
    def previous_gate(replay):
        return replay._failed_numbered_contract_clause

    def actual_units(self, replay):
        order = tuple(replay.windows)
        _require(self.predecessor.unit_id in replay.windows and self.unit_id in replay.windows, self.config_error)
        actual = order[order.index(self.predecessor.unit_id) + 1:order.index(self.unit_id) + 1]
        _require(len(actual) >= 2 and all(replay.windows[unit]["blocks"] for unit in actual), self.config_error)
        return actual

    def predecessor_windows(self):
        return self.predecessor.enriched_windows()

    def _verified_checkpoint(self, unit_id):
        _, unit, _, data, _ = self.run.unit("N2", unit_id)
        _require(unit["output_hash"] == self.prior_checkpoint_hashes.get(unit_id), self.source_error)
        return data

    def verify_success(self, replay, binding, entry, full_window):
        unit_id = binding["unit_id"]
        if unit_id != self.predecessor.unit_id:
            return super().verify_success(replay, binding, entry, full_window)
        # The prior stopped window was recovered with evidence from its own
        # preceding unit. Authenticate that result again in its sealed child.
        predecessor = self._verified_checkpoint(self.predecessor.prefix[-2])
        self.prior_checkpoint_hashes[unit_id] = _verify_replayed_checkpoint(
            self, replay, unit_id, entry, self.predecessor.window, self.source_error,
            predecessor_result=predecessor)

    def read_failure(self, replay, binding, task, full_window):
        from .contract_optional_fields import RESOLUTION_KEY, WINDOW_CONTEXT_KEY
        from .extraction import (
            PlanningError, _validate_normalized_window_result, extraction_window_payload, run_extraction_window,
        )

        unit, call, context = self.read_original_failure(replay, binding["unit_id"], task, full_window)
        raw = call["candidate"]
        proof = self.offline_proof
        _require(digest(raw) == proof["raw_candidate_sha256"], self.proof_error)
        windows, window_config = rebuild_frozen_contract_windows(replay, self.proof_error)
        window = windows[self.unit_id]
        _require(window.get(WINDOW_CONTEXT_KEY) and
                 {key: value for key, value in window.items() if key not in {WINDOW_CONTEXT_KEY, CLAUSE_CONTEXT_KEY}} ==
                 full_window, self.proof_error)

        previous_id = self.prefix[-2]
        previous_window = replay.windows[previous_id]
        previous_task = {**extraction_window_payload(previous_window, replay.window_config),
                         "task_type": "extract_requirements"}
        replay._load_entry(replay.config["bindings"][_TASK + previous_id], previous_task, previous_window)
        previous = self._verified_checkpoint(previous_id)

        def saved_response(_ctx, _task, _unit, _payload, validate):
            return validate(raw)

        try:
            result = run_extraction_window(None, window, window_config, saved_response, predecessor_result=previous)
            _validate_normalized_window_result(result, window, predecessor_result=previous)
        except PlanningError as error:
            raise ProviderFailure("DIAGNOSTIC_REPLAY_" + self.proof_error) from error
        resolution = result.get(RESOLUTION_KEY)
        _require(isinstance(resolution, dict) and digest(result) == proof["result_sha256"] and
                 digest(resolution) == proof["source_resolution_sha256"] and
                 result["raw_model_candidate"] == raw and isinstance(context, dict) and
                 context.get("field") == "unresolved_block_ids" and
                 resolution["resolved_block_ids"] == context.get("block_ids") and
                 resolution["raw_candidate_sha256"] == proof["raw_candidate_sha256"], self.proof_error)
        self.window = window
        entry = self.failure_entry(unit, call, context, task, resolution)
        entry["origin"]["lineage_notes"][0]["offline_proof"] = proof
        return entry

    def allow_candidate(self, replay, ctx, task_type, unit_id):
        _allow_cold_candidate(self, replay, ctx, task_type, unit_id, self.enriched_windows(), self.cold_error,
                              self.prior_checkpoint_hashes, verify_cold_identity=True,
                              terminal_result_hash=self.offline_proof["result_sha256"])
