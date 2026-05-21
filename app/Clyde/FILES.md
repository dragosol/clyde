# File Reference

## ⚠️ Before Modifying Any File
Read START_HERE.md and ARCHITECTURE.md first. They contain critical design decisions that must not be reverted.

## Swift Source Files

| File | Lines | Purpose |
|------|-------|---------|
| `ClydeApp.swift` | ~17 | App entry point, WindowGroup, window constraints |
| `ContentView.swift` | ~80 | NavigationSplitView, connection overlay, toolbar |
| `SidebarView.swift` | ~120 | Messages-style sidebar, frosted search, conversation list |
| `ChatView.swift` | ~350 | Message list, input panel, CustomTextEditor, file handling |
| `MessageBubbleView.swift` | ~870 | Markdown parser, code blocks, thinking, tools, attachments |
| `AppViewModel.swift` | ~220 | State management, streaming, conversation CRUD |
| `Models.swift` | ~300 | Data models, API types, StreamDelta |
| `APIService.swift` | ~390 | SSE streaming, marker parsing, connection check |
| `PersistenceManager.swift` | ~130 | JSON persistence, UserDefaults settings |
| `SettingsView.swift` | ~130 | Three-tab settings UI |

## Documentation Files

| File | Purpose |
|------|---------|
| `START_HERE.md` | **Read first.** Critical design decisions, quick start |
| `ARCHITECTURE.md` | System architecture, patterns, data flow |
| `VISUAL_GUIDE.md` | UI conventions, spacing, colors, typography |
| `IMPLEMENTATION.md` | Detailed implementation notes per file |
| `FILES.md` | This file — file inventory |
| `RUNNING.md` | How to build and run |
| `TESTING.md` | Mock server, test checklist |
| `EXTENDING.md` | How to add features |
| `CHECKLIST.md` | Implementation checklist |
| `README.md` | User-facing documentation |

## Dependency Graph
```
ClydeApp
  └─ ContentView
       ├─ AppViewModel (@StateObject, shared via @EnvironmentObject)
       │    ├─ APIService (streaming, connection)
       │    └─ PersistenceManager (load/save conversations)
       ├─ SidebarView (@EnvironmentObject AppViewModel)
       │    └─ ConversationRow (inline)
       ├─ ChatView (@EnvironmentObject AppViewModel)
       │    ├─ MessageBubbleView (per message)
       │    │    ├─ MarkdownText (block parser + inline parser)
       │    │    ├─ CodeBlockView
       │    │    ├─ ThinkingView
       │    │    ├─ ToolCallView
       │    │    └─ AttachmentsView / AttachmentThumbnail
       │    └─ CustomTextEditor (NSViewRepresentable)
       ├─ EmptyStateView (inline in ContentView)
       └─ SettingsView (sheet)
```

## Platform Requirements
- macOS 26+ (SwiftUI with liquid glass, NavigationSplitView improvements)
- Xcode 26+
- Swift 6.2+
