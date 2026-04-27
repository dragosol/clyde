"""
Clyde — Memory Operations (3-Layer)
===========================================
Aligned with claw-code's memory architecture:

Layer 1: MEMORY.md index (always loaded into system prompt, ~150 chars/line)
Layer 2: Topic files (on-demand, fetched when relevant via memory_read)
Layer 3: Transcripts (never loaded fully, only grep'd via transcript_search)

Write discipline:
  1. Write content to topic file with frontmatter
  2. Update MEMORY.md index with pointer
  Never dump content into the index.

What NOT to store:
  - Code patterns, architecture, file paths (derivable from code)
  - Git history (use git log / git blame)
  - Debugging solutions (fix is in the code; commit message has context)
  - Anything already documented in CLAW.md / README files
  - Ephemeral task details or current conversation context

Staleness rules:
  - If memory contradicts current reality → memory is wrong
  - Code-derived facts are never stored
  - Index is forcibly truncated at max_index_lines
"""

import os
import re
import json
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Config ───
_CFG_PATH = Path(__file__).parent / "config.yaml"
with open(_CFG_PATH) as f:
    _CFG = yaml.safe_load(f)

MEMORY_DIR = Path(os.path.expanduser(_CFG["paths"]["memory_dir"]))
TRANSCRIPTS_DIR = Path(os.path.expanduser(_CFG["paths"]["transcripts_dir"]))
INDEX_FILE = MEMORY_DIR / _CFG["memory"]["index_file"]
MAX_INDEX_LINES = _CFG["memory"]["max_index_lines"]
MAX_TOPIC_CHARS = _CFG["memory"]["max_topic_chars"]


def ensure_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text("# Clyde Memory Index\n\n")


# ─── Layer 1: Index ───

def read_index() -> str:
    """Read MEMORY.md index. Always loaded into system prompt. Truncated beyond max."""
    ensure_dirs()
    lines = INDEX_FILE.read_text().splitlines()
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
        lines.append(f"\n<!-- truncated at {MAX_INDEX_LINES} lines -->")
    return "\n".join(lines)


# ─── Layer 2: Topic Files ───

# ─── Path safety helper ───

