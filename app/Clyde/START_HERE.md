# Clyde — Start Here

## ⚠️ CRITICAL — Read Before Making Any Changes

These design decisions were made through extensive iteration and MUST NOT be reverted:

1. **NO hover-reveal or autohide sidebar.** This was attempted and abandoned. The app uses a standard `NavigationSplitView` with the system sidebar toggle. Do not re-implement hover behavior.
2. **Messages-app style sidebar.** Uses native `List(selection:)` with `.listStyle(.sidebar)` which provides scroll-behind-blur automatically. Do NOT replace with custom ScrollView.
3. **Frosted glass search bar**, NOT liquid glass. Uses `.background(.ultraThinMaterial, in: .capsule)`. Apple's Messages app itself uses frosted glass here despite macOS 26 liquid glass conventions.
4. **Connection status is an `.overlay()` on the detail view**, NOT a toolbar item. Toolbar items on macOS 26 get automatic liquid glass framing which we don't want. Uses `offset(y: -38)` for alignment.
5. **Markdown parser uses CommonMark fence counting.** Opening fence backtick count is tracked; closing fence must have >= that count and nothing else on the line. This prevents inner ``` examples from closing an outer ```` fence.
6. **Empty-language code blocks containing markdown are re-parsed.** The `parseBlocks()` function detects when a code block with no language has markdown-like content (4+ signals in first 30 lines) and inlines the parsed blocks directly. Uses `depth` parameter (max 1) to prevent recursion. This is NOT done with nested MarkdownText views (that causes watchdog kills).
7. **`---` in APIService is context-aware.** It's only treated as an agent separator before any content has been emitted. Once content is flowing, `---` passes through as a markdown horizontal rule.

## What You Have

A native macOS 26 SwiftUI chat client ("Clyde") that connects to a Clyde FastAPI agent on port 8801 (MLX backend on port 8800, e.g. Qwen3.5-122B-A10B via TurboQuant KV).

**10 Swift files:**
| File | Purpose |
|------|---------|
| `ClydeApp.swift` | App entry, window constraints (600×400 min, 1000×700 default) |
| `ContentView.swift` | NavigationSplitView root, connection overlay, toolbar |
| `SidebarView.swift` | Messages-style conversation list with frosted search |
| `ChatView.swift` | Message ScrollView, input panel, file drag-drop |
| `MessageBubbleView.swift` | Markdown parser, code blocks, thinking, tool calls, attachments |
| `AppViewModel.swift` | Central state, streaming, conversation CRUD |
| `Models.swift` | Data models (Conversation, ChatMessage, Attachment, ToolCall, API types) |
| `APIService.swift` | SSE streaming, status marker parsing, connection check |
| `PersistenceManager.swift` | JSON file persistence, UserDefaults settings |
| `SettingsView.swift` | Three-tab settings (Connection, Appearance, Behavior) |

## Quick Start
1. Open `Clyde.xcodeproj` in Xcode
2. Ensure Clyde agent is running on `localhost:8801`
3. Press ⌘R to build and run (previews are disabled)

## API Contract
```
POST /v1/chat/completions (OpenAI-compatible)
GET  /v1/models (connection check)
```
Streaming via SSE with agent status markers: `*thinking...*`, `*using tool* \`args\``, `*tool done*`, `---`

## Keyboard Shortcuts
- ⌘N — New conversation
- Enter — Send message
- ⌘Enter / Shift+Enter — Newline in input
- ⌘, — Settings

## Architecture at a Glance
- MVVM: AppViewModel (`@StateObject`) → EnvironmentObject in child views
- Streaming: AsyncThrowingStream with SSE parsing
- Persistence: JSON files per conversation (sandboxed: ~/Library/Containers/Shastasia.Clyde/Data/Library/Application Support/Clyde/)
- File Output: Agent-generated files stored in .../Clyde/files/{conversationId}/ — separate from JSON
- Settings: UserDefaults via @AppStorage
- Markdown: Custom block parser + Apple's AttributedString for inline

## What's NOT Implemented (Phase 2)
- Projects / workspace organization
- IDE/code view panel
- Command palette (⌘K)
- Syntax highlighting in code blocks (Highlightr)
- Multiple themes / accent color picker
- Sound effects playback
- Export conversations
- iCloud sync
