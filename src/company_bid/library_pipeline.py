"""Company-library preparation through the same durable local executor."""
from __future__ import annotations

from .runtime import Executor, NodeSpec


def build_library_executor(root, config, version):
    from .library import prepare_library

    def prepare(ctx, value):
        data = value["library_input"]
        return prepare_library(ctx, data["registered_sources"], data["parsed"], ctx.execution_config.get("library", {}))

    implementation = version("library_pipeline.py", "library.py", "material_records.py", "slots.py", "models.py", "runtime.py", "storage.py", "observability.py")
    return Executor(root, [NodeSpec("LIBRARY", implementation, prepare, config_keys=("library",))], config)
