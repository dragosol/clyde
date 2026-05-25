"""Qwen 3 / 3.5 tool-call parser.

Qwen emits tool calls as:
    <tool_call>
    {"name": "foo", "arguments": {...}}
    </tool_call>

We fish completed frames out of the accumulated text. An unclosed frame
returns None so the turn loop keeps streaming.
"""
from __future__ import annotations

import json
import re

_OPEN = "<tool_call>"
_CLOSE = "</tool_call>"
_FRAME_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def parse(accumulated_text: str) -> list[dict] | None:
    if _OPEN not in accumulated_text:
        return None
    matches = _FRAME_RE.findall(accumulated_text)
    if not matches:
        return None
    out: list[dict] = []
    for idx, body in enumerate(matches):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        name = data.get("name") or ""
        args = data.get("arguments", {})
        if isinstance(args, dict):
            args_json = json.dumps(args)
        elif isinstance(args, str):
            args_json = args
        else:
            args_json = json.dumps(args)
        out.append({
            "id": f"qwen-{idx}",
            "name": name,
            "arguments_json": args_json,
            "index": idx,
        })
    return out or None
