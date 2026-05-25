#!/usr/bin/env python3
"""
extract_browser_bookmarks.py — Convert any browser's native bookmark file
to Netscape-HTML so `parse_bookmarks.py` can ingest it.

Handles three formats:
  - Chromium JSON (Chrome, Edge, Brave, Arc, Vivaldi, Opera, …)
  - Firefox-family `places.sqlite` (Firefox, Zen, LibreWolf, Waterfox, …)
  - Safari binary plist (`Bookmarks.plist`)

Usage:
  python3 extract_browser_bookmarks.py <input_file> <out_dir>

Writes:
  <out_dir>/<browser>_<basename>.html
"""
from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from html import escape


# ────────────────────────────────────────────────────────────
# Netscape HTML emitter (the format parse_bookmarks.py reads)
# ────────────────────────────────────────────────────────────

def _emit_netscape(roots: list[dict], out_path: Path) -> int:
    """`roots` is a list of {title, children: [...] | url}. Returns count."""
    out: list[str] = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<META HTTP-EQUIV=\"Content-Type\" CONTENT=\"text/html; charset=UTF-8\">",
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]
    count = 0

    def walk(node: dict, indent: int) -> None:
        nonlocal count
        pad = "    " * indent
        if "url" in node and node["url"]:
            title = escape(node.get("title") or node["url"])
            out.append(f'{pad}<DT><A HREF="{escape(node["url"])}">{title}</A>')
            count += 1
        else:
            title = escape(node.get("title") or "Untitled Folder")
            out.append(f"{pad}<DT><H3>{title}</H3>")
            out.append(f"{pad}<DL><p>")
            for child in node.get("children", []):
                walk(child, indent + 1)
            out.append(f"{pad}</DL><p>")

    for r in roots:
        walk(r, 1)
    out.append("</DL><p>")
    out_path.write_text("\n".join(out), encoding="utf-8")
    return count


# ────────────────────────────────────────────────────────────
# Format-specific parsers → list of root nodes
# ────────────────────────────────────────────────────────────

def _from_chromium_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    roots = data.get("roots") or {}
    out = []
    for label in ("bookmark_bar", "other", "synced"):
        node = roots.get(label)
        if not node:
            continue
        out.append(_chromium_node(node, fallback_title=label.replace("_", " ").title()))
    return out


def _chromium_node(node: dict, fallback_title: str = "") -> dict:
    if node.get("type") == "url":
        return {"title": node.get("name") or "", "url": node.get("url") or ""}
    children = [_chromium_node(c) for c in node.get("children", [])]
    return {"title": node.get("name") or fallback_title, "children": children}


def _from_places_sqlite(path: Path) -> list[dict]:
    """Walk Firefox-family moz_bookmarks tree.

    The browser is usually running and holds an exclusive WAL lock on the
    live places.sqlite. Even read-only opens fail. Copy the file (and any
    sibling -shm/-wal) to /tmp first and read the snapshot.
    """
    import shutil
    import tempfile
    snap_dir = Path(tempfile.mkdtemp(prefix="places_snap_"))
    snap = snap_dir / "places.sqlite"
    shutil.copy2(path, snap)
    for sibling in (path.with_suffix(".sqlite-wal"),
                    path.with_suffix(".sqlite-shm")):
        if sibling.exists():
            try:
                shutil.copy2(sibling, snap_dir / sibling.name)
            except Exception:
                pass
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT b.id, b.parent, b.type, b.title, p.url "
        "FROM moz_bookmarks b "
        "LEFT JOIN moz_places p ON b.fk = p.id "
        "ORDER BY b.parent, b.position"
    ).fetchall()
    conn.close()

    # type 1 = bookmark, type 2 = folder
    by_id: dict[int, dict] = {}
    children_of: dict[int, list[int]] = {}
    for bid, parent, btype, title, url in rows:
        node = {"id": bid, "parent": parent, "type": btype,
                "title": title or "", "url": url or ""}
        by_id[bid] = node
        children_of.setdefault(parent, []).append(bid)

    def to_dict(bid: int) -> dict | None:
        n = by_id.get(bid)
        if n is None:
            return None
        if n["type"] == 1:
            if not n["url"]:
                return None
            return {"title": n["title"] or n["url"], "url": n["url"]}
        # folder
        kids = []
        for cid in children_of.get(bid, []):
            d = to_dict(cid)
            if d is not None:
                kids.append(d)
        return {"title": n["title"] or "Untitled Folder", "children": kids}

    # Top-level Firefox roots: 1 = root, 2 = menu, 3 = toolbar, 5 = unfiled, 6 = mobile
    out = []
    for root_id in (2, 3, 5, 6):
        d = to_dict(root_id)
        if d and d.get("children"):
            out.append(d)
    return out


