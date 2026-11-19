"""Supply the frozen N2-only inputs to the shared replay reader."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from .diagnostic_replay import DiagnosticReplay
from .extraction_illustrations import CONFIG_KEY
from .storage import digest


_window_inputs = ContextVar("replay_window_inputs", default=None)


@contextmanager
def _scope(regions, manifest):
    token = _window_inputs.set((regions, manifest) if regions is not None else None)
    try:
        yield
    finally:
        _window_inputs.reset(token)


def replay_window_inputs(config, manifest):
    """Legacy wire requests omit whole-document reviews. Only replay binds them here."""
    scope = _window_inputs.get()
    if manifest is not None or scope is None:
        return config, manifest
    regions, authenticated_manifest = scope
    return {**config, CONFIG_KEY: regions}, authenticated_manifest


class ExtractionDiagnosticReplay(DiagnosticReplay):
    def preflight(self, ctx, value):
        self._scope_caches = {}
        regions = ctx.execution_config.get("extraction", {}).get(CONFIG_KEY)
        with _scope(regions, value["manifest"]):
            return super().preflight(ctx, value)

    def _source_window(self, run, payload, unit_id):
        # The same N1 may have several N2 children with different reviews.
        # Authenticate a binding against that historical run's own decisions.
        regions = run.state["execution_config"].get("extraction", {}).get(CONFIG_KEY)
        manifest = self._n1(run)[0]["manifest"] if regions is not None else None
        with _scope(regions, manifest):
            self._source_windows = self._scope_caches.setdefault(digest(regions), {})
            return super()._source_window(run, payload, unit_id)
