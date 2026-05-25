#!/usr/bin/env python3
"""
enrich_bookmarks.py — Fetch metadata for ambiguous bookmarks via Firecrawl.

Identifies bookmarks with ambiguous titles (short, generic, video IDs, etc.)
and fetches page metadata (title, description, OpenGraph tags) to improve
classification quality. Does NOT render full pages — uses scrape endpoint
with onlyMainContent for speed.

Usage:
  python3 enrich_bookmarks.py <state_dir> [--firecrawl-url http://localhost:3002] [--max-items 100] [--timeout 8]

Writes enriched metadata back to state.json items[].enriched = {
  "title": "...",
  "description": "...",
  "og_title": "...",
  "og_description": "...",
  "source": "firecrawl"
}
Items that fail to fetch or timeout get enriched.source = "failed".
"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


# ─── Ambiguity Detection ─────────────────────────────────────────────

# Generic/uninformative titles that don't help with classification
GENERIC_TITLES = {
    "home", "index", "login", "sign in", "sign up", "register",
    "dashboard", "calendar", "settings", "profile", "about",
    "contact", "search", "404", "not found", "untitled",
    "new tab", "page", "document", "welcome", "loading",
}

# Patterns that indicate a title is just a URL or domain
URL_AS_TITLE_PATTERNS = [
    r'^https?://',           # URL as title
    r'^www\.',               # www.domain.com as title
    r'^\d+\.\d+\.\d+\.\d+', # IP address
]

# Patterns for video IDs, hashes, etc.
HASH_PATTERNS = [
    r'^[a-zA-Z0-9_-]{8,12}$',  # YouTube-style video ID
    r'^[a-f0-9]{32,}$',         # MD5/SHA hash
]


def is_ambiguous(title: str, url: str) -> tuple[bool, str]:
    """Check if a bookmark needs enrichment. Returns (is_ambiguous, reason)."""
    t = title.strip().lower()

    # Empty or very short title
    if len(t) < 5:
        return True, "title_too_short"

    # Generic titles
    if t in GENERIC_TITLES:
        return True, "generic_title"

    # Title is just "From domain.com" or similar
    if t.startswith("from ") and len(t) < 30:
        return True, "from_domain"

    # Title looks like a URL
    for pat in URL_AS_TITLE_PATTERNS:
        if re.match(pat, title.strip()):
            return True, "url_as_title"

    # Title is a hash or video ID
    for pat in HASH_PATTERNS:
        if re.match(pat, t):
            return True, "hash_or_id"

    # Title under 20 chars and doesn't contain meaningful words
    if len(t) < 20:
        # Check if it's just a single word or very terse
        words = t.split()
        if len(words) <= 2:
            return True, "title_too_terse"

    # Shortened URLs (even if title looks OK, the URL gives no classification signal)
    domain = urlparse(url).netloc.lower()
    shortened_domains = {
        "bit.ly", "t.co", "goo.gl", "tinyurl.com", "ow.ly",
        "buff.ly", "is.gd", "v.gd", "soo.gd", "s.id",
    }
    if domain in shortened_domains:
        return True, "shortened_url"

    # YouTube/Vimeo with generic title
    if ("youtube.com" in domain or "youtu.be" in domain) and len(t) < 25:
        return True, "short_video_title"

    return False, ""


# ─── Firecrawl Fetching ──────────────────────────────────────────────

def fetch_metadata(url: str, firecrawl_url: str, timeout: float) -> dict | None:
    """Fetch page metadata via Firecrawl scrape endpoint."""
    try:
        resp = httpx.post(
            f"{firecrawl_url}/v1/scrape",
            json={
                "url": url,
                "formats": ["metadata"],
                "onlyMainContent": True,
                "timeout": int(timeout * 1000),
            },
            timeout=httpx.Timeout(connect=5.0, read=timeout + 2, write=5.0, pool=5.0),
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        # Firecrawl v1 response: data.metadata contains title, description, ogTitle, etc.
        meta = data.get("data", {}).get("metadata", {})
        if not meta:
            # Try alternate shape
            meta = data.get("metadata", {})

        return {
            "title": meta.get("title", ""),
            "description": meta.get("description", ""),
            "og_title": meta.get("ogTitle", meta.get("og:title", "")),
            "og_description": meta.get("ogDescription", meta.get("og:description", "")),
            "source": "firecrawl",
        }
    except Exception:
        return None


# ─── Main ─────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Enrich ambiguous bookmarks via Firecrawl")
    parser.add_argument("state_dir", type=str, help="Path to state directory")
    parser.add_argument("--firecrawl-url", default="http://localhost:3002", help="Firecrawl base URL")
    parser.add_argument("--max-items", type=int, default=100, help="Max items to enrich")
    parser.add_argument("--timeout", type=float, default=8.0, help="Per-request timeout (seconds)")
    args = parser.parse_args()

    state_dir = Path(args.state_dir).expanduser().resolve()
    state_path = state_dir / "state.json"
    if not state_path.exists():
        print(f"ERROR: No state.json at {state_dir}", file=sys.stderr)
        sys.exit(1)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    items = state.get("items", [])

    # Identify ambiguous items
    ambiguous = []
    for i, item in enumerate(items):
        if item.get("enriched"):
            continue  # Already enriched
        orig = item.get("original", {})
        title = orig.get("title", "")
        url = orig.get("url", "")
        is_amb, reason = is_ambiguous(title, url)
        if is_amb:
            ambiguous.append((i, reason))

    print(f"Found {len(ambiguous)} ambiguous items out of {len(items)} total")
    if not ambiguous:
        print("Nothing to enrich.")
        return

    # Cap to max_items
    to_enrich = ambiguous[:args.max_items]
    print(f"Enriching {len(to_enrich)} items (max={args.max_items})...")

    # Check Firecrawl health
    try:
        health = httpx.get(f"{args.firecrawl_url}/health", timeout=5.0)
        if health.status_code != 200:
            print(f"WARNING: Firecrawl health check failed (status {health.status_code})")
    except Exception as e:
        print(f"WARNING: Firecrawl not reachable at {args.firecrawl_url}: {e}")
        print("Proceeding anyway — failures will be marked as enriched.source='failed'")

    enriched_count = 0
    failed_count = 0
    t0 = time.time()

    for idx, reason in to_enrich:
        item = items[idx]
        url = item.get("original", {}).get("url", "")
        old_title = item.get("original", {}).get("title", "")

        meta = fetch_metadata(url, args.firecrawl_url, args.timeout)
        if meta and (meta.get("title") or meta.get("og_title") or meta.get("description")):
            item["enriched"] = meta
            enriched_count += 1
            new_title = meta.get("og_title") or meta.get("title") or ""
            if new_title and new_title != old_title:
                print(f"  [{idx}] {reason}: '{old_title[:40]}' → '{new_title[:60]}'")
            else:
                print(f"  [{idx}] {reason}: got description for '{old_title[:40]}'")
        else:
            item["enriched"] = {"source": "failed", "reason": reason}
            failed_count += 1
            print(f"  [{idx}] {reason}: FAILED to fetch '{url[:60]}'")

    # Save back
    state["items"] = items
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(state_path)

    elapsed = time.time() - t0
    print(f"\nDone: {enriched_count} enriched, {failed_count} failed, {elapsed:.1f}s elapsed")
    print(f"State saved to {state_path}")


if __name__ == "__main__":
    main()
