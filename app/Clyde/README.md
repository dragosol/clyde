# Clyde

A native macOS 26 chat client for Clyde — a local AI agent platform.

## Features
- Real-time streaming responses via SSE
- Thinking visualization (expandable `<think>` blocks)
- Tool call display with 19 tool icon mappings
- File attachments (drag-drop, images, documents, code)
- Full markdown rendering (headings, lists, tables, code blocks, blockquotes)
- CommonMark-compliant code fence parsing
- Conversation persistence (JSON files)
- Pin, search, rename, delete conversations
- Messages-app style sidebar with frosted glass search
- Connection status monitoring
- Configurable settings (endpoint, model, temperature, tokens)

## Quick Start
1. Open `Clyde.xcodeproj` in Xcode 26+
2. Start the Clyde agent on `localhost:8801`
3. Press ⌘R to build and run

## Requirements
- macOS 26+
- Clyde FastAPI agent running on port 8801
- MLX backend on port 8800 (e.g. Qwen3.5-122B-A10B via TurboQuant KV)

## Architecture
See `ARCHITECTURE.md` for full details. MVVM pattern with:
- `AppViewModel` as central state manager
- `APIService` for SSE streaming
- `PersistenceManager` for JSON file storage
- SwiftUI views with `@EnvironmentObject` injection

## ⚠️ For AI Agents
Read `START_HERE.md` before making any changes. It contains critical design decisions that were made through extensive iteration and must not be reverted.

## API
OpenAI-compatible endpoints:
- `POST /v1/chat/completions` — streaming chat
- `GET /v1/models` — connection check

## Keyboard Shortcuts
| Shortcut | Action |
|----------|--------|
| ⌘N | New conversation |
| Enter | Send message |
| ⌘Enter | New line |
| Shift+Enter | New line |

## License
Private project.
