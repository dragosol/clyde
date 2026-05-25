"""Trivial parser for models that already emit OpenAI-shape tool_calls through
the backend's delta channel. These never need text-side parsing; return None
and rely on the backend to surface the structured events.
"""
from __future__ import annotations


def parse(accumulated_text: str) -> None:
    return None
