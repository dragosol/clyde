# Bulk Organizer Skill

You are in **Organizer mode** — processing a large collection of items through
a crash-proof, multi-phase pipeline. The asset graph is your state machine.

---

## Golden Rule

**Every state change is written to disk AND the graph before you proceed.**
If you crash mid-batch, the worst case is re-processing one batch.

---

## Recovery Protocol (EVERY turn start)

```
1. Call organize_progress
2. If NO_JOB    → start new job (Phase 1)
3. If RECOVERY  → re-create graph nodes from disk state, then continue
4. Otherwise    → read the suggested NEXT ACTION and execute it
```

This is non-negotiable. After compaction, you will lose conversational
context. The graph + disk state is your memory.

---

## Phase 1: INGEST

**Goal:** Parse input into a normalized item list.

### Phase 1a (bookmarks only): LOCATE then EXTRACT

When the user says "organize my bookmarks" without naming a path, DO THIS:

1. **Call `bookmark_extract_all`** (one call, optional `out_dir`) — returns a
   working dir with every browser's bookmark file copied to
   `<out_dir>/originals/` AND converted to Netscape HTML in
   `<out_dir>/extracted_html/`. Skips anything behind Full Disk Access with
   a clear message. Replaces running `bookmark_locator` + bash `cp` + bash
   `python3 extract_browser_bookmarks.py` manually — it's one round-trip
   vs ten.

   If for some reason that tool is unavailable, fall back to calling
   `bookmark_locator` (no args) then running
   `python3 ~/.clyde/agent/skills/bulk-organizer/scripts/extract_browser_bookmarks.py`
   with NO args (auto-discover mode — same pipeline, but via bash).

2. **Do NOT try to parse the jsonlz4 backup separately.** Each Firefox-family
   browser's `places.sqlite` already contains the full live bookmark set.
   The jsonlz4 backup is redundant. Move straight to Phase 1b.

3. **Safari needs Full Disk Access**. If bookmark_extract_all skips Safari
   with a PermissionError about FDA, tell the user what to do
   (System Settings → Privacy & Security → Full Disk Access → add the host
   process), OR ask the user to export from Safari (File → Export
   Bookmarks…) and point `parse_bookmarks.py` at the exported HTML.

4. **Then proceed to Phase 1b (parse to state.json)** below.

#### 2026 Browser → Bookmark Path Reference

Canonical macOS locations (bookmark_locator/extract_all check all of these):

| Family | Browser | Path | Format |
|---|---|---|---|
| Webkit | Safari | `~/Library/Safari/Bookmarks.plist` (also under `~/Library/Containers/com.apple.Safari/Data/Library/Safari/` and `~/Library/Group Containers/group.com.apple.Safari/Library/Safari/`) | binary plist (FDA required) |
| Webkit | Orion (Kagi) | `~/Library/Application Support/Orion/Defaults/bk_<N>/favourites.plist` | plist (flat dict, NSDate objects) |
| Webkit | DuckDuckGo | `~/Library/Containers/com.duckduckgo.macos.browser/Data/Library/Application Support/DuckDuckGo/Bookmarks.db` | sqlite (Core Data) |
| Chromium | Chrome / Chrome Beta / Canary / Dev | `~/Library/Application Support/Google/Chrome{,* Beta,* Canary,* Dev}/<Profile>/Bookmarks` | JSON |
| Chromium | Chromium (upstream) | `~/Library/Application Support/Chromium/<Profile>/Bookmarks` | JSON |
| Chromium | Edge / Beta / Dev | `~/Library/Application Support/Microsoft Edge{,* Beta,* Dev}/<Profile>/Bookmarks` | JSON |
| Chromium | Brave / Beta / Nightly | `~/Library/Application Support/BraveSoftware/Brave-Browser{,*-Beta,*-Nightly}/<Profile>/Bookmarks` | JSON |
| Chromium | Arc | `~/Library/Application Support/Arc/User Data/<Profile>/Bookmarks` | JSON |
| Chromium | Dia (Browser Company AI) | `~/Library/Application Support/Dia/User Data/<Profile>/Bookmarks` | JSON |
| Chromium | Vivaldi | `~/Library/Application Support/Vivaldi/<Profile>/Bookmarks` | JSON |
| Chromium | Opera / GX / Air | `~/Library/Application Support/com.operasoftware.Opera{,GX,Air}/Bookmarks` | JSON |
| Chromium | Comet (Perplexity AI) | `~/Library/Application Support/Comet/<Profile>/Bookmarks` | JSON |
| Chromium | Sidekick, Wavebox, Yandex, Whale, Cromite, Ungoogled Chromium | `~/Library/Application Support/<vendor>/…/<Profile>/Bookmarks` | JSON |
| Firefox | Firefox / Nightly / Dev | `~/Library/Application Support/Firefox{, Nightly, Developer Edition}/Profiles/*.default*/places.sqlite` | sqlite |
| Firefox | Zen | `~/Library/Application Support/zen/Profiles/*.Default*/places.sqlite` | sqlite |
| Firefox | LibreWolf, Waterfox, Floorp, Pale Moon, SeaMonkey | `~/Library/Application Support/<vendor>/Profiles/*.default*/places.sqlite` | sqlite |
| Firefox | Tor Browser / Mullvad Browser | `~/Library/Application Support/TorBrowser-Data/Browser/*.default*/places.sqlite` (or `MullvadBrowser/Profiles/…`) | sqlite |

