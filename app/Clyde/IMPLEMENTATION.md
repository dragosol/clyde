# Implementation Details

## ⚠️ Implementation Decisions — DO NOT REVERT

Read ARCHITECTURE.md and START_HERE.md for critical design decisions that must be preserved.

## View Layer

### ContentView.swift
- `NavigationSplitView(columnVisibility:)` with system sidebar toggle
- NO hover-reveal, NO autohide — this was tried and abandoned
- Sidebar column: `SidebarView`, width 250-350pt (ideal 280)
- Detail column: `ChatView` or `EmptyStateView`
- Connection status as `.overlay(alignment: .topTrailing)` with `offset(y: -38)` — NOT toolbar
- Connection glow: `EllipticalGradient` background with `.ignoresSafeArea(.all, edges: .top)`
- New conversation button: `.toolbar(.navigation)` with icon `offset(y: -2)`
- Keyboard shortcut: ⌘N for new conversation
- Settings presented as `.sheet`

### SidebarView.swift
- Matches macOS 26 Messages app styling
- Native `List(selection: Binding<UUID?>)` with custom binding bridging to `viewModel.selectedConversation`
- `.listStyle(.sidebar)` for automatic scroll-behind-blur
- Search bar via `.safeAreaInset(edge: .top)` — frosted glass (`.ultraThinMaterial` in `.capsule`)
- Search font: `.system(size: 14, weight: .medium)` — halfway between regular and bold
- Pinned/unpinned sections with custom separator styling
- Separators: `.listRowSeparatorTint(Color.primary.opacity(0.1))`, `.alignmentGuide(.listRowSeparatorLeading) { _ in 40 }`
- Separators hidden around selected row
- Context menu: pin/unpin, rename, delete
- Settings gear in sidebar `.toolbar(.automatic)`

### ConversationRow (nested in SidebarView)
- Title: `.system(size: 15, weight: .bold)`, 1 line
- Preview: first line of last message, max 80 chars, 2 lines
- Timestamp: `.system(size: 13)`, relative format
- Inline rename via TextField toggle
- No avatars

### ChatView.swift
- `ScrollViewReader` with `LazyVStack` for messages
- Auto-scroll via `onChange` on message count and content
- Anchor: invisible `Color.clear.frame(height: 1)` with id "bottom"
- Input panel: custom `NSViewRepresentable` text editor (not SwiftUI TextEditor)
  - CustomTextEditor wraps NSTextView for proper keyboard handling
  - Enter sends, Cmd+Enter and Shift+Enter add newlines
  - Dynamic height: 66pt min (3 lines) to 154pt max (7 lines) at 16pt font
  - Line count calculation via `layoutManager.usedRect` with min 22pt guard
- Attachment bar with thumbnails and remove buttons
- Drag-drop: `.dropDestination(for: URL.self)` with yellow glow stroke feedback
- File handling: security-scoped resource access, base64 encoding
- Supported types: images, video, PDFs, Office docs, code files, JSON, YAML, etc.
- Send button disabled while streaming; stop button shown instead

### MessageBubbleView.swift
- User messages: right-aligned bubble with `accentColor.opacity(0.15)`, rounded 16
- Assistant messages: left-aligned, no bubble, max 680pt
- During streaming: plain `Text()` for performance
- After streaming: full `MarkdownText()` parser

#### MarkdownText Block Parser
`parseBlocks(_ text: String, depth: Int = 0) -> [MarkdownBlock]`

Block types (in parse priority order):
1. **Code fences**: CommonMark-compliant. Regex `/^(\`{3,}|~{3,})(.*)$/`. Counts fence length. Closing must be same char, >= length, nothing else. Unclosed = capture to end.
2. **Smart detection**: At depth 0, empty-language code blocks with `looksLikeMarkdown()` (4+ signals in 30 lines) are inlined as parsed blocks at depth 1.
3. **Headings**: `/^(#{1,6})\s+(.+)$/` → H1 26pt → H6 15pt
4. **Horizontal rules**: All same char (`-`, `*`, `_`), 3+ chars
5. **Tables**: Pipe-delimited, requires `|` prefix AND suffix, separator row with `---` and `|`
6. **Task lists**: `- [x]` / `- [ ]`
7. **Bullet lists**: `- ` or `* `
8. **Numbered lists**: `/^\d+\.\s+/`
9. **Blockquotes**: `> ` prefix, accumulated
10. **Plain text**: accumulated until block element or empty line

Inline: Apple's `AttributedString(markdown:, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))`

Text accumulator break patterns: fence regex, `#`, `- `, `* `, `> `, pipe-delimited table, numbered list regex

#### Supporting Views
- `CodeBlockView`: language header, copy button (2s feedback), horizontal scroll, monospaced font
- `ThinkingView`: purple theme, brain icon, expandable, rotating messages, animated dots
- `ToolCallView`: 19 tool icon mappings to SF Symbols, status colors, expandable args
- `AttachmentsView`: LazyVGrid adaptive thumbnails, type-specific icons
- `StreamingCursor`: 2pt blinking rectangle

## Data Layer

### AppViewModel.swift
- `@MainActor class` with `@Published` properties
- Conversation CRUD: create (insert at 0, auto-select), delete (update selection), toggle pin, rename
- `sendMessage()`: appends user message, creates streaming placeholder, processes StreamDeltas
- Delta handling: `.content` appends, `.toolStart` creates ToolCall, `.toolDone` updates status, `.thinking` sets content, `.separator` trims whitespace, `.done` marks complete
- Throttled persistence: saves every 500ms during streaming, force save on completion
- Auto-title: generates from first message (first sentence or 50 chars)
- Connection monitoring: background task every 15 seconds

### APIService.swift
- `@MainActor ObservableObject`
- SSE streaming via `URLSession.shared.bytes(for:)` with 300s timeout
- Content pipeline: SSE → JSON decode → `processStreamContent()` → `parseStatusMarkers()`
- `<think>` block handling: buffered with incremental updates
- Status markers: `*thinking...*`, `*using tool*`, `*tool done/error*`
- `---` separator: ONLY before first content line. Once `hasEmittedContent` is true, passes through as content. This preserves markdown horizontal rules.

### PersistenceManager.swift
- Singleton `@MainActor class`
- Storage (sandboxed): ~/Library/Containers/Shastasia.Clyde/Data/Library/Application Support/Clyde/conversations/{UUID}.json
- Atomic writes: write to temp file (.tmp extension), then rename via `replaceItemAt` to prevent partial writes
- JSON with ISO8601 dates, pretty-printed
- Title generation: first sentence up to punctuation, or first 50 chars, fallback "New Conversation"

### Models.swift
- Codable data models for persistence and API
- `ChatCompletionRequest` with snake_case key encoding (OpenAI compatibility)
- `ContentPart` enum: `.text(String)` or `.imageUrl(ImageURL)` for multimodal
- `StreamDelta` with typed enum for all delta types
- `MessageRole`: `.user`, `.assistant`, `.system`

## Settings (SettingsView.swift)
TabView with 3 tabs, all using `@AppStorage`:
- **Connection**: API endpoint, model name, temperature (0-2), max tokens (256-8192), test button
- **Appearance**: theme picker (auto/light/dark)
- **Behavior**: show thinking, show tool calls, auto-title, sound effects
- Frame: 500×400, saves on "Done" click
