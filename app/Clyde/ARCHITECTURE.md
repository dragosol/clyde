# Clyde App Architecture

**Last Updated:** April 2, 2026
**Status:** Production — Read this before making changes.

This document describes the Clyde macOS chat interface for Clyde. It is the source of truth for the app's design. Future AI agents will use this to understand the codebase and make informed changes. **Any inaccuracies will cause them to break the app.** Update this document whenever you significantly change the architecture.

---

## CRITICAL — DO NOT CHANGE THESE CONVENTIONS

The following design decisions were made intentionally after careful consideration and testing. They have been abandoned, reverted, or proven to work best. **Do not revert these without understanding why they exist.**

### 1. Simple NavigationSplitView — NO hover-reveal, NO autohide sidebar
- Uses macOS system sidebar toggle (hamburger menu).
- Previous attempts at hover-reveal and auto-hiding were abandoned as too complex and fragile.
- The current approach is stable and matches native macOS patterns.

### 2. Messages-app-style sidebar
- Uses native `List(selection:)` with `.listStyle(.sidebar)` for native scroll-behind-blur effect.
- **NOT** a custom ScrollView — this ensures proper macOS sidebar behavior and performance.
- Pinned and unpinned sections organized separately.

### 3. Frosted glass search bar
- Uses `.ultraThinMaterial` in `.capsule` shape, matching Apple Messages app.
- **NOT** liquid glass `.glassEffect()` — tested and determined frosted glass is more appropriate.
- Positioned in `.safeAreaInset(edge: .top)` to sit above the conversation list.

### 4. Connection status as overlay
- Positioned as `.overlay(alignment: .topTrailing)` with `offset(y: -38)`.
- **NOT** in toolbar — toolbar items get automatic liquid glass framing on macOS 26, which interferes.
- Floating green/red indicator with connection state label.

### 5. New message button in toolbar
- Placement `.navigation` (left side) with `.offset(y: -2)` for precise pixel alignment.
- Matches Messages app "compose" button position.

### 6. CommonMark-compliant markdown fence parsing
- Counts opening fence length (backticks or tildes).
- Closing fence must be same character, same length or greater, **nothing else on the line**.
- Handles 4-backtick outer fences with inner 3-backtick code examples correctly.
- Per CommonMark spec: unclosed fences capture to EOF.

### 7. Markdown-in-code-block detection
- Empty-language code blocks with markdown signals are re-parsed at the **parsing level**, not the view level.
- Detects 4+ markdown signals (headings, bullets, bold, numbered lists) in first 30 lines.
- Uses `depth` parameter in `parseBlocks()` to prevent infinite recursion (depth > 0 skips re-parsing).
- This is done during `parseBlocks()` with inlined blocks, NOT with nested views.

### 8. Smart `---` separator handling
- `---` is only treated as an agent separator when **no regular content has been emitted yet**.
- Once content is flowing, `---` passes through as a markdown horizontal rule.
- Prevents agent dividers from interfering with markdown rendering.

---

## System Architecture Diagram

```
ClydeApp.swift (@main)
  └─ ContentView (NavigationSplitView, 250-350pt sidebar)
       ├─ SidebarView
       │    ├─ Frosted glass search bar (.safeAreaInset, .ultraThinMaterial)
       │    ├─ Native List(selection:) with UUID binding
       │    │    ├─ Section("Pinned") → [ConversationRow]
       │    │    └─ Section("") (unpinned) → [ConversationRow]
       │    │         ├─ Title, last message preview, relative date
       │    │         ├─ Context menu: pin, rename, delete
       │    │         └─ Separator visibility tied to selection
       │    └─ Toolbar: Settings gear
       │
       ├─ Detail column
       │    ├─ Connection glow (.background, EllipticalGradient, top 100pt)
       │    ├─ ChatView (ScrollView + LazyVStack)
       │    │    ├─ Message bubbles with markdown rendering
       │    │    ├─ Input panel with attachments
       │    │    └─ Auto-scroll to newest message
       │    ├─ EmptyStateView (no conversation selected)
       │    └─ Connection status (.overlay, .topTrailing, offset: -38)
       │
       └─ Toolbar: New conversation button (.placement .navigation, -2y offset)
```

---

## State Management