def _from_orion_favourites(path: Path) -> list[dict]:
    """Orion (Kagi, Webkit-based) stores bookmarks as a flat dict plist
    under `Defaults/bk_<N>/favourites.plist`. Each entry has:
      id, parentId, title, type ('folder' | 'url'), url (for leaves),
      index (sort order among siblings).
    Rebuild the tree from id/parentId and emit.
    """
    with open(path, "rb") as f:
        data = plistlib.load(f)
    # Flat map {id: node_dict}
    by_id: dict = {}
    children_of: dict = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        node_id = v.get("id", k)
        parent = v.get("parentId", None)
        by_id[str(node_id)] = v
        children_of.setdefault(str(parent) if parent is not None else None, []).append(str(node_id))

    def walk(node_id: str) -> dict | None:
        n = by_id.get(node_id)
        if n is None:
            return None
        kind = n.get("type")
        # Orion uses `"bookmark"` (not `"url"`) for leaf entries.
        if kind in ("bookmark", "url"):
            url = n.get("url") or ""
            if not url:
                return None
            return {"title": n.get("title") or url, "url": url}
        # folder
        kids_ids = sorted(
            children_of.get(node_id, []),
            key=lambda k: by_id.get(k, {}).get("index", 0),
        )
        kids = [walk(k) for k in kids_ids]
        kids = [k for k in kids if k is not None]
        return {"title": n.get("title") or "Untitled", "children": kids}

    # Top-level roots = entries whose parentId is None / missing.
    roots_ids = children_of.get(None, [])
    out: list[dict] = []
    for rid in sorted(roots_ids, key=lambda k: by_id.get(k, {}).get("index", 0)):
        d = walk(rid)
        if d and (d.get("children") or d.get("url")):
            out.append(d)
    return out


def _from_ddg_sqlite(path: Path) -> list[dict]:
    """DuckDuckGo Privacy Browser (macOS) — bookmarks in `BOOKMARK` /
    `FOLDER` rows of a Core Data-backed sqlite. Schema varies across
    releases; this is a best-effort walk of ZBOOKMARKENTITY / ZFOLDERENTITY.
    """
    import shutil, tempfile
    snap_dir = Path(tempfile.mkdtemp(prefix="ddg_snap_"))
    snap = snap_dir / "Bookmarks.db"
    shutil.copy2(path, snap)
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    cur = conn.cursor()
    # List tables to figure out schema on this install.
    tbls = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    rows = []
    if "ZBOOKMARKENTITY" in tbls:
        rows = cur.execute(
            "SELECT Z_PK, ZPARENTFOLDER, ZISFOLDER, ZTITLE, ZURL "
            "FROM ZBOOKMARKENTITY ORDER BY ZPARENTFOLDER"
        ).fetchall()
    conn.close()
    if not rows:
        return []
    by_id, children_of = {}, {}
    for pk, parent, is_folder, title, url in rows:
        by_id[pk] = {"parent": parent, "is_folder": bool(is_folder),
                     "title": title or "", "url": url or ""}
        children_of.setdefault(parent, []).append(pk)

    def walk(pk):
        n = by_id.get(pk)
        if n is None:
            return None
        if not n["is_folder"]:
            if not n["url"]:
                return None
            return {"title": n["title"] or n["url"], "url": n["url"]}
        kids = [walk(c) for c in children_of.get(pk, [])]
        return {"title": n["title"] or "Untitled",
                "children": [k for k in kids if k is not None]}

    # Top-level = entries with no ZPARENTFOLDER
    out = []
    for pk in children_of.get(None, []):
        d = walk(pk)
        if d:
            out.append(d)
    return out


