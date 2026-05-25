#!/usr/bin/env python3
"""
state_manager.py — Shared utilities for reading/writing organizer state files.

Can be used as a library (import) or as a CLI tool for quick state inspection.

CLI Usage:
  python3 state_manager.py <state_dir> status        — print progress summary
  python3 state_manager.py <state_dir> batch <N>     — print batch N items
  python3 state_manager.py <state_dir> uncertain     — list uncertain items
  python3 state_manager.py <state_dir> categories    — list categories with counts
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone


def read_state(state_dir: str | Path) -> dict:
    """Read state.json, returning the full state dict."""
    path = Path(state_dir).expanduser() / "state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(state_dir: str | Path, state: dict):
    """Atomic write of state.json."""
    sd = Path(state_dir).expanduser()
    state["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    tmp = sd / "state.json.tmp"
    final = sd / "state.json"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(final)


def read_taxonomy(state_dir: str | Path) -> dict:
    """Read taxonomy.json."""
    path = Path(state_dir).expanduser() / "taxonomy.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"categories": {}, "version": 0}


def write_taxonomy(state_dir: str | Path, taxonomy: dict):
    """Write taxonomy.json (non-atomic — small file, low risk)."""
    path = Path(state_dir).expanduser() / "taxonomy.json"
    taxonomy["last_updated"] = datetime.now(tz=timezone.utc).isoformat()
    path.write_text(json.dumps(taxonomy, indent=2, ensure_ascii=False), encoding="utf-8")


def read_pending_review(state_dir: str | Path) -> dict:
    """Read pending_review.json."""
    path = Path(state_dir).expanduser() / "pending_review.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"items": []}


def write_pending_review(state_dir: str | Path, pending: dict):
    """Write pending_review.json."""
    path = Path(state_dir).expanduser() / "pending_review.json"
    path.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")


def save_batch(state_dir: str | Path, batch_num: int, batch_data: dict):
    """Save a batch audit trail file."""
    sd = Path(state_dir).expanduser() / "batches"
    sd.mkdir(parents=True, exist_ok=True)
    path = sd / f"batch_{batch_num:03d}.json"
    path.write_text(json.dumps(batch_data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_batch_items(state: dict, start: int, count: int) -> list[dict]:
    """Extract a batch slice from state items."""
    items = state.get("items", [])
    return items[start:start + count]


def get_uncertain_items(state: dict) -> list[dict]:
    """Get all items with 'uncertain' status."""
    return [item for item in state.get("items", []) if item.get("status") == "uncertain"]


def get_category_distribution(state: dict) -> dict[str, int]:
    """Count items per category."""
    dist: dict[str, int] = {}
    for item in state.get("items", []):
        cls = item.get("classification")
        if cls and item.get("status") in ("classified", "user_resolved"):
            cat = cls.get("category", "uncategorized")
            dist[cat] = dist.get(cat, 0) + 1
    return dist


def update_batch_results(
    state: dict,
    batch_start: int,
    results: list[dict],
) -> dict:
    """Apply classification results to state items.

    Each result should have: id, category, subcategory, confidence, reasoning.
    Returns the updated state (caller should write_state after).
    """
    items = state.get("items", [])
    classified = 0
    uncertain = 0

    for result in results:
        item_id = result.get("id")
        if item_id is None or item_id >= len(items):
            continue

        item = items[item_id]
        confidence = result.get("confidence", 0.5)

        item["classification"] = {
            "category": result.get("category", ""),
            "subcategory": result.get("subcategory", ""),
            "confidence": confidence,
            "batch_num": result.get("batch_num", 0),
            "reasoning": result.get("reasoning", ""),
        }

        if confidence >= 0.6:
            item["status"] = "classified"
            classified += 1
        else:
            item["status"] = "uncertain"
            uncertain += 1

    # Update progress
    prog = state.get("progress", {})
    prog["next_batch_start"] = batch_start + len(results)
    prog["total_classified"] = sum(1 for i in items if i.get("status") == "classified")
    prog["total_uncertain"] = sum(1 for i in items if i.get("status") == "uncertain")
    prog["total_pending"] = sum(1 for i in items if i.get("status") == "pending")
    state["progress"] = prog

    return state


# ─── CLI ──────────────────────────────────────────────────────────────

def _cli_status(state_dir: str):
    state = read_state(state_dir)
    prog = state.get("progress", {})
    total = len(state.get("items", []))
    print(f"Job: {state.get('job_id', '?')}")
    print(f"Source: {state.get('source_type', '?')} — {state.get('source_path', '?')}")
    print(f"Phase: {prog.get('phase', '?')}")
    print(f"Total items: {total}")
    print(f"Classified: {prog.get('total_classified', 0)}")
    print(f"Uncertain: {prog.get('total_uncertain', 0)}")
    print(f"Pending: {prog.get('total_pending', 0)}")
    print(f"Next batch start: {prog.get('next_batch_start', 0)}")
    print(f"Batch size: {prog.get('batch_size', '?')}")


def _cli_categories(state_dir: str):
    state = read_state(state_dir)
    dist = get_category_distribution(state)
    taxonomy = read_taxonomy(state_dir)
    cats = taxonomy.get("categories", {})

    print(f"Categories ({len(dist)}):")
    for cat_key, count in sorted(dist.items(), key=lambda x: -x[1]):
        cat_info = cats.get(cat_key, {})
        name = cat_info.get("name", cat_key)
        print(f"  {name} ({cat_key}): {count} items")


def _cli_uncertain(state_dir: str):
    state = read_state(state_dir)
    uncertain = get_uncertain_items(state)
    print(f"Uncertain items ({len(uncertain)}):")
    for item in uncertain[:30]:  # cap at 30 for readability
        orig = item.get("original", {})
        cls = item.get("classification", {})
        label = orig.get("title", orig.get("name", orig.get("url", f"item {item['id']}")))
        print(f"  [{item['id']}] {label}")
        if cls:
            print(f"        → {cls.get('category', '?')} (confidence: {cls.get('confidence', '?')})")
            print(f"        reason: {cls.get('reasoning', '')}")


def main():
    if len(sys.argv) < 3:
        print("Usage: state_manager.py <state_dir> <command> [args...]", file=sys.stderr)
        sys.exit(1)

    state_dir = sys.argv[1]
    command = sys.argv[2]

    if command == "status":
        _cli_status(state_dir)
    elif command == "categories":
        _cli_categories(state_dir)
    elif command == "uncertain":
        _cli_uncertain(state_dir)
    elif command == "batch":
        if len(sys.argv) < 4:
            print("Usage: state_manager.py <state_dir> batch <start_index>", file=sys.stderr)
            sys.exit(1)
        state = read_state(state_dir)
        start = int(sys.argv[3])
        batch_size = state.get("progress", {}).get("batch_size", 30)
        items = get_batch_items(state, start, batch_size)
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
