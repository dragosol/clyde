#!/usr/bin/env python3
"""
parse_photos.py — Scan a photo directory and extract metadata into state.json

Uses PIL/Pillow for EXIF when available, falls back to file metadata.

Usage:
  python3 parse_photos.py <input_path> <state_dir>

Creates:
  <state_dir>/state.json — master item list with all photos
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw",
    ".dng", ".orf", ".rw2", ".sr2",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".3gp",
}


def try_exif(path: Path) -> dict:
    """Try to extract EXIF data using Pillow. Returns {} on failure."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS

        img = Image.open(path)
        exif_raw = img._getexif()
        if not exif_raw:
            return {}

        exif = {}
        for tag_id, value in exif_raw.items():
            tag_name = TAGS.get(tag_id, str(tag_id))
            # Only keep useful string/number tags
            if isinstance(value, (str, int, float)):
                exif[tag_name] = value
            elif isinstance(value, bytes) and len(value) < 100:
                try:
                    exif[tag_name] = value.decode("utf-8", errors="replace")
                except Exception:
                    pass

        # Extract GPS if present
        gps_info = exif_raw.get(34853)  # GPSInfo tag
        if gps_info:
            gps = {}
            for key, val in gps_info.items():
                tag_name = GPSTAGS.get(key, str(key))
                if isinstance(val, (str, int, float)):
                    gps[tag_name] = val
            if gps:
                exif["GPSInfo"] = gps

        return exif
    except Exception:
        return {}


def scan_photos(root: Path) -> list[dict]:
    """Scan a directory for photos/videos and extract metadata."""
    all_extensions = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS
    items: list[dict] = []

    for entry in sorted(root.rglob("*")):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in all_extensions:
            continue
        if entry.name.startswith("."):
            continue

        try:
            stat = entry.stat()
        except (OSError, PermissionError):
            continue

        item = {
            "path": str(entry),
            "relative_path": str(entry.relative_to(root)),
            "name": entry.name,
            "extension": entry.suffix.lower(),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "created": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(),
            "media_type": "photo" if entry.suffix.lower() in PHOTO_EXTENSIONS else "video",
        }

        # Try EXIF for photos
        if entry.suffix.lower() in PHOTO_EXTENSIONS:
            exif = try_exif(entry)
            if exif:
                item["exif_date"] = exif.get("DateTimeOriginal", exif.get("DateTime", ""))
                item["camera_make"] = exif.get("Make", "")
                item["camera_model"] = exif.get("Model", "")
                item["gps"] = exif.get("GPSInfo", {})

        items.append(item)

    return items


def build_state(source_path: str, photos: list[dict]) -> dict:
    items = []
    for i, p in enumerate(photos):
        items.append({
            "id": i,
            "original": p,
            "classification": None,
            "status": "pending",
        })

    return {
        "job_id": str(uuid.uuid4()),
        "source_type": "photos",
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
        print("Usage: parse_photos.py <input_path> <state_dir>", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1]).expanduser().resolve()
    state_dir = Path(sys.argv[2]).expanduser().resolve()

    if not input_path.is_dir():
        print(f"ERROR: Not a directory: {input_path}", file=sys.stderr)
        sys.exit(1)

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "batches").mkdir(exist_ok=True)

    photos = scan_photos(input_path)
    state = build_state(str(input_path), photos)

    tmp_path = state_dir / "state.json.tmp"
    final_path = state_dir / "state.json"
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.rename(final_path)

    (state_dir / "taxonomy.json").write_text(
        json.dumps({"categories": {}, "version": 0}, indent=2), encoding="utf-8"
    )
    (state_dir / "pending_review.json").write_text(
        json.dumps({"items": []}, indent=2), encoding="utf-8"
    )

    # Summary
    photo_count = sum(1 for p in photos if p.get("media_type") == "photo")
    video_count = sum(1 for p in photos if p.get("media_type") == "video")
    exif_count = sum(1 for p in photos if p.get("exif_date"))

    print(f"OK: Found {len(photos)} media files ({photo_count} photos, {video_count} videos)")
    print(f"  EXIF data available: {exif_count} files")
    print(f"  State dir: {state_dir}")
    print(f"  Job ID: {state['job_id']}")


if __name__ == "__main__":
    main()