def _from_safari_plist(path: Path) -> list[dict]:
    """Safari Bookmarks.plist is a binary plist with a tree structure.

    macOS protects `~/Library/Safari/` behind Full Disk Access (TCC).
    Without FDA the agent gets PermissionError. Surface that with a clear
    message so the model can tell the user how to grant access (System
    Settings → Privacy & Security → Full Disk Access → add Terminal/IDE).
    """
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except PermissionError:
        raise PermissionError(
            f"Cannot read {path}: macOS Full Disk Access required. "
            "Grant FDA to the host process (Terminal / Xcode / ClydeEngine) "
            "in System Settings → Privacy & Security → Full Disk Access. "
            "Workaround: in Safari → File → Export Bookmarks… and pass the "
            "exported HTML file to this script instead."
        )

    def walk(node: dict) -> dict | None:
        wstype = node.get("WebBookmarkType")
        if wstype == "WebBookmarkTypeLeaf":
            uri = node.get("URLString") or ""
            uri_dict = node.get("URIDictionary") or {}
            title = uri_dict.get("title") or uri
            if not uri:
                return None
            return {"title": title, "url": uri}
        if wstype in ("WebBookmarkTypeList", None):  # None = root
            kids = []
            for child in node.get("Children", []):
                d = walk(child)
                if d is not None:
                    kids.append(d)
            return {"title": node.get("Title") or "Bookmarks", "children": kids}
        return None

    root = walk(data)
    if root and root.get("children"):
        return root["children"]
    return []


# ────────────────────────────────────────────────────────────
# Detector + dispatcher
# ────────────────────────────────────────────────────────────

def detect_format(path: Path) -> str:
    """Order matters — most specific first. Each branch matches by
    substring rather than exact name so the bookmark_extract_all copies
    (which prefix with `<parent>__`) still classify correctly."""
    name_l = path.name.lower()
    suffix = path.suffix.lower()
    if "favourites.plist" in name_l or "favorites.plist" in name_l:
        return "orion"
    if "bookmarks.db" in name_l:
        return "ddg"
    if "places.sqlite" in name_l:
        return "places"
    if name_l == "bookmarks" and suffix == "":
        return "chromium"
    if name_l.endswith("__bookmarks") and suffix == "":
        return "chromium"  # bookmark_extract_all copy
    if suffix == ".jsonlz4":
        return "firefox-jsonlz4"
    if "bookmarks.plist" in name_l:
        return "safari"
    if suffix == ".sqlite":
        # generic sqlite — assume Firefox-family if not caught above
        return "places"
    if suffix == ".plist":
        return "safari"
    if suffix == ".json":
        return "chromium"
    if suffix in (".html", ".htm"):
        return "netscape"
    return "unknown"


