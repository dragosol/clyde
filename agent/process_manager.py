"""
process_manager.py — Lazy-load backend servers with idle timeout
================================================================

Backends (mlx_lm.server, llama-server, etc.) eat tens of GB each.
This module ensures only the ACTIVE backend is loaded:

  1. On-demand launch:  ``ensure_running(backend_id)`` starts the server
     if it isn't already up. Called from routing before each completion.
  2. Activity tracking: ``mark_active(backend_id)`` timestamps last use.
  3. Idle reaper:       ``start_reaper()`` spawns an asyncio task that
     checks every 30s and kills any backend idle > IDLE_TIMEOUT_S.

Launch configuration comes from backends.yaml per backend:

    backends:
      mlx-local:
        type: mlx
        endpoint: http://127.0.0.1:8800
        launch:
          cmd: /opt/homebrew/bin/mlx_lm.server --model {model} --port 8800 ...
          idle_timeout: 600        # seconds, default 600 (10 min)
          log_file: /tmp/cm-mlx.log
          health_path: /v1/models  # endpoint to probe for readiness
          startup_timeout: 120     # seconds to wait for server ready

Backends without a ``launch`` block are treated as externally managed
(e.g. Ollama, remote Tailscale) — the reaper won't touch them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("process_manager")

DEFAULT_IDLE_TIMEOUT = 600       # 10 minutes
REAPER_INTERVAL = 30             # check every 30s
STARTUP_POLL_INTERVAL = 2        # poll health every 2s while waiting for startup
DEFAULT_STARTUP_TIMEOUT = 120    # 2 minutes max to wait for server ready
DEFAULT_HEALTH_PATH = "/v1/models"


@dataclass
class ManagedBackend:
    """State for one backend whose process lifecycle we own."""
    backend_id: str
    endpoint: str                        # e.g. "http://127.0.0.1:8800"
    launch_cmd: str                      # shell command with {model} placeholder
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    log_file: str = "/tmp/cm-backend.log"
    health_path: str = DEFAULT_HEALTH_PATH
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    port: int | None = None              # for port-cleanup before launch

    # Runtime state
    pid: int | None = None
    last_active: float = 0.0             # time.monotonic()
    model_loaded: str | None = None      # the {model} that was launched
    in_flight: int = 0                   # active streaming requests — reaper skips > 0


class ProcessManager:
    """Manages backend server processes with lazy loading and idle reaping."""

    def __init__(self) -> None:
        self._managed: dict[str, ManagedBackend] = {}
        self._reaper_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ─── Configuration ───

    def register(self, backend_id: str, endpoint: str, launch_cfg: dict) -> None:
        """Register a backend for managed lifecycle. Called during config load."""
        mb = ManagedBackend(
            backend_id=backend_id,
            endpoint=endpoint,
            launch_cmd=launch_cfg.get("cmd", ""),
            idle_timeout=launch_cfg.get("idle_timeout", DEFAULT_IDLE_TIMEOUT),
            log_file=launch_cfg.get("log_file", f"/tmp/cm-{backend_id}.log"),
            health_path=launch_cfg.get("health_path", DEFAULT_HEALTH_PATH),
            startup_timeout=launch_cfg.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT),
            port=launch_cfg.get("port"),
        )
        self._managed[backend_id] = mb
        log.info("registered managed backend: %s (idle timeout: %ds)", backend_id, mb.idle_timeout)

    def is_managed(self, backend_id: str) -> bool:
        return backend_id in self._managed

    # ─── On-demand launch ───

    async def ensure_running(self, backend_id: str, model: str | None = None) -> bool:
        """Make sure the backend's server process is up. Returns True if ready.

        If model differs from what's currently loaded, kills and relaunches
        with the new model (handles MLX single-model-at-a-time constraint).

        EXCLUSIVE MODE: kills all OTHER managed backends before launching.
        Only one model loaded at a time — saves tens of GB of RAM.
        """
        mb = self._managed.get(backend_id)
        if not mb:
            return True  # not managed — assume externally running

        async with self._lock:
            # Check if already running and healthy
            if mb.pid and self._is_process_alive(mb.pid):
                # If model changed, need to restart
                if model and mb.model_loaded and model != mb.model_loaded:
                    log.info("model swap on %s: %s -> %s, restarting",
                             backend_id, mb.model_loaded, model)
                    self._kill_process(mb)
                else:
                    # Verify actually responding (process alive ≠ healthy)
                    if await self._probe_health(mb):
                        return True
                    else:
                        log.warning("%s pid %d alive but not healthy, restarting",
                                    backend_id, mb.pid)
                        self._kill_process(mb)

            # EXCLUSIVE: kill all OTHER managed backends before launching.
            # Only one model loaded at a time to conserve RAM.
            for other_id, other_mb in self._managed.items():
                if other_id != backend_id and other_mb.pid and self._is_process_alive(other_mb.pid):
                    log.info("exclusive mode: killing %s (pid %d) before launching %s",
                             other_id, other_mb.pid, backend_id)
                    self._kill_process(other_mb)

            # Also kill any stray servers on our port that weren't tracked
            # (e.g. spawned by Clyde's AgentManager or left from prior sessions)
            if mb.port:
                self._kill_port(mb.port)

            # Launch the server
            return await self._launch(mb, model)

    async def _launch(self, mb: ManagedBackend, model: str | None = None) -> bool:
        """Spawn the server process and wait for it to become healthy."""
        # Kill anything already on this port (zombie servers, stale instances)
        if mb.port:
            self._kill_port(mb.port)

        cmd = mb.launch_cmd
        if model and "{model}" in cmd:
            cmd = cmd.replace("{model}", model)

        log.info("launching %s: %s", mb.backend_id, cmd)

        try:
            log_path = Path(mb.log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "a")

            process = subprocess.Popen(
                cmd,
                shell=True,
                stdin=subprocess.DEVNULL,  # avoid bad-fd inheritance from agent
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,  # detach from agent
            )
            mb.pid = process.pid
            mb.model_loaded = model
            mb.last_active = time.monotonic()

            log.info("%s started pid %d, waiting for health…", mb.backend_id, mb.pid)

            # Wait for server to become healthy
            deadline = time.monotonic() + mb.startup_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(STARTUP_POLL_INTERVAL)
                if not self._is_process_alive(mb.pid):
                    log.error("%s pid %d died during startup", mb.backend_id, mb.pid)
                    mb.pid = None
                    return False
                if await self._probe_health(mb):
                    log.info("%s healthy after %.1fs",
                             mb.backend_id, time.monotonic() - (deadline - mb.startup_timeout))
                    return True

            log.error("%s failed to become healthy within %ds",
                      mb.backend_id, mb.startup_timeout)
            self._kill_process(mb)
            return False

        except Exception as e:
            log.error("failed to launch %s: %r", mb.backend_id, e)
            return False

    # ─── Activity tracking ───

    def mark_active(self, backend_id: str) -> None:
        """Update the last-active timestamp. Call this on every completion request."""
        mb = self._managed.get(backend_id)
        if mb:
            mb.last_active = time.monotonic()

    def mark_request_start(self, backend_id: str) -> None:
        """Increment in-flight counter. Reaper won't kill while > 0."""
        mb = self._managed.get(backend_id)
        if mb:
            mb.in_flight += 1
            mb.last_active = time.monotonic()

    def mark_request_end(self, backend_id: str) -> None:
        """Decrement in-flight counter + refresh last_active."""
        mb = self._managed.get(backend_id)
        if mb:
            mb.in_flight = max(0, mb.in_flight - 1)
            mb.last_active = time.monotonic()

    # ─── Idle reaper ───

    def start_reaper(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the background reaper task."""
        if self._reaper_task and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.ensure_future(self._reaper_loop())
        log.info("idle reaper started (interval=%ds)", REAPER_INTERVAL)

    async def _reaper_loop(self) -> None:
        """Periodically check for and kill idle backends."""
        while True:
            try:
                await asyncio.sleep(REAPER_INTERVAL)
                await self._reap_idle()
            except asyncio.CancelledError:
                log.info("reaper cancelled")
                break
            except Exception as e:
                log.error("reaper error: %r", e)

    async def _reap_idle(self) -> None:
        """Kill any managed backend that's been idle longer than its timeout."""
        now = time.monotonic()
        for bid, mb in self._managed.items():
            if not mb.pid or not self._is_process_alive(mb.pid):
                continue
            # Never kill a backend with active streaming requests —
            # the reaper was killing mlx-vlm mid-generation because
            # mark_active only fired at request START and long streams
            # (10+ min creative writing) outlived the idle timeout.
            if mb.in_flight > 0:
                continue
            idle_seconds = now - mb.last_active
            if mb.last_active > 0 and idle_seconds > mb.idle_timeout:
                log.info("reaping idle backend %s (pid %d, idle %.0fs > %ds)",
                         bid, mb.pid, idle_seconds, mb.idle_timeout)
                self._kill_process(mb)

    # ─── Process control helpers ───

    def _kill_process(self, mb: ManagedBackend) -> None:
        """Gracefully stop, then force-kill the backend's server process."""
        if not mb.pid:
            return
        try:
            # Try SIGTERM first (graceful)
            os.kill(mb.pid, signal.SIGTERM)
            # Give it 3 seconds
            for _ in range(6):
                time.sleep(0.5)
                if not self._is_process_alive(mb.pid):
                    break
            else:
                # Force kill
                os.kill(mb.pid, signal.SIGKILL)
                time.sleep(0.5)
            log.info("killed %s pid %d", mb.backend_id, mb.pid)
        except ProcessLookupError:
            pass  # already dead
        except Exception as e:
            log.error("error killing %s pid %d: %r", mb.backend_id, mb.pid, e)
        finally:
            mb.pid = None
            mb.model_loaded = None

    def _kill_port(self, port: int) -> None:
        """Kill any process listening on the given TCP port."""
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p) for p in result.stdout.strip().split() if p.isdigit()]
            for pid in pids:
                # Don't kill ourselves or the agent
                if pid == os.getpid():
                    continue
                log.info("killing stale process pid %d on port %d", pid, port)
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            if pids:
                time.sleep(1)
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                time.sleep(0.5)
        except Exception as e:
            log.warning("port cleanup on %d failed: %r", port, e)

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)  # signal 0 = existence check
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def _probe_health(self, mb: ManagedBackend) -> bool:
        """Quick HTTP probe to see if the backend is responding."""
        url = mb.endpoint.rstrip("/") + mb.health_path
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=3.0)
                return resp.status_code == 200
        except Exception:
            return False

    # ─── Shutdown ───

    def stop_all_backends(self) -> None:
        """Kill all managed backends but keep the reaper alive.
        Called by Clyde before switching models to ensure a clean slate."""
        for bid, mb in self._managed.items():
            if mb.pid and self._is_process_alive(mb.pid):
                log.info("stopping backend %s (pid %d) for model switch", bid, mb.pid)
                self._kill_process(mb)

    def shutdown_all(self) -> None:
        """Kill all managed backends AND the reaper. Called on agent exit."""
        self.stop_all_backends()
        if self._reaper_task:
            self._reaper_task.cancel()

    # ─── Status ───

    def status(self) -> list[dict]:
        """Return status of all managed backends for debug endpoints."""
        now = time.monotonic()
        out = []
        for bid, mb in self._managed.items():
            alive = mb.pid is not None and self._is_process_alive(mb.pid)
            idle = (now - mb.last_active) if mb.last_active > 0 else None
            out.append({
                "backend_id": bid,
                "managed": True,
                "running": alive,
                "pid": mb.pid if alive else None,
                "model_loaded": mb.model_loaded if alive else None,
                "idle_seconds": round(idle) if idle else None,
                "idle_timeout": mb.idle_timeout,
                "time_until_reap": round(mb.idle_timeout - idle) if (idle and alive) else None,
            })
        return out


# ─── Singleton ───

_instance: ProcessManager | None = None


def get_process_manager() -> ProcessManager:
    global _instance
    if _instance is None:
        _instance = ProcessManager()
    return _instance
