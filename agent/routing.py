"""
agent/routing.py
────────────────
BackendRouter — maps "the model name Clyde sends" → (Backend, ModelProfile).

Loads `~/.clyde/backends.yaml` at construction. Holds exactly one
Backend instance per backend entry (shared across routes). Fast-path
resolve is a single dict get.

2026-04-16: Integrates with ProcessManager for lazy-loading. When a
route is resolved, the router ensures the backend's server process is
running (launching on demand if needed) and marks it active so the
idle reaper doesn't kill it.
"""
from __future__ import annotations

import logging
import os
import typing as t
from dataclasses import dataclass
from pathlib import Path

import yaml

from backends import Backend, make_backend
from model_profiles import ModelProfile
from model_profiles.registry import ModelProfileRegistry
from process_manager import get_process_manager

log = logging.getLogger("routing")


@dataclass(slots=True)
class Route:
    logical_name: str           # what Clyde sends (e.g. "clyde:latest")
    backend: Backend            # shared instance
    backend_id: str             # key in backends.yaml (for ProcessManager)
    backend_model_id: str       # what to send to the backend as `model`
    profile: ModelProfile       # resolved ModelProfile


class BackendRouter:
    def __init__(
        self,
        config_path: Path | None = None,
        profiles: ModelProfileRegistry | None = None,
    ) -> None:
        if config_path is None:
            config_path = Path(os.path.expanduser("~/.clyde/backends.yaml"))
        self.config_path = Path(config_path)
        self.profiles = profiles or ModelProfileRegistry()
        self._backends: dict[str, Backend] = {}
        self._routes: dict[str, Route] = {}
        self._default_logical_name: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.config_path.exists():
            log.error("backends.yaml not found at %s", self.config_path)
            return
        data = yaml.safe_load(self.config_path.read_text()) or {}
        self._default_logical_name = data.get("default")
        pm = get_process_manager()
        for bid, spec in (data.get("backends") or {}).items():
            try:
                self._backends[bid] = make_backend({"id": bid, **spec})
                # Register with ProcessManager if launch config exists
                launch_cfg = spec.get("launch")
                if launch_cfg:
                    pm.register(bid, spec.get("endpoint", ""), launch_cfg)
            except Exception as exc:
                log.error("failed to build backend %s: %r", bid, exc)
        for logical, route_spec in (data.get("routes") or {}).items():
            bid = route_spec.get("backend")
            backend = self._backends.get(bid)
            if not backend:
                log.error("route %s references unknown backend %s", logical, bid)
                continue
            profile = self.profiles.resolve(
                route_spec.get("profile") or logical,
                family_hint=route_spec.get("family"),
            )
            self._routes[logical] = Route(
                logical_name=logical,
                backend=backend,
                backend_id=bid,
                backend_model_id=route_spec.get("model") or logical,
                profile=profile,
            )
        log.info(
            "router loaded %d backends, %d routes; default=%s",
            len(self._backends), len(self._routes), self._default_logical_name,
        )

    # ─── public API ───

    def resolve(self, logical_name: str | None) -> Route:
        """Return the Route for logical_name, falling back to the default."""
        key = logical_name or self._default_logical_name
        if key and (route := self._routes.get(key)):
            return route
        if self._default_logical_name and (route := self._routes.get(self._default_logical_name)):
            log.warning("route %r not found; using default %r", logical_name, self._default_logical_name)
            return route
        raise RuntimeError(
            f"no route for {logical_name!r} and no default configured "
            f"(known: {list(self._routes)})"
        )

    async def resolve_and_ensure(self, logical_name: str | None) -> Route:
        """Resolve + ensure the backend server is running (lazy-load).

        This is the primary entry point for completions. It:
          1. Resolves the route (same as resolve())
          2. Ensures the backend's server process is up (launching if needed)
          3. Marks the backend as active (resets idle timer)
        """
        route = self.resolve(logical_name)
        pm = get_process_manager()
        if pm.is_managed(route.backend_id):
            ok = await pm.ensure_running(route.backend_id, model=route.backend_model_id)
            if not ok:
                log.error("failed to start backend %s for route %s",
                          route.backend_id, route.logical_name)
            pm.mark_active(route.backend_id)
        return route

    def all_routes(self) -> list[Route]:
        return list(self._routes.values())

    async def probe_all(self, timeout_s: float = 2.0) -> dict[str, dict]:
        """Best-effort reachability map for Clyde's debug panel."""
        out: dict[str, dict] = {}
        for bid, backend in self._backends.items():
            status = await backend.probe(timeout_s=timeout_s)
            out[bid] = {
                "kind": backend.kind,
                "endpoint": backend.endpoint,
                "reachable": status.reachable,
                "latency_ms": status.latency_ms,
                "loaded_model": status.loaded_model,
                "notes": status.notes,
            }
        return out

    def reload(self) -> None:
        """Hot-reload backends.yaml and model_profiles/*.yaml."""
        self.profiles.reload()
        self._backends.clear()
        self._routes.clear()
        self._load()