def _safe_memory_path(filename: str):
    """Return resolved Path inside MEMORY_DIR, or None if traversal/empty."""
    if not filename:
        return None
    fp = MEMORY_DIR / filename
    try:
        if not fp.resolve().is_relative_to(MEMORY_DIR.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return fp


def read_topic(filename: str) -> str:
    """Read a specific topic file. On-demand, fetched when relevant."""
    filepath = _safe_memory_path(filename)
    if filepath is None:
        return "ERROR: Invalid memory path (empty or traversal attempt)."
    if not filepath.exists():
        return f"ERROR: Memory file '{filename}' not found."
    content = filepath.read_text()
    if len(content) > MAX_TOPIC_CHARS:
        content = content[:MAX_TOPIC_CHARS] + "\n\n[truncated]"
    return content


def write_memory(filename: str, name: str, description: str,
                 mem_type: str, content: str) -> str:
    """Two-step write: 1) topic file with frontmatter, 2) update index."""
    ensure_dirs()
    valid_types = {"user", "feedback", "project", "reference"}
    if mem_type not in valid_types:
        return f"ERROR: Invalid type '{mem_type}'. Must be one of: {valid_types}"

    # Sanitize filename
    filename = re.sub(r'[^\w\-.]', '_', filename)
    if not filename.endswith('.md'):
        filename += '.md'

    # Step 1: Write topic file
    filepath = MEMORY_DIR / filename
    frontmatter = f"""---
name: {name}
description: {description}
type: {mem_type}
created: {datetime.now().strftime('%Y-%m-%d %H:%M')}
---

{content}
"""
    filepath.write_text(frontmatter)

    # Step 2: Update index
    _update_index(filename, name, description)
    return f"Memory saved: {filename}"


def update_memory(filename: str, content: str) -> str:
    """Update existing memory content, preserving frontmatter."""
    filepath = _safe_memory_path(filename)
    if filepath is None:
        return "ERROR: Invalid memory path (empty or traversal attempt)."
    if not filepath.exists():
        return f"ERROR: Memory file '{filename}' not found."
    text = filepath.read_text()
    parts = text.split('---', 2)
    if len(parts) >= 3:
        new_text = f"---{parts[1]}---\n\n{content}\n"
    else:
        new_text = content
    filepath.write_text(new_text)
    return f"Memory updated: {filename}"


def delete_memory(filename: str) -> str:
    """Delete topic file and remove from index."""
    filepath = _safe_memory_path(filename)
    if filepath is None:
        return "ERROR: Invalid memory path (empty or traversal attempt)."
    if not filepath.exists():
        return f"ERROR: Memory file '{filename}' not found."
    filepath.unlink()
    _remove_from_index(filename)
    return f"Memory deleted: {filename}"


def search_memory(query: str) -> str:
    """Search across all topic files. Returns matching lines with filenames."""
    if not query:
        return "ERROR: No query provided."
    ensure_dirs()
    results = []
    for f in MEMORY_DIR.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if query.lower() in line.lower():
                    results.append(f"{f.name}:{i}: {line.strip()}")
        except Exception:
            continue
    if not results:
        return f"No matches for '{query}' in memory files."
    return "\n".join(results[:30])


def list_memories() -> str:
    """List all memory files with types and descriptions."""
    ensure_dirs()
    entries = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            text = f.read_text()
            m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if m:
                fm = yaml.safe_load(m.group(1))
                entries.append(f"  {f.name} [{fm.get('type','?')}] — {fm.get('description','')}")
            else:
                entries.append(f"  {f.name}")
        except Exception:
            entries.append(f"  {f.name} (unreadable)")
    if not entries:
        return "No memories stored yet."
    return "Memory files:\n" + "\n".join(entries)


# ─── Layer 3: Transcripts ───

def search_transcripts(query: str) -> str:
    """Grep transcripts. Never loaded fully — only searched."""
    if not query:
        return "ERROR: No query provided."
    ensure_dirs()
    results = []
    for f in sorted(TRANSCRIPTS_DIR.glob("*.jsonl"), reverse=True)[:20]:
        try:
            for i, line in enumerate(f.open(), 1):
                if query.lower() in line.lower():
                    results.append(f"{f.name}:{i}: {line.strip()[:200]}")
        except Exception:
            continue
    if not results:
        return f"No matches for '{query}' in transcripts."
    return "\n".join(results[:20])


def save_transcript(messages: list, response_preview: str):
    """Append a conversation turn to today's transcript JSONL."""
    ensure_dirs()
    today = datetime.now().strftime("%Y-%m-%d")
    filepath = TRANSCRIPTS_DIR / f"{today}.jsonl"
    # Only save last few messages for context (not the whole conversation)
    recent = messages[-4:] if len(messages) > 4 else messages
    entry = {
        "timestamp": datetime.now().isoformat(),
        "messages": [
            {"role": m.get("role", "?"), "content": str(m.get("content", ""))[:300]}
            for m in recent if isinstance(m, dict)
        ],
        "response_preview": response_preview[:500]
    }
    with open(filepath, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─── Internal ───

def _update_index(filename: str, name: str, description: str):
    ensure_dirs()
    text = INDEX_FILE.read_text()
    lines = text.splitlines()
    # Remove existing entry for this file
    lines = [l for l in lines if f"({filename})" not in l]
    # Add new entry (under 150 chars)
    entry = f"- [{name}]({filename}) — {description}"
    if len(entry) > 150:
        entry = entry[:147] + "..."
    lines.append(entry)
    if len(lines) > MAX_INDEX_LINES:
        lines = lines[:MAX_INDEX_LINES]
    INDEX_FILE.write_text("\n".join(lines) + "\n")


def _remove_from_index(filename: str):
    text = INDEX_FILE.read_text()
    lines = [l for l in text.splitlines() if f"({filename})" not in l]
    INDEX_FILE.write_text("\n".join(lines) + "\n")
