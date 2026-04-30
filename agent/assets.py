"""
Clyde — Conversation Assets Sidecar
==========================================

Per-conversation structured fact store that tracks files, URLs, search
queries, and extracted facts as typed nodes with edges between them.

Purpose:
  - Survives context compaction (lives in assets.json, not session.messages)
  - Injected into the system prompt so the model never loses track of what
    it's created, what URLs it found, or what facts it extracted
  - Data layer for a future Obsidian-style graph side panel

Storage:
  ~/.clyde/sessions/<conv_id>/assets.json

Follows the exact same patterns as plan.py: dataclasses with to_dict() /
from_dict(), no caching, direct JSON I/O per operation, per-conversation
directory isolation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("assets")


# ─── Constants ───

NODE_TYPE_FILE = "file"
NODE_TYPE_URL = "url"
NODE_TYPE_QUERY = "query"
NODE_TYPE_FACT = "fact"

STATUS_CREATED = "created"
STATUS_FETCHED = "fetched"
STATUS_NOT_FETCHED = "not_fetched"
STATUS_REFERENCED = "referenced"

EDGE_SEARCH_RESULT = "search_result"
EDGE_EXTRACTED_FROM = "extracted_from"
EDGE_FETCHED_AS = "fetched_as"

# System prompt injection budget
RENDER_TOKEN_BUDGET = 1500  # ~6000 chars


# ─── Data Model ───

@dataclass
class AssetNode:
    id: str
    type: str                    # file | url | query | fact
    title: str                   # short display name
    body: str                    # extended info (path, URL, extract)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_referenced_at: float = field(default_factory=time.time)
    status: str = STATUS_CREATED
    source_tool: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> AssetNode:
        return cls(
            id=d.get("id", f"a_{uuid.uuid4().hex[:8]}"),
            type=d.get("type", ""),
            title=d.get("title", ""),
            body=d.get("body", ""),
            metadata=d.get("metadata") or {},
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            last_referenced_at=d.get("last_referenced_at", time.time()),
            status=d.get("status", STATUS_CREATED),
            source_tool=d.get("source_tool", ""),
        )


@dataclass
class AssetEdge:
    id: str
    from_id: str
    to_id: str
    kind: str                    # search_result | extracted_from | fetched_as
    note: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> AssetEdge:
        return cls(
            id=d.get("id", f"e_{uuid.uuid4().hex[:6]}"),
            from_id=d.get("from_id", ""),
            to_id=d.get("to_id", ""),
            kind=d.get("kind", ""),
            note=d.get("note", ""),
            created_at=d.get("created_at", time.time()),
        )


@dataclass
class ConversationAssets:
    nodes: list[AssetNode] = field(default_factory=list)
    edges: list[AssetEdge] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, d: dict) -> ConversationAssets:
        return cls(
            nodes=[AssetNode.from_dict(n) for n in d.get("nodes", [])],
            edges=[AssetEdge.from_dict(e) for e in d.get("edges", [])],
            version=d.get("version", 1),
        )

    def add_node(self, **kwargs) -> AssetNode:
        node = AssetNode(id=f"a_{uuid.uuid4().hex[:8]}", **kwargs)
        self.nodes.append(node)
        return node

    def add_edge(self, from_id: str, to_id: str, kind: str, note: str = "") -> AssetEdge:
        edge = AssetEdge(
            id=f"e_{uuid.uuid4().hex[:6]}",
            from_id=from_id,
            to_id=to_id,
            kind=kind,
            note=note,
        )
        self.edges.append(edge)
        return edge

    def find_node(self, type: str, body: str) -> Optional[AssetNode]:
        """Find an existing node by type + body (dedup key)."""
        for n in self.nodes:
            if n.type == type and n.body == body:
                return n
        return None

    def get_node(self, node_id: str) -> Optional[AssetNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def nodes_by_type(self, type: str) -> list[AssetNode]:
        return [n for n in self.nodes if n.type == type]


# ─── Storage ───

def _conv_dir(session_dir: Path, conv_id: str) -> Path:
    """Per-conversation directory (same as plan.py)."""
    d = session_dir / conv_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _assets_path(session_dir: Path, conv_id: str) -> Path:
    return _conv_dir(session_dir, conv_id) / "assets.json"


def load_assets(session_dir: Path, conv_id: str) -> Optional[ConversationAssets]:
    """Load assets for a conversation, if any exist."""
    if not session_dir or not conv_id:
        return None
    p = _assets_path(session_dir, conv_id)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        return ConversationAssets.from_dict(data)
    except Exception as e:
        log.error(f"Failed to load assets from {p}: {e}")
        return None


def save_assets(session_dir: Path, conv_id: str, assets: ConversationAssets) -> None:
    """Persist assets to disk."""
    if not session_dir or not conv_id:
        return
    p = _assets_path(session_dir, conv_id)
    try:
        with open(p, "w") as f:
            json.dump(assets.to_dict(), f, indent=2)
    except Exception as e:
        log.error(f"Failed to save assets to {p}: {e}")


# ─── Tool Result Handlers ───

def _resolve_path(raw: str) -> str:
    """Expand ~ and resolve to absolute path."""
    try:
        return str(Path(os.path.expanduser(raw)).resolve())
    except Exception:
        return raw


def _filename(path: str) -> str:
    """Extract filename from path."""
    return Path(path).name if path else ""


def _file_suffix(path: str) -> str:
    """Extract file extension without dot."""
    s = Path(path).suffix
    return s.lstrip(".") if s else ""


def _domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        return urlparse(url).netloc or url[:60]
    except Exception:
        return url[:60]


def _handle_write_file(assets: ConversationAssets, args: dict, result: str):
    """Handle write_file result → file node."""
    m = re.search(r"Written (\d+) bytes to (.+?)(?:\n|$)", result)
    if not m:
        return
    size = int(m.group(1))
    path = _resolve_path(m.group(2).strip())
    name = _filename(path)

    existing = assets.find_node(NODE_TYPE_FILE, path)
    now = time.time()
    if existing:
        existing.updated_at = now
        existing.last_referenced_at = now
        existing.metadata["size_bytes"] = size
    else:
        assets.add_node(
            type=NODE_TYPE_FILE,
            title=name,
            body=path,
            metadata={"path": path, "size_bytes": size, "format": _file_suffix(path)},
            status=STATUS_CREATED,
            source_tool="write_file",
        )


def _handle_create_file(assets: ConversationAssets, args: dict, result: str):
    """Handle create_document / create_spreadsheet / create_pdf / create_presentation."""
    m = re.search(r"Created:\s*(.+?)(?:\n|$)", result)
    if not m:
        return
    path = _resolve_path(m.group(1).strip())
    name = _filename(path)

    existing = assets.find_node(NODE_TYPE_FILE, path)
    now = time.time()
    if existing:
        existing.updated_at = now
        existing.last_referenced_at = now
    else:
        assets.add_node(
            type=NODE_TYPE_FILE,
            title=name,
            body=path,
            metadata={"path": path, "format": _file_suffix(path)},
            status=STATUS_CREATED,
            source_tool="create_document",
        )


def _handle_edit_file(assets: ConversationAssets, args: dict, result: str):
    """Handle edit_file result → file node (update or create)."""
    m = re.search(r"Edit applied to (.+?) \(", result)
    if not m:
        return
    path = _resolve_path(m.group(1).strip())
    name = _filename(path)

    existing = assets.find_node(NODE_TYPE_FILE, path)
    now = time.time()
    if existing:
        existing.updated_at = now
        existing.last_referenced_at = now
    else:
        assets.add_node(
            type=NODE_TYPE_FILE,
            title=name,
            body=path,
            metadata={"path": path, "format": _file_suffix(path)},
            status=STATUS_CREATED,
            source_tool="edit_file",
        )


def _handle_read_file(assets: ConversationAssets, args: dict, result: str):
    """Handle read_file → track file reference."""
    raw_path = args.get("path", args.get("file_path", ""))
    if not raw_path:
        return
    path = _resolve_path(raw_path)
    name = _filename(path)

    # Skip directory listings
    if result.startswith("Directory listing"):
        return

    existing = assets.find_node(NODE_TYPE_FILE, path)
    now = time.time()
    if existing:
        existing.last_referenced_at = now
    else:
        assets.add_node(
            type=NODE_TYPE_FILE,
            title=name,
            body=path,
            metadata={"path": path, "format": _file_suffix(path)},
            status=STATUS_REFERENCED,
            source_tool="read_file",
        )


def _handle_web_search(assets: ConversationAssets, args: dict, result: str):
    """Handle web_search → query node + url nodes with edges."""
    query_text = args.get("query", "")
    if not query_text:
        return

    # Check for no results
    if "No results found" in result or not result.strip():
        assets.add_node(
            type=NODE_TYPE_QUERY,
            title=query_text[:80],
            body=query_text,
            metadata={"result_count": 0},
            status=STATUS_CREATED,
            source_tool="web_search",
        )
        return

    # Parse numbered results: "1. Title\n   URL: https://...\n   Snippet"
    pattern = re.compile(
        r"(\d+)\.\s+(.+?)\n\s+URL:\s+(\S+)\n\s*(.*?)(?=\n\n\d+\.|\Z)",
        re.DOTALL,
    )
    matches = pattern.findall(result)

    query_node = assets.add_node(
        type=NODE_TYPE_QUERY,
        title=query_text[:80],
        body=query_text,
        metadata={"result_count": len(matches)},
        status=STATUS_CREATED,
        source_tool="web_search",
    )

    for rank, title, url, snippet in matches:
        url = url.strip().rstrip(")")  # Clean trailing parens
        title = title.strip()

        # Dedup: reuse existing URL node
        existing = assets.find_node(NODE_TYPE_URL, url)
        if existing:
            existing.last_referenced_at = time.time()
            url_node = existing
        else:
            url_node = assets.add_node(
                type=NODE_TYPE_URL,
                title=title[:80] if title else _domain(url),
                body=url,
                metadata={"domain": _domain(url), "fetched": False},
                status=STATUS_NOT_FETCHED,
                source_tool="web_search",
            )

        # Edge: query → url
        assets.add_edge(
            from_id=query_node.id,
            to_id=url_node.id,
            kind=EDGE_SEARCH_RESULT,
            note=f"result #{rank}",
        )


def _handle_web_fetch(assets: ConversationAssets, args: dict, result: str):
    """Handle web_fetch → enrich url node + create fact node."""
    # Parse: "Source: URL\n\n<content>" or "Source: URL (via Wayback...)\n\n<content>"
    m = re.match(r"Source:\s+(\S+)(?:\s+\(via .+?\))?\s*\n\n(.+)", result, re.DOTALL)
    if not m:
        return

    url = m.group(1).strip()
    content = m.group(2).strip()

    now = time.time()

    # Find or create URL node and mark as fetched
    url_node = assets.find_node(NODE_TYPE_URL, url)
    if url_node:
        url_node.status = STATUS_FETCHED
        url_node.updated_at = now
        url_node.last_referenced_at = now
        url_node.metadata["fetched"] = True
        url_node.metadata["fetch_chars"] = len(content)
    else:
        url_node = assets.add_node(
            type=NODE_TYPE_URL,
            title=_domain(url),
            body=url,
            metadata={"domain": _domain(url), "fetched": True, "fetch_chars": len(content)},
            status=STATUS_FETCHED,
            source_tool="web_fetch",
        )

    # Create fact node with first ~500 chars of content
    fact_body = content[:500]
    # Use first sentence or first 80 chars as title
    first_line = content.split("\n")[0][:80] if content else "Web content"
    fact_node = assets.add_node(
        type=NODE_TYPE_FACT,
        title=first_line,
        body=fact_body,
        metadata={"source_url": url, "confidence": "extracted"},
        status=STATUS_CREATED,
        source_tool="web_fetch",
    )

    # Edge: url → fact (fetched_as)
    assets.add_edge(
        from_id=url_node.id,
        to_id=fact_node.id,
        kind=EDGE_FETCHED_AS,
    )


# ─── Dispatcher ───

_TOOL_HANDLERS = {
    "write_file": _handle_write_file,
    "create_document": _handle_create_file,
    "create_spreadsheet": _handle_create_file,
    "create_pdf": _handle_create_file,
    "create_presentation": _handle_create_file,
    "edit_file": _handle_edit_file,
    "read_file": _handle_read_file,
    "web_search": _handle_web_search,
    "web_fetch": _handle_web_fetch,
}


def process_tool_result(
    session_dir,
    conv_id: str,
    tool_name: str,
    args: dict,
    result: str,
):
    """
    Main entry point: called from conversation.py after every tool result.
    Dispatches to the appropriate handler to create/update asset nodes.
    Single load-mutate-save cycle per call.
    """
    if not session_dir or not conv_id:
        return
    if not result or result.startswith("ERROR"):
        return

    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return

    # Ensure args is a dict
    if not isinstance(args, dict):
        args = {}

    # Load or create
    assets = load_assets(session_dir, conv_id) or ConversationAssets()

    try:
        handler(assets, args, result)
    except Exception as e:
        log.debug(f"Asset handler for {tool_name} failed: {e}")
        return

    save_assets(session_dir, conv_id, assets)


# ─── System Prompt Rendering ───

def render_assets_for_system_prompt(
    assets: ConversationAssets,
    token_budget: int = RENDER_TOKEN_BUDGET,
) -> str:
    """
    Render a compact asset list for injection into the system prompt.
    Budget-capped. Returns empty string if no assets.
    """
    if not assets or not assets.nodes:
        return ""

    files = assets.nodes_by_type(NODE_TYPE_FILE)
    urls = assets.nodes_by_type(NODE_TYPE_URL)
    queries = assets.nodes_by_type(NODE_TYPE_QUERY)
    facts = assets.nodes_by_type(NODE_TYPE_FACT)

    if not files and not urls and not facts:
        return ""

    char_budget = token_budget * 4  # ~4 chars per token
    lines: list[str] = []

    lines.append("# Tracked Assets")
    lines.append(
        "Files, URLs, and facts from this conversation. These survive "
        "context compaction — use them as authoritative references."
    )
    lines.append("")

    # ── Files ──
    if files:
        lines.append(f"Files ({len(files)})")
        for f in sorted(files, key=lambda n: n.last_referenced_at, reverse=True):
            home = str(Path.home())
            display = f.body.replace(home, "~") if f.body.startswith(home) else f.body
            lines.append(f"  {f.id}  {f.title[:40]:<40}  {display[:60]:<60}  {f.status}")
        lines.append("")

    # ── URLs ──
    if urls:
        unfetched = sum(1 for u in urls if u.status == STATUS_NOT_FETCHED)
        label = f"URLs ({len(urls)}"
        if unfetched:
            label += f", {unfetched} NOT fetched"
        label += ")"
        lines.append(label)
        # Show fetched first, then unfetched
        sorted_urls = sorted(urls, key=lambda u: (u.status != STATUS_FETCHED, -u.last_referenced_at))
        for u in sorted_urls:
            mark = "fetched" if u.status == STATUS_FETCHED else "NOT fetched"
            display_url = u.body[:60] + ("..." if len(u.body) > 60 else "")
            lines.append(f"  {u.id}  {u.title[:40]:<40}  {display_url:<63}  {mark}")
        lines.append("")

    # ── Facts ──
    if facts:
        lines.append(f"Facts ({len(facts)})")
        for f in sorted(facts, key=lambda n: n.created_at, reverse=True):
            source = f.metadata.get("source_url", "")
            source_short = _domain(source) if source else ""
            body_preview = f.body[:80].replace("\n", " ")
            source_tag = f"  (from {source_short})" if source_short else ""
            lines.append(f"  {f.id}  {body_preview}{source_tag}")
        lines.append("")

    # ── Guidance ──
    if any(u.status == STATUS_NOT_FETCHED for u in urls):
        lines.append(
            "Unfetched URLs may contain useful information. Consider "
            "fetching them before writing your final output."
        )

    rendered = "\n".join(lines)

    # ── Truncation ──
    # If over budget, progressively reduce content
    if len(rendered) > char_budget:
        # Pass 1: truncate fact bodies to 40 chars
        for f in facts:
            if len(f.body) > 40:
                f.body = f.body[:37] + "..."
        rendered = "\n".join(lines)  # Re-render (titles from nodes are still in lines)

    if len(rendered) > char_budget:
        # Pass 2: keep only most recent 5 facts
        facts = sorted(facts, key=lambda n: n.created_at, reverse=True)[:5]

    if len(rendered) > char_budget:
        # Pass 3: drop unfetched URLs that only have search_result edges
        edge_targets = {e.to_id for e in assets.edges if e.kind != EDGE_SEARCH_RESULT}
        urls = [u for u in urls if u.status == STATUS_FETCHED or u.id in edge_targets]

    # Final re-render with truncated data (rebuild from scratch)
    if len(rendered) > char_budget:
        lines = ["# Tracked Assets", ""]
        if files:
            lines.append(f"Files ({len(files)})")
            for f in files[:10]:
                home = str(Path.home())
                display = f.body.replace(home, "~") if f.body.startswith(home) else f.body
                lines.append(f"  {f.id}  {f.title[:30]}  {display[:50]}  {f.status}")
            lines.append("")
        if urls:
            lines.append(f"URLs ({len(urls)})")
            for u in urls[:10]:
                mark = "fetched" if u.status == STATUS_FETCHED else "NOT fetched"
                lines.append(f"  {u.id}  {u.title[:30]}  {mark}")
            lines.append("")
        if facts:
            lines.append(f"Facts ({len(facts)})")
            for f in facts[:5]:
                lines.append(f"  {f.id}  {f.body[:40]}")
            lines.append("")
        rendered = "\n".join(lines)

    return rendered