def _auto_discover_all() -> int:
    """No-args mode: locate every browser's bookmark file on this Mac and
    extract them all to /tmp/clyde-bookmarks-<ts>/. Mirrors the agent's
    bookmark_extract_all tool so the model can also reach the same outcome
    via bash."""
    import time as _time
    out = Path(f"/tmp/clyde-bookmarks-{int(_time.time())}")
    out.mkdir(parents=True, exist_ok=True)
    print(f"# Auto-discover mode → output dir: {out}")
    home = Path.home()

    # Same locator logic as tools._exec_bookmark_locator (kept inline so this
    # script stays self-contained for direct bash use).
    chromium = [
        ("Chrome", home / "Library/Application Support/Google/Chrome"),
        ("Edge", home / "Library/Application Support/Microsoft Edge"),
        ("Brave", home / "Library/Application Support/BraveSoftware/Brave-Browser"),
        ("Arc", home / "Library/Application Support/Arc/User Data"),
        ("Vivaldi", home / "Library/Application Support/Vivaldi"),
        ("Opera", home / "Library/Application Support/com.operasoftware.Opera"),
    ]
    firefox = [
        ("Firefox", home / "Library/Application Support/Firefox/Profiles"),
        ("Zen", home / "Library/Application Support/zen/Profiles"),
        ("LibreWolf", home / "Library/Application Support/LibreWolf/Profiles"),
        ("Waterfox", home / "Library/Application Support/Waterfox/Profiles"),
    ]

    paths: list[Path] = []
    for _, root in chromium:
        if not root.exists():
            continue
        for prof in sorted(root.iterdir()):
            bm = prof / "Bookmarks"
            if bm.exists():
                paths.append(bm)
    for _, root in firefox:
        if not root.exists():
            continue
        for prof in sorted(root.iterdir()):
            ps = prof / "places.sqlite"
            if ps.exists():
                paths.append(ps)
    for s in [
        home / "Library/Safari/Bookmarks.plist",
        home / "Library/Containers/com.apple.Safari/Data/Library/Safari/Bookmarks.plist",
    ]:
        if s.exists():
            paths.append(s)

    if not paths:
        print("No bookmark files found.", file=sys.stderr)
        return 1

    rc = 0
    for src in paths:
        try:
            sub_rc = main(["", str(src), str(out)])
            if sub_rc != 0:
                rc = sub_rc
        except Exception as e:
            print(f"  ERROR {src}: {e}", file=sys.stderr)
            rc = 1
    print(f"# Done — output dir: {out}")
    return rc


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        if len(argv) >= 1:
            # No args = auto-discover mode (Phase 3 — handles all browsers).
            return _auto_discover_all()
        print("usage: extract_browser_bookmarks.py <input_file> <out_dir>",
              file=sys.stderr)
        return 1
    src = Path(argv[1]).expanduser().resolve()
    out_dir = Path(argv[2]).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 1

    fmt = detect_format(src)
    # Browser tag derived from path so the output filename is informative.
    parts = src.parts
    if "Application Support" in parts:
        idx = parts.index("Application Support")
        browser_tag = parts[idx + 1] if idx + 1 < len(parts) else "browser"
    elif "Safari" in parts:
        browser_tag = "Safari"
    else:
        browser_tag = src.stem.lower()
    profile_tag = src.parent.name.split("/")[-1]
    # Sanitize: spaces, parens, and other shell-hostile chars → underscores.
    # Bash chokes on unquoted `(release)` in paths the model writes; cleaner
    # to never produce them in the first place.
    import re as _re
    profile_tag = _re.sub(r"[^A-Za-z0-9._-]+", "_", profile_tag).strip("_")
    browser_tag = _re.sub(r"[^A-Za-z0-9._-]+", "_", browser_tag).strip("_")
    out_path = out_dir / f"{browser_tag}_{profile_tag}.html"

    try:
        if fmt == "chromium":
            roots = _from_chromium_json(src)
        elif fmt == "places":
            roots = _from_places_sqlite(src)
        elif fmt == "safari":
            roots = _from_safari_plist(src)
        elif fmt == "orion":
            roots = _from_orion_favourites(src)
        elif fmt == "ddg":
            roots = _from_ddg_sqlite(src)
        elif fmt == "firefox-jsonlz4":
            # Decompress mozLz40 then load JSON.
            import struct
            buf = src.read_bytes()
            assert buf[:8] == b"mozLz40\0", "not a mozLz40 file"
            try:
                import lz4.block as lz4_block
            except ImportError:
                print("ERROR: needs `lz4` python pkg for jsonlz4 (pip install lz4)",
                      file=sys.stderr)
                return 1
            (orig_size,) = struct.unpack("<I", buf[8:12])
            data = lz4_block.decompress(buf[12:], uncompressed_size=orig_size)
            payload = json.loads(data)
            # Same structure as places.sqlite-derived nodes; reuse chromium walker.
            roots = [_jsonlz4_node(payload)]
        elif fmt == "netscape":
            # Already in target format — copy through.
            out_path.write_text(src.read_text())
            print(str(out_path))
            return 0
        else:
            print(f"ERROR: unrecognised format for {src}", file=sys.stderr)
            return 2
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    count = _emit_netscape(roots, out_path)
    print(f"{count} bookmarks → {out_path}")
    return 0


def _jsonlz4_node(node: dict) -> dict:
    """Walk a Firefox jsonlz4 backup node tree (same shape as places dump)."""
    if node.get("type") == "text/x-moz-place":
        return {"title": node.get("title") or node.get("uri") or "", "url": node.get("uri") or ""}
    kids = [_jsonlz4_node(c) for c in node.get("children", [])]
    return {"title": node.get("title") or "Bookmarks", "children": kids}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
