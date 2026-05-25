"""agent/backends — concrete Backend implementations (ADR-001)."""
from backends.base import (
    Backend,
    ChunkKind,
    CompletionChunk,
    HealthStatus,
    Message,
    ModelProfileLike,
    Sampling,
    ThinkingMode,
    ToolSpec,
)
from backends.llamacpp import LlamaCppBackend
from backends.openai import GenericOpenAIBackend

__all__ = [
    "Backend",
    "ChunkKind",
    "CompletionChunk",
    "HealthStatus",
    "Message",
    "ModelProfileLike",
    "Sampling",
    "ThinkingMode",
    "ToolSpec",
    "LlamaCppBackend",
    "GenericOpenAIBackend",
    "make_backend",
]


def make_backend(config: dict) -> Backend:
    """Factory: one dict from backends.yaml → a concrete Backend.

    config schema:
        id: str        (required)
        type: str      (mlx | llamacpp | swiftlm | openai; required)
        endpoint: str  (required)
        **kwargs passed through to the backend class
    """
    kind = config["type"]
    klass = {
        "mlx": LlamaCppBackend,       # Legacy alias (MLX was purged)
        "swiftlm": LlamaCppBackend,   # SwiftLM — OpenAI-compatible API
        "llamacpp": LlamaCppBackend,
        "openai": GenericOpenAIBackend,
    }.get(kind)
    if klass is None:
        raise ValueError(f"Unknown backend type: {kind!r}")
    return klass(
        id=config["id"],
        endpoint=config["endpoint"],
        extra=config.get("extra", {}),
    )
