"""
agent/backends/base.py
───────────────────────
Protocol-level definitions for the Clyde backend abstraction (ADR-001).

A *Backend* is "how we talk to the server that serves a model." It knows
nothing about which model is loaded, what the tool-call format looks like,
or how thinking is requested — those concerns live on the ModelProfile.

Design constraints (Q1/Q2 in ADR-001):
  - Hot path is per-chunk. No dict lookups, no reflection, no YAML parsing
    during streaming. Everything mutable is captured at Backend construction.
  - Backend is stateless at the request level. Two concurrent requests on
    the same Backend must not share any internal state except the httpx
    client's connection pool.
  - All backends present the SAME CompletionChunk event stream regardless of
    upstream payload shape. The router never branches on backend type.
"""
from __future__ import annotations

import asyncio
import time
import typing as t
from dataclasses import dataclass, field
from enum import Enum

# ──────────────────────────────────────────────────────────────────────────────
# Value types
# ──────────────────────────────────────────────────────────────────────────────


class ThinkingMode(str, Enum):
    """Tri-state switch requested by the turn loop for a given completion."""

    ON = "on"
    OFF = "off"
    AUTO = "auto"  # let the ModelProfile decide based on phase


@dataclass(frozen=True, slots=True)
class Sampling:
    """Normalised sampling knobs. Backends translate to their native keys."""

    temperature: float = 0.7
    top_p: float = 0.95
    top_k: int = 40
    repetition_penalty: float = 1.05
    max_tokens: int = 2048
    stop: tuple[str, ...] = ()
    seed: int | None = None


@dataclass(slots=True)
class Message:
    """OpenAI-shaped, but we own the dataclass so swaps stay local."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None  # OpenAI tool_calls payload, when role=assistant


@dataclass(slots=True)
class ToolSpec:
    """Tool schema to expose to the model. Backends translate as needed."""

    name: str
    description: str
    parameters: dict  # JSON Schema


# ──────────────────────────────────────────────────────────────────────────────
# Streaming chunk events
# ──────────────────────────────────────────────────────────────────────────────


class ChunkKind(str, Enum):
    TEXT = "text"            # delta of assistant content
    THINKING = "thinking"    # delta of reasoning/thinking block
    TOOL_CALL = "tool_call"  # structured tool invocation
    FINISH = "finish"        # terminal; carries finish_reason
    ERROR = "error"          # terminal; carries error info


@dataclass(slots=True)
class CompletionChunk:
    """One normalised event in a streaming completion.

    The contract is strict: backends must only emit chunks whose `kind` is
    the correct enum variant, and must always emit exactly one `FINISH` or
    `ERROR` as the final chunk before closing the stream.
    """

    kind: ChunkKind
    text: str = ""                       # TEXT or THINKING payload
    tool_call: dict | None = None        # TOOL_CALL payload: {id, name, arguments_json}
    finish_reason: str | None = None     # FINISH: "stop" | "length" | "tool_calls" | "error"
    error: str | None = None             # ERROR message
    raw: dict | None = None              # optional: upstream delta for debug


@dataclass(slots=True)
class HealthStatus:
    reachable: bool
    latency_ms: float
    loaded_model: str | None = None
    notes: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Backend protocol
# ──────────────────────────────────────────────────────────────────────────────


class Backend(t.Protocol):
    """
    Every backend must implement this protocol.

    - `id`: stable identifier (matches the key in backends.yaml).
    - `kind`: short type tag (mlx | llamacpp | ollama | openai).
    - `endpoint`: the base URL.
    - `complete(...)`: the streaming call.
    - `probe()`: cheap reachability check.
    """

    id: str
    kind: str
    endpoint: str

    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        sampling: Sampling,
        thinking: ThinkingMode,
        profile: "ModelProfileLike",  # avoid import cycle with forward ref
        backend_hints: dict | None = None,
    ) -> t.AsyncIterator[CompletionChunk]:
        """Stream a completion. MUST yield exactly one FINISH or ERROR chunk at end."""
        ...

    async def probe(self, timeout_s: float = 2.0) -> HealthStatus:
        """Cheap health check. Must not perform a completion."""
        ...


# ──────────────────────────────────────────────────────────────────────────────
# ModelProfile forward reference
# ──────────────────────────────────────────────────────────────────────────────


class ModelProfileLike(t.Protocol):
    """Backend's view of a ModelProfile. Defined here to avoid import cycle.

    Backends only need three things from the profile at request time:
      1. The thinking directive (how to ask the backend for thinking).
      2. The tool-call formatting strategy (OpenAI-shape vs. grammar-based).
      3. Any chat-template quirks (extra kwargs to pass through).
    """

    id: str
    family: str
    thinking_directive: str             # "chat_template_kwarg" | "system_prompt_marker" | "force_template" | "not_supported"
    thinking_directive_payload: dict    # profile-specific rendering hints
    tool_schema_strategy: str           # "openai" | "gbnf" | "qwen_xml"
    chat_template_quirks: dict          # free-form kwargs merged into request


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers for concrete backends
# ──────────────────────────────────────────────────────────────────────────────


def now_ms() -> float:
    return time.monotonic() * 1000.0


__all__ = [
    "ThinkingMode",
    "Sampling",
    "Message",
    "ToolSpec",
    "ChunkKind",
    "CompletionChunk",
    "HealthStatus",
    "Backend",
    "ModelProfileLike",
    "now_ms",
]