**Parsing notes** (extract_browser_bookmarks.py handles all of these — do
not re-implement):

- **Chromium JSON**: `roots.bookmark_bar / other / synced`, each with a `children` tree. Entry `type`: `"url"` or `"folder"`.
- **Firefox `places.sqlite`**: tables `moz_bookmarks` (id, parent, type, title, fk) joined to `moz_places` (id, url). Types: `1`=bookmark, `2`=folder. Roots: id `2`=menu, `3`=toolbar, `5`=unfiled, `6`=mobile. **Copy the file first** — live browsers hold a WAL lock even under `mode=ro`.
- **Safari Bookmarks.plist**: binary plist tree with `WebBookmarkType` = `WebBookmarkTypeLeaf` (URL) / `WebBookmarkTypeList` (folder). Keys: `URIDictionary.title`, `URLString`, `Children`.
- **Orion `favourites.plist`**: flat dict `{id: {id, parentId, type, title, url?, index}}`. Type is `"bookmark"` (leaf) or `"folder"`. NSDate objects in `lastSynced` break `plutil -convert json`; use `plistlib`.
- **DuckDuckGo `Bookmarks.db`**: Core Data sqlite; `ZBOOKMARKENTITY` with `Z_PK`, `ZPARENTFOLDER`, `ZISFOLDER`, `ZTITLE`, `ZURL`.

### Phase 1b: PARSE to state.json

1. Detect input type (bookmark HTML, filesystem path, photo directory)
2. Run the appropriate parser script via `bash`:
   - Bookmarks: `python3 ~/.clyde/skills/bulk-organizer/scripts/parse_bookmarks.py <input_path> <state_dir>`
   - Filesystem: `python3 ~/.clyde/skills/bulk-organizer/scripts/parse_filesystem.py <input_path> <state_dir>`
   - Photos: `python3 ~/.clyde/skills/bulk-organizer/scripts/parse_photos.py <input_path> <state_dir>`
3. Parser writes `state.json` with all items in `"pending"` status
4. Call `organize_update_graph` with action=`create_task`:
   - title: "Organize {count} {type}"
   - state_dir: path to `.organize-state/`
   - total_items: item count
5. Call `organize_update_graph` with action=`create_phase`:
   - title: "Phase: Ingest"
   - phase_name: "ingest"
   - status: "done"
   - task_node_id: from step 4
6. Report count to user

**State dir location:** Create `.organize-state/` inside the user's working
area (e.g., next to the input file or in a designated output folder).

---

## Phase 2: TAXONOMY DISCOVERY

**Goal:** Establish a HIERARCHICAL taxonomy from a small sample.

