#!/usr/bin/env python3
"""
parse_bookmarks.py — Parse browser bookmark exports into state.json

Supports:
  - Netscape HTML (Chrome, Firefox, Safari, Zen, Edge, etc.)
  - JSON (Chrome Bookmarks file, Firefox jsonlz4 after extraction)

Usage:
  python3 parse_bookmarks.py <input_path> <state_dir>

Creates:
  <state_dir>/state.json   — master item list with all bookmarks
"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


class NetscapeBookmarkParser(HTMLParser):
    """Parse Netscape-format bookmark HTML exports.

    All major browsers export in this format. Structure is:
    <DT><H3>Folder Name</H3>
    <DL><p>
      <DT><A HREF="url" ADD_DATE="epoch" ...>Title</A>
      ...
    </DL><p>
    """

    def __init__(self):
        super().__init__()
        self.bookmarks: list[dict] = []
        self.folder_stack: list[str] = []
        self._current_tag = ""
        self._current_attrs: dict = {}
        self._current_text = ""
        self._in_h3 = False
        self._in_a = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag = tag.lower()
        attr_dict = {k.lower(): v for k, v in attrs}

        if tag == "h3":
            self._in_h3 = True
            self._current_text = ""
        elif tag == "a":
            self._in_a = True
            self._current_attrs = attr_dict
            self._current_text = ""
        elif tag == "dl":
            pass  # folder content starts
        elif tag == "dt":
            pass

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            folder_name = self._current_text.strip()
            self.folder_stack.append(folder_name)

        elif tag == "a" and self._in_a:
            self._in_a = False
            title = self._current_text.strip()
            href = self._current_attrs.get("href", "")

            if href and href.startswith(("http://", "https://", "ftp://")):
                add_date = self._current_attrs.get("add_date", "")
                icon = self._current_attrs.get("icon", "")
                tags = self._current_attrs.get("tags", "")

                # Parse epoch timestamp
                date_added = None
                if add_date:
                    try:
                        ts = int(add_date)
                        # Chrome uses microseconds, Firefox uses seconds
                        if ts > 1e12:
                            ts = ts // 1_000_000
                        date_added = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    except (ValueError, OSError):
                        pass

                bookmark = {
                    "url": href,
                    "title": title or href,
                    "folder_path": "/".join(self.folder_stack) if self.folder_stack else "",
                    "date_added": date_added,
                    "tags": tags,
                }
                if icon:
                    bookmark["icon_data"] = icon[:100]  # truncate base64 icons
                self.bookmarks.append(bookmark)

        elif tag == "dl":
            if self.folder_stack:
                self.folder_stack.pop()

    def handle_data(self, data: str):
        if self._in_h3 or self._in_a:
            self._current_text += data


def parse_netscape_html(path: Path) -> list[dict]:
    """Parse a Netscape-format bookmark HTML file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    parser = NetscapeBookmarkParser()
    parser.feed(text)
    return parser.bookmarks