### AppViewModel
**Type:** `@MainActor ObservableObject`
**File:** `AppViewModel.swift`

Publishes:
- `@Published conversations: [Conversation]` — sorted by `updatedAt` descending (most recent first)
- `@Published selectedConversation: Conversation?` — currently selected conversation
- `@Published searchText: String` — live search filter
- `@Published isStreaming: Bool` — whether an assistant response is actively streaming
- `@Published showSettings: Bool` — sheet presentation state
- `@Published isConnected: Bool` — API connection status (checked every 15 seconds)

Computed properties:
- `filteredConversations: [Conversation]` — title + message content search
- `pinnedConversations: [Conversation]` — filtered, pinned only
- `unpinnedConversations: [Conversation]` — filtered, not pinned

Key behaviors:
- Throttles disk persistence to 500ms during streaming to reduce I/O.
- Loads conversations on init, begins background connection check every 15 seconds.
- On message send, auto-generates title from first message (if `autoTitle` is true).

### APIService
**Type:** `@MainActor ObservableObject`
**File:** `APIService.swift`

Streams chat completions via `AsyncThrowingStream<StreamDelta, Error>`:
- Endpoint: `POST /v1/chat/completions` (OpenAI-compatible).
- Base URL: configurable (default `http://localhost:8801`).
- Streaming via `URLSession.bytes` with SSE parsing.

Request format:
```swift
ChatCompletionRequest(
    model: String,
    messages: [APIMessage],        // text + data URLs for attachments
    stream: true,
    temperature: Double,
    maxTokens: Int
)
```

Response format (SSE):
```
data: {"choices":[{"delta":{"content":"..."}}]}
data: [DONE]
```

Content processing pipeline:
1. Parse SSE line → extract JSON chunk
2. `processStreamContent()` → buffer `<think>` blocks, detect agent markers
3. `parseStatusMarkers()` → line-by-line marker detection:
   - `*thinking...*` → `.thinkingStart`
   - `*using toolname* \`args\`` → `.toolStart(name, argsPreview)`
   - `*toolname done*` or `*toolname error*` → `.toolDone(name, isError)`
   - `---` → `.separator` (only before first content line)
   - `<think>...</think>` → `.thinking(content)` (incremental updates)
4. Dispatch StreamDelta types to AppViewModel for UI update

### PersistenceManager
**Type:** `@MainActor` singleton
**File:** `PersistenceManager.swift`

Storage (sandboxed):
- Conversations: `~/Library/Containers/Shastasia.Clyde/Data/Library/Application Support/Clyde/conversations/{uuid}.json`
- Settings: `UserDefaults` (via `@AppStorage` in views)

API:
- `loadConversations()` → sorted by `updatedAt` (most recent first)
- `saveConversation(_)` → atomic write (temp file + rename) with JSON prettyPrinted, sortedKeys
- `deleteConversation(_)` → remove file
- `generateTitle(from:)` → first sentence or 50 chars max

Settings keys (UserDefaults):
- `api_endpoint` (default: `http://localhost:8801`)
- `model_name` (default: `clyde`)
- `temperature` (default: `0.7`)
- `max_tokens` (default: `4096`)
- `show_thinking`, `show_tool_calls`, `auto_title`, `sound_effects`, `theme`

---

## Data Models

### Conversation
```swift
struct Conversation: Identifiable, Codable {
    let id: UUID
    var title: String
    var messages: [ChatMessage]
    var projectId: UUID?              // Future: project grouping
    var isPinned: Bool
    var createdAt: Date
    var updatedAt: Date               // Used for sorting in sidebar
}
```

### ChatMessage
```swift
struct ChatMessage: Identifiable, Codable {
    let id: UUID
    var role: MessageRole             // .user, .assistant, .system
    var content: String
    var attachments: [Attachment]
    var toolCalls: [ToolCall]
    var thinkingContent: String?      // <think> block content
    var timestamp: Date
    var isStreaming: Bool             // true while receiving content
}
```

### Attachment
```swift
struct Attachment: Identifiable, Codable {
    let id: UUID
    var type: AttachmentType          // .image, .video, .document
    var fileName: String
    var mimeType: String
    var base64Data: String            // For transmission
}
```

Attachments are sent to API as data URLs:
```
data:image/png;base64,{base64Data}
data:application/pdf;base64,{base64Data}
```