1. Read `state.json` — take items 0–24 + a random sample of 25 more
2. Load them into your context
3. Propose a **two-level hierarchy**:
   - **Target: 15–20 top-level folders**, each containing 2–8 subfolders
   - Top-level folders are broad themes (e.g. "Technology", "Entertainment", "Spirituality")
   - Subfolders are specific topics (e.g. "Technology/Linux", "Technology/AI", "Entertainment/Anime")
   - **Hard ceiling: ≤40 leaf categories total.** If you exceed 40, merge the smallest.
4. Present the taxonomy to the user via `ask_user`:
   - Show the TREE structure with parent → children relationships
   - Then call ask_user with HIGH-LEVEL grouping choices (not individual categories)
5. If user wants adjustments → ask for specifics, iterate
6. Write `taxonomy.json` with the approved categories. **REQUIRED FIELDS:**
   ```json
   {
     "categories": {
       "Technology": {
         "name": "Technology",
         "description": "Software, hardware, development tools",
         "parent": null,
         "subcategories": ["Technology/Linux", "Technology/AI", "Technology/Gaming"],
         "item_count": 0,
         "examples": ["github.com", "stackoverflow.com"]
       },
       "Technology/Linux": {
         "name": "Linux & Open Source",
         "description": "Linux distros, CLI tools, open source projects",
         "parent": "Technology",
         "subcategories": [],
         "item_count": 0,
         "examples": ["fedoraproject.org"]
       }
     }
   }
   ```
7. Create graph nodes AND hierarchy edges:
   - `create_phase`: "Phase: Taxonomy" (status: done)
   - `create_category` for each approved category
   - `set_parent_bulk` with mappings for all parent→child relationships
   - `update_task`: phase → "taxonomy" (or "enrich" if ready)

**This is a mandatory pause point.** Never skip user approval.

---

## Phase 2.5: ENRICH (Bookmarks only)

**Goal:** Fetch metadata for ambiguous bookmarks to improve classification.

1. Run the enrichment script:
   ```
   python3 ~/.clyde/skills/bulk-organizer/scripts/enrich_bookmarks.py <state_dir>
   ```
2. This will:
   - Identify bookmarks with ambiguous titles (short, generic, URL-as-title, etc.)
   - Fetch page metadata via Firecrawl at localhost:3002 (title, description, OG tags)
   - Write `enriched` field back to each item in state.json
   - Skip dead links gracefully (marks them as `enriched.source = "failed"`)
3. Report enrichment stats to user
4. Update graph: `create_phase` "Phase: Enrich" (status: done)

**Enriched metadata appears automatically in `organize_batch_read` output**
as `[enriched: ...]` and `[desc: ...]` annotations after the title/URL.
Use these when classifying — they often resolve ambiguous titles.

---

## Phase 3: CLASSIFY (Adaptive Batching)

**Goal:** Process all remaining items in batches.

**CRITICAL: YOU are the classifier.** NEVER write external Python scripts
to classify items. No classify_bookmarks.py, no scripts calling the LLM API.
External classification scripts crash the backend. Instead:
1. Call `organize_batch_read` to get a batch
2. Classify each item yourself by examining its title and URL
3. Call `organize_batch_write` with your classifications
4. Follow the NEXT action returned by batch_write

### Batch sizing
Batches are limited to 15 items (enforced by organize_batch_read/write).

### Per-batch workflow

1. Call `organize_batch_read` with start index and count (max 15)
2. Examine each item's title, URL, and metadata
3. Classify each item yourself (you ARE the classifier):
   - Assign `category` (must be a key from taxonomy)
   - Assign `subcategory` if applicable
   - Assign `confidence` (0.0–1.0)
   - Write brief `reasoning`
   - If confidence < 0.6 → mark status as `"uncertain"`
5. Write results back to `state.json`:
   - Update each item's `classification` and `status`
   - Update `progress.next_batch_start`, `progress.total_classified`, etc.
   - **Atomic write:** write to `state.json.tmp` then rename
6. Save batch audit trail: `batches/batch_NNN.json`
7. Update graph:
   - `create_batch`: record what happened
   - `update_task`: new classified/uncertain counts
   - `create_category` if new categories emerged
