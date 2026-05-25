"""
agent/model_profiles — per-model metadata for Clyde (ADR-001).

A ModelProfile declares everything the turn loop needs to know about a
model that isn't "how do I reach its server" (that's a Backend). Compiled
at registry-load time for zero hot-path lookup cost.
"""
from __future__ import annotations

import importlib
import typing as t
from dataclasses import dataclass, field
from enum import Enum


class ThinkingDirective(str, Enum):
    """How a model's backend accepts thinking requests."""

    CHAT_TEMPLATE_KWARG = "chat_template_kwarg"    # e.g. enable_thinking kwarg passed to backend
    SYSTEM_PROMPT_MARKER = "system_prompt_marker"  # e.g. /no_think or explicit cue in system prompt
    FORCE_TEMPLATE = "force_template"              # llama.cpp with two chat templates
    NOT_SUPPORTED = "not_supported"                # model doesn't have a thinking mode


class ToolSchemaStrategy(str, Enum):
    OPENAI = "openai"          # vanilla OpenAI tool JSON; model is expected to emit tool_calls
    QWEN_XML = "qwen_xml"      # Qwen <tool_call>…</tool_call> tags around JSON
    GBNF = "gbnf"              # llama.cpp grammar-enforced shape
    FREEFORM = "freeform"      # model emits JSON blocks, parser fishes them out


# Callable shape for parsers. Receives a text-delta string (what has been
# accumulated in the current assistant turn) and returns either None
# (nothing to emit yet) or a list of completed tool_call dicts.
ToolCallParser = t.Callable[[str], list[dict] | None]


@dataclass(slots=True)
class PhaseProfile:
    """Per-phase sampling + max_tokens override table."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repetition_penalty: float | None = None
    max_tokens_floor: int | None = None


@dataclass(slots=True)
class ModelProfile:
    """All non-transport metadata about a model. Loaded once, hot-path readonly."""

    id: str
    family: str
    context_window: int = 131_072
    # Thinking
    thinking_directive: str = ThinkingDirective.NOT_SUPPORTED.value
    thinking_directive_payload: dict = field(default_factory=dict)
    thinking_mode_default: str = "on"    # "on" | "off" | "auto"
    # Tool-calling
    tool_schema_strategy: str = ToolSchemaStrategy.OPENAI.value
    tool_call_parser: ToolCallParser | None = None   # resolved callable
    tool_call_parser_path: str | None = None         # dotted path for debug
    # Chat template quirks (merged into request; backend-specific)
    chat_template_quirks: dict = field(default_factory=dict)
    # Sampling baseline
    sampling_defaults: dict = field(default_factory=dict)
    # Per-phase overrides. Keys are phase names matching conversation.py's Phase enum.
    phase_overrides: dict[str, PhaseProfile] = field(default_factory=dict)
    # Additional notes for debug panel
    notes: str = ""


def resolve_parser(dotted: str | None) -> ToolCallParser | None:
    """Import a parser callable by dotted path, once, at profile load.

    Tries the dotted path as given, and if the top-level module is not
    importable, retries with `agent.` stripped or prepended so the same
    YAML works whether PYTHONPATH points at ~/.clyde or at
    ~/.clyde/agent (the agent's own runtime layout).
    """
    if not dotted:
        return None
    module_name, _, attr = dotted.rpartition(".")
    if not module_name:
        raise ValueError(f"invalid parser path: {dotted!r}")

    candidates = [module_name]
    if module_name.startswith("agent."):
        candidates.append(module_name[len("agent."):])
    else:
        candidates.append(f"agent.{module_name}")

    last_exc: Exception | None = None
    for mod_path in candidates:
        try:
            mod = importlib.import_module(mod_path)
            break
        except ModuleNotFoundError as exc:
            last_exc = exc
            continue
    else:
        raise ModuleNotFoundError(
            f"parser {dotted!r}: none of {candidates} importable "
            f"(last: {last_exc!r})"
        )

    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise ValueError(f"parser {dotted!r} is not callable")
    return fn
