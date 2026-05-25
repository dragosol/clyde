"""
Debug Metrics — Thread-safe metrics aggregator for Clyde stack.

Centralized collection point for all components:
  - Agent (turn counts, tool calls, durations)
  - MLX (inference latency, tokens, recoveries)
  - Memory (read/write/search operations)
  - Compaction (events, token ratios, durations)
  - Errors (circular buffer of last 50)

Usage:
    from debug_metrics import metrics
    metrics.record_turn_complete(...)
    snapshot = metrics.snapshot()
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ErrorEvent:
    timestamp: float
    component: str      # "agent", "mlx", "memory", "compaction", "conversation"
    severity: str       # "info", "warning", "error"
    error_type: str     # "timeout", "oom", "crash", "permission", etc.
    message: str
    turn_id: Optional[str] = None
    resolved: bool = False

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "component": self.component,
            "severity": self.severity,
            "error_type": self.error_type,
            "message": self.message[:500],
            "turn_id": self.turn_id,
            "resolved": self.resolved,
        }


@dataclass
class CompactionEvent:
    timestamp: float
    tokens_before: int
    tokens_after: int
    duration_seconds: float
    success: bool
    method: str = "llm"  # "llm" or "emergency"

    def to_dict(self):
        return asdict(self)


@dataclass
class InferenceEvent:
    timestamp: float
    duration_ms: float
    tokens_generated: int
    prefill_tokens: int = 0
    finish_reason: str = ""

    def to_dict(self):
        return asdict(self)


class DebugMetrics:
    """Thread-safe singleton for metrics collection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.time()

        # Agent metrics
        self.total_turns = 0
        self.total_tool_calls = 0
        self.total_tokens_processed = 0
        self.total_turn_duration = 0.0
        self.failed_turns = 0
        self.active_turn_id: Optional[str] = None
        self.active_turn_start: Optional[float] = None
        self.active_turn_iteration: int = 0

        # MLX metrics
        self.total_inferences = 0
        self.total_tokens_generated = 0
        self.total_inference_duration_ms = 0.0
        self.timeout_recoveries = 0
        self.oom_recoveries = 0
        self.mlx_crashes = 0
        self.recent_inferences: deque[InferenceEvent] = deque(maxlen=20)

        # Memory metrics
        self.memory_reads = 0
        self.memory_writes = 0
        self.memory_searches = 0
        self.memory_updates = 0
        self.memory_deletes = 0

        # Compaction metrics
        self.compactions_triggered = 0
        self.compactions_failed = 0
        self.recent_compactions: deque[CompactionEvent] = deque(maxlen=10)

        # Error tracking
        self.recent_errors: deque[ErrorEvent] = deque(maxlen=50)

        # Tool call tracking
        self.tool_call_counts: dict[str, int] = {}  # tool_name → count
        self.tool_call_errors: dict[str, int] = {}   # tool_name → error count

    # ── Agent ──

    def record_turn_start(self, turn_id: str = None) -> str:
        with self._lock:
            if turn_id is None:
                import uuid
                turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            self.active_turn_id = turn_id
            self.active_turn_start = time.time()
            self.active_turn_iteration = 0
            return turn_id

    def record_turn_iteration(self, iteration: int):
        with self._lock:
            self.active_turn_iteration = iteration

    def record_turn_complete(self, duration: float, tool_calls: int,
                              tokens: int, success: bool):
        with self._lock:
            self.total_turns += 1
            self.total_tool_calls += tool_calls
            self.total_tokens_processed += tokens
            self.total_turn_duration += duration
            if not success:
                self.failed_turns += 1
            self.active_turn_id = None
            self.active_turn_start = None
            self.active_turn_iteration = 0

    def record_tool_call(self, tool_name: str, success: bool = True):
        with self._lock:
            self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1
            if not success:
                self.tool_call_errors[tool_name] = self.tool_call_errors.get(tool_name, 0) + 1

    # ── MLX ──

    def record_inference(self, duration_ms: float, tokens_generated: int,
                          prefill_tokens: int = 0, finish_reason: str = ""):
        with self._lock:
            self.total_inferences += 1
            self.total_tokens_generated += tokens_generated
            self.total_inference_duration_ms += duration_ms
            evt = InferenceEvent(
                timestamp=time.time(),
                duration_ms=duration_ms,
                tokens_generated=tokens_generated,
                prefill_tokens=prefill_tokens,
                finish_reason=finish_reason,
            )
            self.recent_inferences.append(evt)

    def record_mlx_timeout(self):
        with self._lock:
            self.timeout_recoveries += 1

    def record_mlx_oom(self):
        with self._lock:
            self.oom_recoveries += 1

    def record_mlx_crash(self):
        with self._lock:
            self.mlx_crashes += 1

    # ── Memory ──

    def record_memory_op(self, op_type: str):
        with self._lock:
            if op_type == "read":
                self.memory_reads += 1
            elif op_type == "write":
                self.memory_writes += 1
            elif op_type == "search":
                self.memory_searches += 1
            elif op_type == "update":
                self.memory_updates += 1
            elif op_type == "delete":
                self.memory_deletes += 1

    # ── Compaction ──

    def record_compaction(self, tokens_before: int, tokens_after: int,
                           duration: float, success: bool, method: str = "llm"):
        with self._lock:
            self.compactions_triggered += 1
            if not success:
                self.compactions_failed += 1
            evt = CompactionEvent(
                timestamp=time.time(),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                duration_seconds=duration,
                success=success,
                method=method,
            )
            self.recent_compactions.append(evt)

    # ── Errors ──

    def record_error(self, component: str, severity: str, error_type: str,
                      message: str, turn_id: str = None):
        with self._lock:
            self.recent_errors.append(ErrorEvent(
                timestamp=time.time(),
                component=component,
                severity=severity,
                error_type=error_type,
                message=message,
                turn_id=turn_id or self.active_turn_id,
            ))

    # ── Snapshot ──

    def snapshot(self) -> dict:
        """Return a complete metrics snapshot for /v1/debug."""
        with self._lock:
            uptime = time.time() - self._start_time
            avg_turn = (self.total_turn_duration / max(1, self.total_turns))
            avg_inference = (self.total_inference_duration_ms / max(1, self.total_inferences))

            return {
                "uptime_seconds": round(uptime, 1),
                "agent": {
                    "total_turns": self.total_turns,
                    "total_tool_calls": self.total_tool_calls,
                    "total_tokens_processed": self.total_tokens_processed,
                    "avg_turn_duration_seconds": round(avg_turn, 2),
                    "failed_turns": self.failed_turns,
                    "active_turn": {
                        "turn_id": self.active_turn_id,
                        "iteration": self.active_turn_iteration,
                        "elapsed_seconds": round(time.time() - self.active_turn_start, 1) if self.active_turn_start else None,
                    } if self.active_turn_id else None,
                    "tool_usage": dict(self.tool_call_counts),
                    "tool_errors": dict(self.tool_call_errors),
                },
                "mlx": {
                    "total_inferences": self.total_inferences,
                    "total_tokens_generated": self.total_tokens_generated,
                    "avg_latency_ms": round(avg_inference, 1),
                    "timeout_recoveries": self.timeout_recoveries,
                    "oom_recoveries": self.oom_recoveries,
                    "crashes": self.mlx_crashes,
                    "recent_inferences": [e.to_dict() for e in self.recent_inferences],
                },
                "memory": {
                    "reads": self.memory_reads,
                    "writes": self.memory_writes,
                    "searches": self.memory_searches,
                    "updates": self.memory_updates,
                    "deletes": self.memory_deletes,
                },
                "compaction": {
                    "total_triggered": self.compactions_triggered,
                    "total_failed": self.compactions_failed,
                    "recent": [e.to_dict() for e in self.recent_compactions],
                },
                "recent_errors": [e.to_dict() for e in self.recent_errors],
            }


# ── Global singleton ──
metrics = DebugMetrics()