The agent's sidecars route by MIME prefix:
- `data:image/*` → vision sidecar
- `data:video/*` → video sidecar
- `data:application/*` or other → document sidecar

### ToolCall
```swift
struct ToolCall: Identifiable, Codable {
    let id: UUID
    var toolName: String
    var arguments: String             // Compact preview for UI
    var result: String?
    var status: ToolStatus            // .running, .done, .error
    var isExpanded: Bool
}
```

Tool icons are mapped to SF Symbols. Fallback patterns:
- Tool contains "search" → `magnifyingglass`
- Tool contains "file" → `doc`
- Tool contains "web" → `globe`
- Tool contains "memory" → `brain`
- Default → `wrench.and.screwdriver`

### StreamDelta
```swift
enum DeltaType {
    case content(String)                      // Regular response text
    case toolStart(name: String, argsPreview: String)
    case toolDone(name: String, isError: Bool)
    case thinking(String)                     // Incremental <think> content
    case thinkingStart                        // *thinking...* marker
    case separator                            // --- divider
    case done                                 // Stream complete
}
```

---

## Markdown Rendering (MessageBubbleView.swift)

### Block Parser (`parseBlocks(text, depth: Int = 0)`)

Parses blocks in order:

1. **Code fences** (CommonMark-compliant):
   - Opening: `^(`{3,}|~{3,})(.*)$` (3+ backticks/tildes + optional language)
   - Closing: same fence char, >= opening length, **nothing else on line**
   - Unclosed: captures to EOF per spec
   - If `lang.isEmpty && depth == 0 && content looks like markdown`: re-parse as blocks with `depth: 1`

2. **Headings**: `^(#{1,6})\s+(.+)$`
   - H1: 26pt bold
   - H2: 22pt bold
   - H3: 18pt semibold
   - H4: 16pt semibold
   - H5/H6: 15pt semibold

3. **Horizontal rules**: Lines of only `-`, `*`, or `_` (3+ chars, one type)

4. **Tables**: Pipe-delimited, requires `|` prefix AND suffix, `---` separator row

5. **Task lists**: `- [x] text` / `- [ ] text` with checkbox rendering

6. **Bullet lists**: `- ` or `* ` prefix, consecutive lines

7. **Numbered lists**: `^\d+\.\s+` prefix

8. **Blockquotes**: `> ` prefix with left accent bar

9. **Text**: Accumulated consecutive non-empty lines

### Inline Parser
Uses Apple's built-in `AttributedString(markdown:, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))` for:
- Bold: `**text**`
- Italic: `*text*` or `_text_`
- Code: `` `text` ``
- Links: `[text](url)`

### Markdown Detection (`looksLikeMarkdown(text)`)
Looks for 4+ signals in first 30 lines:
- Headings: `# `, `## `, `### `
- Bullets: `- `, `* `
- Blockquotes: `> `
- Bold: `**...**`
- Numbered lists: `^\d+\.\s+`

If 4+ signals found, block is re-parsed as inline markdown blocks (not a code block).

---

## UI Components

### MessageBubbleView
- **User messages**: right-aligned, tinted bubble, no markdown
- **Assistant messages**: left-aligned, no background, full markdown rendering

User message layout:
```
[Spacer] [Attachments] [Bubble] [Timestamp]
```

Assistant message layout:
```
[ThinkingView] (if content)
[ToolCallView] (if calls)
[MarkdownText]
[StreamingCursor] (if streaming)
[AttachmentsView]
[Timestamp]
```

### ThinkingView
- Collapsed: shows rotating thinking messages + animated dots
- Expanded: shows full `<think>` content in italic monospace
- Messages cycle every 2 seconds

### ToolCallView
- Header: icon, tool name, status icon (circle.dotted / checkmark / exclamation)
- Expandable details: arguments in monospace
- Status colors: blue (running), green (done), red (error)

### CodeBlockView
- Header: language label, Copy button
- Content: monospace, horizontal scroll, text selection enabled
- Copy button shows checkmark + "Copied" for 2 seconds

### AttachmentsView
- Displays images as thumbnails (100×100, clipped)
- Videos: purple background with film icon
- Documents: colored by type (PDF red, DOCX blue, XLSX green, etc.)
- Clickable to remove before send

### InputTextEditor (NSViewRepresentable)
- Custom NSTextView with keyboard handling:
  - **Enter** (plain) → send message
  - **Cmd+Enter** or **Shift+Enter** → insert newline
  - Dynamic height: 3–7 lines (66–154pt), then scrolls
  - Auto-disable smart quotes, dashes, replacements

---

## File & Folder Structure

```
Clyde/
├─ ClydeApp.swift              — @main, WindowGroup config
├─ ContentView.swift           — NavigationSplitView, connection glow
├─ SidebarView.swift           — List(selection:), search, ConversationRow
├─ ChatView.swift              — ScrollView, LazyVStack, input panel
│   ├─ InputTextEditor (NSViewRepresentable)
│   ├─ AttachmentPreviewBar
│   └─ AttachmentPreview
├─ MessageBubbleView.swift     — Markdown parser, ThinkingView, ToolCallView
│   ├─ MarkdownText (block parser)
│   ├─ CodeBlockView
│   ├─ ThinkingView
│   ├─ ThinkingDotsView
│   ├─ ToolCallView
│   ├─ AttachmentsView
│   ├─ AttachmentThumbnail
│   └─ StreamingCursor
├─ AppViewModel.swift          — State, conversation ops, streaming
├─ APIService.swift            — SSE streaming, marker parsing
├─ Models.swift                — Data types (Conversation, ChatMessage, etc.)
├─ PersistenceManager.swift    — JSON files, UserDefaults
└─ SettingsView.swift          — TabView with Connection, Appearance tabs
```

---

## Key Design Patterns

### MVVM with Environment Injection
- AppViewModel is `@StateObject` in ContentView, `@EnvironmentObject` in all children.
- One-way data flow: ViewModel publishes state → Views subscribe.
- Bidirectional bindings for UI state (search text, input, selection).

### Streaming with AsyncThrowingStream
- APIService returns `AsyncThrowingStream<StreamDelta, Error>`.
- AppViewModel iterates with `for try await delta in stream`.
- Cancellation via `stream.finish()` or task cancellation.

### Throttled Persistence
- During streaming, disk writes are throttled to 500ms.
- On `.done` delta, force save immediately.
- Prevents excessive I/O during token-by-token generation.

### Conversation Sorting
- Always sorted by `updatedAt` descending (most recent first).
- Updated on message send and conversation title/pin changes.
- Sidebar display respects pinned section at top.

### Security-Scoped File Access
- File attachments use `url.startAccessingSecurityScopedResource()`.
- Access is released immediately after encoding to base64.
- File picker and drag-drop both supported.

---

## Performance Optimizations

1. **LazyVStack** for message rendering — only renders visible messages.
2. **Throttled persistence** (500ms) during streaming reduces disk I/O.
3. **Atomic file writes** — write to temp file, then rename to prevent partial writes.
4. **Incremental thinking updates** — UI updates before complete block arrives.
5. **50K character safety limit** — messages over 50K skip markdown parsing to prevent UI freezes.
6. **ScrollViewReader** with auto-scroll to bottom on new messages.
7. **Connection check on background task**, not main thread (every 15 seconds).
8. **NSViewRepresentable text editor** for fine-grained keyboard control without SwiftUI overhead.
9. **Debug JSON logging** — SSE decode errors logged in DEBUG builds for diagnostics.

---

## API Integration Details

### Endpoint
`POST /v1/chat/completions` (OpenAI-compatible SSE)

### Request Headers
```
Content-Type: application/json
```

### Request Body
```json
{
  "model": "clyde",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
      ]
    }
  ],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 4096
}
```

### Response (SSE)
```
data: {"choices":[{"delta":{"content":"Hello"}}]}
data: {"choices":[{"delta":{"content":" there"}}]}
data: [DONE]
```

### Status Markers (Agent Output)
```
*thinking...*              — Indicator that agent is thinking
*using bash* `ls -la`      — Tool start with preview args
*bash done*                — Tool succeeded
*bash error*               — Tool failed
---                        — Separator (before content)
<think>...</think>         — Thinking block (XML tags)
```

---

## Settings

All persisted via UserDefaults (accessible via `@AppStorage` in SettingsView):

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `api_endpoint` | String | `http://localhost:8801` | Clyde API location |
| `model_name` | String | `clyde` | Model identifier |
| `temperature` | Double | `0.7` | Sampling temperature (0–2) |
| `max_tokens` | Int | `4096` | Max response tokens |
| `show_thinking` | Bool | `false` | Display thinking blocks |
| `show_tool_calls` | Bool | `true` | Display tool call UI |
| `auto_title` | Bool | `true` | Auto-generate conversation title |
| `sound_effects` | Bool | `false` | Play notification sounds |
| `theme` | String | `"auto"` | "auto" / "light" / "dark" |

Changes to connection settings (`api_endpoint`, `model_name`, `temperature`, `max_tokens`) trigger `updateAPISettings()` to rebuild APIService.

---

## Common Tasks

### Adding a New UI Component
1. Create view struct in appropriate file (or new file if large).
2. Use `@EnvironmentObject var viewModel: AppViewModel` for state.
3. Ensure previews are disabled (Xcode Preview system has issues).
4. Test via full app run (`Cmd+R`), not previews.

### Modifying Markdown Rendering
1. Edit `parseBlocks()` in MessageBubbleView.swift.
2. Remember: block parsing is at the **parsing level**, not the view level.
3. If re-parsing empty code blocks as markdown, increment `depth` to prevent infinite loops.
4. Test with various markdown inputs (nested fences, tables, mixed blocks).

### Adding a New File Type to Attachments
1. Add case to `AttachmentType` enum in Models.swift.
2. Update `MIMEType.from(extension:)` and `attachmentType(for:)`.
3. Update `ChatView.fileImporter` to include new `UTType`.
4. Update `AttachmentPreview` to handle rendering (thumbnail or icon).
5. Update ToolCallView icon mapping if applicable.

### Changing Connection Settings
1. Update key in `PersistenceManager.SettingsKey`.
2. Add `@AppStorage` binding in SettingsView.
3. Call `viewModel.updateAPISettings()` to rebuild APIService (tied to onChange on settings fields).

---

## Known Limitations

- **Xcode Previews disabled**: SwiftUI Preview system has issues with the custom text editor and complex markdown rendering. Test via full app run only.
- **No voice input**: Audio recording not yet implemented.
- **50K character markdown limit**: Messages over 50,000 characters skip markdown parsing and render as plain text to prevent UI freezes.
- **Sandboxed persistence path**: App data lives in `~/Library/Containers/Shastasia.Clyde/Data/Library/Application Support/Clyde/conversations/`, NOT the regular Application Support directory.
- **Tool result display**: Tool results from the agent are not displayed in the UI — only the tool call itself (name, status). This is by design; the agent's response is the result.

---

## Future Enhancements

- [ ] Project system integration (organize conversations by project)
- [ ] System prompt customization per conversation
- [ ] Code execution sandboxing (run code blocks directly in Clyde)
- [ ] Conversation search with Spotlight indexing
- [ ] Sync to iCloud (conversation backup)
- [ ] Voice input / voice-to-text
- [ ] Real-time collaboration (multi-user conversations)
- [ ] Conversation templates / saved prompts

---

## Troubleshooting

**App won't connect to API:**
- Check API endpoint in Settings (default `http://localhost:8801`).
- Verify Clyde service is running and accessible.
- Try "Test Connection" button in Settings.

**Messages not streaming:**
- Check connection status (green indicator top-right).
- Verify model name in Settings matches running agent.
- Check console for API errors.

**Markdown not rendering:**
- Ensure content is in assistant message, not user message.
- Check if thinking/tool blocks are interfering with parsing.
- View raw message JSON in ~/Library/Containers/Shastasia.Clyde/Data/Library/Application Support/Clyde/conversations/{uuid}.json.

**File attachments not working:**
- Verify file type is in `ChatView.fileImporter` allowed types.
- Check sandboxed app support directory permissions.
- Try drag-drop instead of file picker (or vice versa).

---

## References

- SwiftUI documentation: https://developer.apple.com/documentation/swiftui/
- CommonMark spec: https://spec.commonmark.org/
- OpenAI API: https://platform.openai.com/docs/api-reference/chat/create
- macOS Human Interface Guidelines: https://developer.apple.com/design/human-interface-guidelines/macos
