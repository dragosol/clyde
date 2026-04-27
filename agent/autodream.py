#!/usr/bin/env python3
"""
Clyde — autoDream Memory Consolidation
==============================================
Aligned with claw-code's memory architecture principles:
  - Runs in an ISOLATED context (forked subagent, limited tools)
  - Memory is continuously edited, not appended
  - Aggressively prunes stale/low-value memories

Operations:
  1. Load all memory topic files
  2. Call the model to consolidate:
     - Merge related memories into one
     - Deduplicate overlapping content
     - Remove contradictions (newer wins)
     - Convert vague dates → absolute
     - Prune: derivable, stale, low-value
  3. Apply changes (update/delete/merge files)
  4. Rebuild MEMORY.md index from scratch

Isolation:
  - This runs as a separate process (LaunchAgent, 3am daily)
  - Has NO access to the main conversation context
  - Can only read/write memory files
  - Uses a low-temperature model call for precision
"""

import os
import sys
import json
import re
import yaml
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

import httpx

# ─── Config ───
CFG_PATH = Path(__file__).parent / "config.yaml"
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)

MEMORY_DIR = Path(os.path.expanduser(CFG["paths"]["memory_dir"]))
INDEX_FILE = MEMORY_DIR / CFG["memory"]["index_file"]
BACKEND_URL = CFG["backend"]["url"]
MODEL = CFG["backend"]["model"]
MAX_MEMORIES = CFG["autodream"]["max_memories"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [autodream] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("autodream")


def load_all_memories() -> list[dict]:
    """Load all memory topic files."""
    memories = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            text = f.read_text()
            fm = {}
            m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if m:
                fm = yaml.safe_load(m.group(1)) or {}
                body = text[m.end():].strip()
            else:
                body = text.strip()

            memories.append({
                "filename": f.name,
                "name": fm.get("name", f.stem),
                "description": fm.get("description", ""),
                "type": fm.get("type", "unknown"),
                "created": fm.get("created", ""),
                "content": body,
            })
        except Exception as e:
            log.warning(f"Failed to read {f.name}: {e}")
    return memories


def build_consolidation_prompt(memories: list[dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    memory_dump = ""
    for i, m in enumerate(memories):
        memory_dump += f"\n### [{i}] {m['filename']} ({m['type']})\n"
        memory_dump += f"Name: {m['name']}\n"
        memory_dump += f"Description: {m['description']}\n"
        memory_dump += f"Created: {m['created']}\n"
        memory_dump += f"Content:\n{m['content']}\n"

    return f"""You are the memory consolidation agent for Clyde. Today is {today}.

You have {len(memories)} memories to review. Produce a consolidated set.

RULES:
1. MERGE: Combine memories about the same topic into one.
2. DEDUPE: Remove near-duplicates. Keep the more detailed one.
3. CONTRADICTIONS: If two memories contradict, keep the newer one.
4. DATES: Convert relative dates ("last Thursday") to absolute using the created date.
5. PRUNE: Remove memories that are:
   - About code patterns, file paths, architecture (derivable from code)
   - About debugging solutions (fix is in the code)
   - Stale project info (completed tasks, past deadlines)
   - Trivially obvious or low-value
6. REWRITE: Improve clarity. Lead feedback/project memories with rule/fact, then Why + How to apply.
7. CAP: Maximum {MAX_MEMORIES} memories. If over, drop least valuable.

CURRENT MEMORIES:
{memory_dump}

OUTPUT FORMAT:
Return a JSON array. Each object:
{{"action": "keep" | "update" | "delete", "filename": "...", "name": "...", "description": "...", "type": "...", "content": "..."}}

For "delete": only filename needed.
For "keep": skip it from output (no changes).
For "update": include all fields with rewritten content.

Return ONLY the JSON array. If no changes needed, return [].
"""


def call_model(prompt: str) -> str:
    """Call MLX backend for consolidation. Low temperature for precision."""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a memory consolidation agent. Output only valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 16384,
        "temperature": 0.3,
        "stream": False
    }
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(f"{BACKEND_URL}/v1/chat/completions", json=payload)
            if resp.status_code != 200:
                log.error(f"Backend error: {resp.status_code}")
                return "[]"
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            # Strip thinking tags
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
            return content.strip()
    except Exception as e:
        log.error(f"Model call failed: {e}")
        return "[]"


def apply_changes(memories: list[dict], changes: list[dict]):
    """Apply consolidation changes to memory files."""
    for change in changes:
        action = change.get("action", "")
        filename = change.get("filename", "")

        if action == "delete":
            filepath = MEMORY_DIR / filename
            if filepath.exists():
                filepath.unlink()
                log.info(f"  Deleted: {filename}")

        elif action == "update":
            filepath = MEMORY_DIR / filename
            fm = f"""---
name: {change.get('name', '')}
description: {change.get('description', '')}
type: {change.get('type', 'user')}
created: {next((m['created'] for m in memories if m['filename'] == filename), '')}
consolidated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
---

{change.get('content', '')}
"""
            filepath.write_text(fm)
            log.info(f"  Updated: {filename}")


def rebuild_index():
    """Rebuild MEMORY.md from all remaining topic files."""
    lines = ["# Clyde Memory Index\n"]
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            text = f.read_text()
            m = re.search(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if m:
                fm = yaml.safe_load(m.group(1)) or {}
                name = fm.get("name", f.stem)
                desc = fm.get("description", "")
                entry = f"- [{name}]({f.name}) — {desc}"
                if len(entry) > 150:
                    entry = entry[:147] + "..."
                lines.append(entry)
            else:
                lines.append(f"- [{f.stem}]({f.name})")
        except Exception:
            lines.append(f"- [{f.stem}]({f.name}) — (parse error)")

    INDEX_FILE.write_text("\n".join(lines) + "\n")
    log.info(f"Index rebuilt: {len(lines) - 1} entries")


def run():
    log.info("=" * 50)
    log.info("autoDream consolidation starting")
    log.info("=" * 50)

    memories = load_all_memories()
    log.info(f"Loaded {len(memories)} memories")

    if not memories:
        log.info("No memories to consolidate.")
        return

    prompt = build_consolidation_prompt(memories)
    log.info("Calling model for consolidation...")
    result = call_model(prompt)

    try:
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if match:
            changes = json.loads(match.group())
        else:
            changes = json.loads(result)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse model response: {e}")
        log.error(f"Response: {result[:500]}")
        return

    if not changes:
        log.info("No changes needed.")
        return

    log.info(f"Applying {len(changes)} changes:")
    apply_changes(memories, changes)
    rebuild_index()
    log.info("autoDream consolidation complete")


if __name__ == "__main__":
    run()
