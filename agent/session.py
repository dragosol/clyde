"""
Clyde — Session State
=============================
Aligned with claw-code: rust/crates/runtime/src/session.rs

Session holds versioned conversation messages.
Each message has a role and content blocks.
ContentBlock types: Text, ToolUse, ToolResult.
Sessions can be saved/loaded from JSON.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any


# ─── Content Blocks (aligned with claw-code ContentBlock enum) ───

@dataclass
class TextBlock:
    text: str
    type: str = "text"

    def to_dict(self) -> dict:
        return {"type": "text", "text": self.text}


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"

    def to_dict(self) -> dict:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class ToolResultBlock:
    tool_use_id: str
    tool_name: str
    output: str
    is_error: bool = False
    type: str = "tool_result"

    def to_dict(self) -> dict:
        return {
            "type": "tool_result", "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name, "output": self.output, "is_error": self.is_error
        }


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


def block_from_dict(d: dict) -> ContentBlock:
    t = d.get("type", "text")
    if t == "text":
        return TextBlock(text=d["text"])
    elif t == "tool_use":
        return ToolUseBlock(id=d["id"], name=d["name"], input=d.get("input", {}))
    elif t == "tool_result":
        return ToolResultBlock(
            tool_use_id=d["tool_use_id"], tool_name=d["tool_name"],
            output=d["output"], is_error=d.get("is_error", False)
        )
    return TextBlock(text=str(d))


# ─── Conversation Message ───

@dataclass
class ConversationMessage:
    role: str  # "system", "user", "assistant", "tool"
    blocks: list[ContentBlock] = field(default_factory=list)
    usage: Optional[dict] = None

    @staticmethod
    def user_text(text: str) -> ConversationMessage:
        return ConversationMessage(role="user", blocks=[TextBlock(text=text)])

    @staticmethod
    def assistant_text(text: str, usage: dict = None) -> ConversationMessage:
        return ConversationMessage(role="assistant", blocks=[TextBlock(text=text)], usage=usage)

    @staticmethod
    def assistant_with_blocks(blocks: list[ContentBlock], usage: dict = None) -> ConversationMessage:
        return ConversationMessage(role="assistant", blocks=blocks, usage=usage)

    @staticmethod
    def tool_result(tool_use_id: str, tool_name: str, output: str, is_error: bool = False) -> ConversationMessage:
        return ConversationMessage(
            role="tool",
            blocks=[ToolResultBlock(tool_use_id=tool_use_id, tool_name=tool_name, output=output, is_error=is_error)]
        )

    @staticmethod
    def system_text(text: str) -> ConversationMessage:
        return ConversationMessage(role="system", blocks=[TextBlock(text=text)])

    def text_content(self) -> str:
        """Extract all text content from this message."""
        parts = []
        for b in self.blocks:
            if isinstance(b, TextBlock):
                parts.append(b.text)
        return "\n".join(parts)

    def tool_uses(self) -> list[ToolUseBlock]:
        """Extract all tool use blocks."""
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]

    def to_dict(self) -> dict:
        d = {"role": self.role, "blocks": [b.to_dict() for b in self.blocks]}
        if self.usage:
            d["usage"] = self.usage
        return d

    @staticmethod
    def from_dict(d: dict) -> ConversationMessage:
        blocks = [block_from_dict(b) for b in d.get("blocks", [])]
        return ConversationMessage(role=d["role"], blocks=blocks, usage=d.get("usage"))

    def to_openai_message(self) -> dict:
        """Convert to OpenAI chat format for sending to MLX backend."""
        if self.role == "tool":
            # Tool results go back as user messages with tool result markers
            for b in self.blocks:
                if isinstance(b, ToolResultBlock):
                    prefix = "ERROR: " if b.is_error else ""
                    return {
                        "role": "tool",
                        "tool_call_id": b.tool_use_id,
                        "content": f"{prefix}{b.output}"
                    }
            return {"role": "user", "content": "(empty tool result)"}

        if self.role == "assistant":
            # Check for tool calls
            tool_calls = self.tool_uses()
            text_parts = [b.text for b in self.blocks if isinstance(b, TextBlock)]
            content = "\n".join(text_parts) if text_parts else None

            if tool_calls:
                msg: dict[str, Any] = {"role": "assistant"}
                if content:
                    msg["content"] = content
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.input)}
                    }
                    for tc in tool_calls
                ]
                return msg
            return {"role": "assistant", "content": content or ""}

        # User or system
        text = self.text_content()
        return {"role": self.role, "content": text}


# ─── Session ───

@dataclass
class Session:
    version: int = 1
    messages: list[ConversationMessage] = field(default_factory=list)
    session_id: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.session_id:
            import uuid
            self.session_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def save(self, path: Path):
        """Save session to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "messages": [m.to_dict() for m in self.messages]
        }
        path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def load(path: Path) -> Session:
        """Load session from JSON file."""
        data = json.loads(path.read_text())
        messages = [ConversationMessage.from_dict(m) for m in data.get("messages", [])]
        return Session(
            version=data.get("version", 1),
            messages=messages,
            session_id=data.get("session_id", ""),
            created_at=data.get("created_at", "")
        )

    def estimate_tokens(self) -> int:
        """Rough token estimate: ~4 chars per token."""
        total = 0
        for m in self.messages:
            for b in m.blocks:
                if isinstance(b, TextBlock):
                    total += len(b.text) // 4
                elif isinstance(b, ToolUseBlock):
                    total += (len(b.name) + len(json.dumps(b.input))) // 4
                elif isinstance(b, ToolResultBlock):
                    total += len(b.output) // 4
        return total
