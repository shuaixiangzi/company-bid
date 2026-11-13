"""One local entry point for automatic bids, prepared fixtures and library jobs."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .pipeline import build_executor
from .provider import ProviderFailure
from .storage import StorageError, atomic_json


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def safe_summary(state):
    summary = {key: state.get(key) for key in ("run_id", "status", "parent_run_id", "fork_from_node", "cursor", "error_code", "artifact_scope", "observability", "call_budget") if key in state}
    if state.get("error_code"):
        summary["error_type"] = state.get("error_type")
    return summary


def parser():
    command = argparse.ArgumentParser(description="Local full-chain/document/library executor; PostgreSQL Worker follows in M2")
    command.add_argument("--root", type=Path, default=Path(".local/runtime"))
    command.add_argument("--config", type=Path, help="non-secret execution config JSON; secrets only in environment")
    command.add_argument("--credential-file", type=Path, help="optional local untracked key file; never serialized into run config")
    actions = command.add_subparsers(dest="command", required=True)
    create = actions.add_parser("create")
    create.add_argument("--input", type=Path, required=True)
    create.add_argument("--proposals", type=Path)
    create.add_argument("--recorded-candidates", type=Path, help="explicit offline JSON candidates bound to request hashes")
    create.add_argument("--stop-after", choices=[*[f"N{i}" for i in range(7)], "LIBRARY"])
    create.add_argument("--queue-only", action="store_true")
    for action in ("resume", "step"):
        item = actions.add_parser(action)
        item.add_argument("run_id")
        item.add_argument("--stop-after", choices=[*[f"N{i}" for i in range(7)], "LIBRARY"], required=action == "step")
    fork = actions.add_parser("rerun")
    fork.add_argument("run_id")
    fork.add_argument("--from-node", choices=[*[f"N{i}" for i in range(7)], "LIBRARY"], required=True)
    fork.add_argument("--input", type=Path)
    fork.add_argument("--proposals", type=Path)
    fork.add_argument("--recorded-candidates", type=Path)
    for action in ("inspect", "result", "diagnostic-result", "repair", "diagnose"):
        actions.add_parser(action).add_argument("run_id")
    compare = actions.add_parser("diff")
    compare.add_argument("old_run")
    compare.add_argument("new_run")
    sample = actions.add_parser("sample")
    sample.add_argument("--directory", type=Path, default=Path(".local/synthetic"))
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "sample":
            from .sample import create_synthetic_sample
            sample = create_synthetic_sample(args.directory)
            atomic_json(args.directory / "prepared.json", sample.model_dump(mode="json"))
            print(json.dumps({"prepared": str(args.directory / "prepared.json"), "synthetic": True}))
            return 0
        config = read_json(args.config) if args.config else {"provider": {"mode": "prepared"}, "debug_capture": True}
        if args.command == "diagnose":
            from .diagnostics import inspect_diagnostics
            report = inspect_diagnostics(args.root, args.run_id)
            target = args.root / "diagnostics" / f"{args.run_id}-{uuid.uuid4().hex}.json"
            atomic_json(target, report)
            print(json.dumps({"diagnostics": str(target.resolve()), "evidence_status": report["evidence_status"],
                              "gap_count": len(report["gaps"]), "actual_call_attempts": report["actual_call_attempts"]}))
            return 0
        if args.credential_file:
            auth_env = config.get("provider", {}).get("auth_env", "COMPANY_BID_API_KEY")
            if not auth_env.startswith("COMPANY_BID_"):
                raise ProviderFailure("LOCAL_CREDENTIAL_ENV_PREFIX_REQUIRED")
            secret = args.credential_file.read_text(encoding="utf-8").strip()
            if not secret or "\n" in secret or len(secret) > 512:
                raise ProviderFailure("LOCAL_CREDENTIAL_INVALID")
            os.environ[auth_env] = secret
        executor = build_executor(args.root, config)
        input_key = {"automatic": "manifest", "library": "library_input"}.get(config.get("pipeline"), "prepared")
        if args.command == "create":
            initial = {input_key: read_json(args.input),
                       "proposals": read_json(args.proposals) if args.proposals else {}}
            if args.recorded_candidates:
                initial["recorded_candidates"] = read_json(args.recorded_candidates)
            run_id = executor.create_run(initial)
            result = executor.inspect(run_id) if args.queue_only else executor.execute(run_id, stop_after=args.stop_after)
        elif args.command in ("resume", "step"):
            result = executor.resume(args.run_id, stop_after=args.stop_after)
        elif args.command == "rerun":
            initial = None
            if args.input or args.proposals or args.recorded_candidates:
                if args.from_node != ("LIBRARY" if input_key == "library_input" else "N0"):
                    raise ProviderFailure("CHANGED_PREPARED_INPUT_REQUIRES_N0")
                if not args.input:
                    raise ProviderFailure("PREPARED_INPUT_REQUIRED")
                initial = {input_key: read_json(args.input),
                           "proposals": read_json(args.proposals) if args.proposals else {}}
                if args.recorded_candidates:
                    initial["recorded_candidates"] = read_json(args.recorded_candidates)
            run_id = executor.fork(args.run_id, args.from_node, initial_input=initial, execution_config=config)
            result = executor.inspect(run_id)
        elif args.command == "repair":
            run_id = executor.repair(args.run_id)
            result = executor.inspect(run_id)
        elif args.command == "diff":
            # Detailed diff is controlled content, not ordinary terminal logs.
            report = executor.diff(args.old_run, args.new_run)
            target = args.root / "comparisons" / f"{args.old_run}-{args.new_run}.json"
            atomic_json(target, report)
            print(json.dumps({"comparison": str(target.resolve())}))
            return 0
        elif args.command in ("result", "diagnostic-result"):
            state = executor.inspect(args.run_id)
            if state["status"] != "succeeded":
                raise ProviderFailure("RUN_NOT_PUBLISHED")
            output = executor.output(args.run_id)
            diagnostic = executor.artifact_scope(args.run_id) == "diagnostic"
            if args.command == "result" and diagnostic:
                raise ProviderFailure("DIAGNOSTIC_RESULT_NOT_PUBLISHED")
            if args.command == "diagnostic-result":
                if (not diagnostic or output.get("publication_status") != "diagnostic_only" or "files" in output or
                        not isinstance(output.get("diagnostic_files"), dict)):
                    raise ProviderFailure("RUN_NOT_DIAGNOSTIC_RESULT")
                print(json.dumps({"run_id": args.run_id, "publication_status": "diagnostic_only",
                    "diagnostic_files": {k: {**v, "path": str(args.root.resolve() / v["path"])}
                                         for k, v in output["diagnostic_files"].items()},
                    "generation_mode": output["generation_mode"]}, ensure_ascii=False))
                return 0
            if state["execution_config"].get("pipeline") == "library":
                print(json.dumps({"run_id": args.run_id, "snapshot_id": output["snapshot_id"], "library_status": output["status"],
                                  "counts": output["counts"], "snapshot_path": str(args.root.resolve() / output["snapshot_artifact"]["path"])}, ensure_ascii=False))
                return 0
            print(json.dumps({"run_id": args.run_id, "files": {k: {**v, "path": str(args.root.resolve() / v["path"])} for k, v in output["files"].items()}, "generation_mode": output["generation_mode"]}, ensure_ascii=False))
            return 0
        else:
            result = executor.inspect(args.run_id)
        print(json.dumps(safe_summary(result), ensure_ascii=False))
        return 1 if result.get("status") == "failed" else 0
    except Exception as error:
        # Never echo source text, schema inputs, provider bodies or secrets.
        code = getattr(error, "error_code", getattr(error, "code", "COMMAND_FAILED"))
        print(json.dumps({"error_code": code, "error_type": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