def parse_chrome_json(path: Path) -> list[dict]:
    """Parse Chrome's Bookmarks JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    bookmarks = []

    def walk(node: dict, path_parts: list[str]):
        ntype = node.get("type", "")
        if ntype == "folder":
            children = node.get("children", [])
            folder_name = node.get("name", "")
            new_path = path_parts + [folder_name] if folder_name else path_parts
            for child in children:
                walk(child, new_path)
        elif ntype == "url":
            url = node.get("url", "")
            if url.startswith(("http://", "https://")):
                date_added = None
                raw = node.get("date_added", "")
                if raw:
                    try:
                        # Chrome epoch: microseconds since 1601-01-01
                        chrome_epoch = int(raw)
                        unix_ts = (chrome_epoch - 11644473600000000) / 1_000_000
                        date_added = datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
                    except (ValueError, OSError):
                        pass
                bookmarks.append({
                    "url": url,
                    "title": node.get("name", url),
                    "folder_path": "/".join(path_parts),
                    "date_added": date_added,
                    "tags": "",
                })

    roots = data.get("roots", {})
    for root_name, root_node in roots.items():
        if isinstance(root_node, dict) and "children" in root_node:
            walk(root_node, [root_name])

    return bookmarks


def detect_and_parse(input_path: Path) -> tuple[str, list[dict]]:
    """Auto-detect format and parse."""
    text_start = input_path.read_text(encoding="utf-8", errors="replace")[:500].strip()

    # Netscape HTML
    if "<!DOCTYPE NETSCAPE-Bookmark-file" in text_start or "<DL>" in text_start.upper():
        return "bookmarks_html", parse_netscape_html(input_path)

    # JSON
    if text_start.startswith("{"):
        data = json.loads(input_path.read_text(encoding="utf-8"))
        if "roots" in data:
            return "bookmarks_chrome_json", parse_chrome_json(input_path)
        # Generic JSON array of bookmarks
        if isinstance(data, list):
            return "bookmarks_json", data

    raise ValueError(f"Unrecognized bookmark format in {input_path}")


def dedup_bookmarks(bookmarks: list[dict]) -> tuple[list[dict], int]:
    """Remove duplicates by URL. Returns (deduped_list, dup_count)."""
    seen_urls: set[str] = set()
    unique: list[dict] = []
    dups = 0
    for bm in bookmarks:
        url = bm.get("url", "").rstrip("/").lower()
        if url in seen_urls:
            dups += 1
            continue
        seen_urls.add(url)
        unique.append(bm)
    return unique, dups


def build_state(source_type: str, source_path: str, bookmarks: list[dict]) -> dict:
    """Build the state.json structure."""
    items = []
    for i, bm in enumerate(bookmarks):
        items.append({
            "id": i,
            "original": bm,
            "classification": None,
            "status": "pending",
        })

    return {
        "job_id": str(uuid.uuid4()),
        "source_type": source_type,
        "source_path": source_path,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        "settings": {
            "ambiguity_mode": "batch_and_ask",
            "execution_mode": "hybrid",
            "batch_strategy": "adaptive",
        },
        "items": items,
        "progress": {
            "phase": "ingest",
            "next_batch_start": 0,
            "batch_size": 30,
            "total_classified": 0,
            "total_uncertain": 0,
            "total_pending": len(items),
        },
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: parse_bookmarks.py <input_path> <state_dir>", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1]).expanduser().resolve()
    state_dir = Path(sys.argv[2]).expanduser().resolve()

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Create state directory
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "batches").mkdir(exist_ok=True)

    # Parse
    source_type, bookmarks = detect_and_parse(input_path)
    raw_count = len(bookmarks)

    # Dedup
    bookmarks, dup_count = dedup_bookmarks(bookmarks)

    # Build state
    state = build_state(source_type, str(input_path), bookmarks)

    # Atomic write
    tmp_path = state_dir / "state.json.tmp"
    final_path = state_dir / "state.json"
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.rename(final_path)

    # Initialize empty taxonomy and pending review
    (state_dir / "taxonomy.json").write_text(
        json.dumps({"categories": {}, "version": 0}, indent=2),
        encoding="utf-8",
    )
    (state_dir / "pending_review.json").write_text(
        json.dumps({"items": []}, indent=2),
        encoding="utf-8",
    )

    # Report
    print(f"OK: Parsed {len(bookmarks)} bookmarks ({source_type})")
    if dup_count:
        print(f"  Removed {dup_count} duplicates (from {raw_count} total)")
    print(f"  State dir: {state_dir}")
    print(f"  Job ID: {state['job_id']}")

    # Extract folder distribution for taxonomy hints
    folders: dict[str, int] = {}
    for bm in bookmarks:
        folder = bm.get("folder_path", "") or "(unfiled)"
        folders[folder] = folders.get(folder, 0) + 1
    if folders:
        print(f"  Existing folders ({len(folders)}):")
        for folder, count in sorted(folders.items(), key=lambda x: -x[1])[:20]:
            print(f"    {folder}: {count}")


if __name__ == "__main__":
    main()