8. If new categories → also update `taxonomy.json`

### Ambiguity pause (threshold: 25 uncertain items)

When uncertain count reaches 25:
1. Collect representative uncertain items (5–8 examples)
2. Present to user via `ask_user`:
   - Show each item with what makes it ambiguous
   - Offer: "Tell me categories", "Create 'Misc' bucket", "Best guess"
3. Resolve based on user choice
4. Clear `pending_review.json`
5. Continue classification

### Category explosion guard (threshold: 40 categories)

If categories exceed 40:
1. Pause and suggest consolidation
2. Show smallest categories (< 3 items)
3. Propose merges or a hierarchy
4. Get user approval before continuing

---

## Phase 4: REVIEW

**Goal:** Final quality check.

1. Generate summary:
   - Category distribution (name, count, percentage)
   - Any remaining uncertain items
   - Suggested merges for tiny categories (< 3 items)
2. Present to user via `ask_user`
3. If user wants changes → apply them, loop back
4. Update graph: phase → "review" (done)

---

## Phase 5: EXECUTE (Hybrid)

**Goal:** Apply the organization.

1. Generate execution plan based on source type:
   - **Bookmarks:** New bookmark HTML with folder hierarchy
   - **Files/Documents:** Shell script creating directories + moving files
   - **Photos:** Rename/tag script
2. Save plan to `execution_plan.json` + `execution_plan.sh`
3. Present plan to user for review
4. If user approves:
   - For files in workspace: execute via `bash`
   - For Mac files: provide script path for user to run, or use computer-use
5. Update graph: task status → "done"

---

## Disk State Reference

```
.organize-state/
├── state.json            # Master: all items + classifications + progress
├── taxonomy.json         # Category hierarchy
├── pending_review.json   # Items awaiting user decision
├── execution_plan.sh     # Generated script
├── execution_plan.json   # Structured plan
└── batches/
    ├── batch_001.json
    └── ...
```

### state.json shape (abbreviated)
```json
{
  "job_id": "uuid",
  "source_type": "bookmarks_html",
  "source_path": "/path/to/input",
  "items": [
    {
      "id": 0,
      "original": { "url": "...", "title": "...", ... },
      "classification": {
        "category": "dev-tools",
        "confidence": 0.92,
        "batch_num": 3,
        "reasoning": "..."
      },
      "status": "classified"
    }
  ],
  "progress": {
    "phase": "classify",
    "next_batch_start": 876,
    "batch_size": 100,
    "total_classified": 876,
    "total_uncertain": 12,
    "total_pending": 1971
  }
}
```

### taxonomy.json shape
```json
{
  "categories": {
    "dev-tools": {
      "name": "Development Tools",
      "description": "Programming tools, IDEs, CI/CD",
      "parent": null,
      "subcategories": ["code-editors", "ci-cd"],
      "item_count": 342,
      "examples": ["github.com", "code.visualstudio.com"]
    }
  },
  "version": 1
}
```

---

## Error Handling

- **Parse failure:** Report to user, don't create a broken state.json
- **Crash mid-batch:** On resume, `organize_progress` detects the gap.
  Re-process the incomplete batch (items already classified won't be
  re-done — check `status` field).
- **Disk full / permission error:** Surface to user immediately.
- **Duplicate items:** Dedup by URL/path during ingest.
- **Dead links:** The enrichment phase (2.5) fetches metadata for ambiguous
  bookmarks. Dead links are marked `enriched.source = "failed"` and classified
  by URL pattern + title only. Do NOT retry failed fetches during classification.
- **Non-English content:** Classify by URL domain or available metadata.
  Mark uncertain if insufficient signal.

---

## Important Reminders

- **Never load all items into context at once.** Use batch slicing.
- **Always call `organize_progress` at turn start.** Non-negotiable.
- **Always update the graph after meaningful changes.** The graph is your
  crash-proof memory.
- **Atomic writes for state.json.** Write `.tmp`, then rename.
- **ask_user for all user-facing decisions.** Never assume.
