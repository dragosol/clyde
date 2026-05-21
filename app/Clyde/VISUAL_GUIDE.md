# Visual Design Guide

## Warning Design Decisions — DO NOT CHANGE

These visual choices were iterated on extensively with side-by-side comparison to Apple's Messages app:

- **Sidebar**: Native `List` with `.listStyle(.sidebar)` — provides macOS 26 scroll-behind-blur automatically
- **Search bar**: Frosted glass (`.ultraThinMaterial` in `.capsule`), NOT liquid glass. Matches Messages app.
- **Search bar sizing**: Font `.system(size: 14, weight: .medium)`, padding `.horizontal(14).vertical(10)`, container padding `.horizontal(12).bottom(6).top(2)`
- **Connection status**: `.overlay(alignment: .topTrailing)` on detail view with `offset(y: -38)`, NOT in toolbar (toolbar items get unwanted liquid glass framing on macOS 26)
- **New message button**: In `.toolbar(.navigation)` with icon `.offset(y: -2)` for vertical centering
- **No hover/autohide sidebar**: Abandoned after extensive attempts. Uses system toggle.

## Layout Structure

```
┌──────────────────────────────────────────────────────────────┐
│ [Toggle] [New ✎]          ● Connected              │
├──────────────┬───────────────────────────────────────────────┤
│              │                                               │
│  ┌─Search──┐ │          Message Area                         │
│  │🔍 Search│ │   (ScrollView + LazyVStack)                   │
│  └─────────┘ │                                               │
│              │   ┌─────────────────────────────┐             │
│  Conversation│   │ User bubble (right-aligned)  │             │
│  Row         │   │ accent color bg, rounded 16  │             │
│  ──────────  │   └─────────────────────────────┘             │
│  Conversation│                                               │
│  Row         │   Assistant text (left-aligned)               │
│  ──────────  │   No bubble, plain text, max 680pt            │
│  ...         │   Thinking ▸ expandable                       │
│              │   Tool calls ▸ expandable                     │
│              │   Code blocks with copy button                │
│              │                                               │
│  [⚙️ gear]   │   ┌─────────────────────────────┐             │
│              │   │ Input Area                    │             │
│              │   │ [+] [text editor...] [↑ send] │             │
│              │   └─────────────────────────────┘             │
└──────────────┴───────────────────────────────────────────────┘
```

## Sidebar Conventions (Messages-app style)

### Conversation Row
- Title: `.system(size: 15, weight: .bold)`, 1 line max
- Timestamp: `.system(size: 13)`, `.secondary`, relative format
- Preview: `.system(size: 13)`, `.secondary`, 2 lines max
- Vertical padding: 4pt per row
- No avatars/icons

### Separators
- Tint: `Color.primary.opacity(0.1)`
- Leading alignment: 40pt from leading edge
- Hidden around selected row (both above and below)

### Search Bar (Frosted Glass)
```
.safeAreaInset(edge: .top, spacing: 0) {
    HStack(spacing: 6) {
        Image(systemName: "magnifyingglass")
            .foregroundStyle(.secondary)
            .font(.system(size: 13))
        TextField("Search", text: ...)
            .textFieldStyle(.plain)
            .font(.system(size: 14, weight: .medium))
    }
    .padding(.horizontal, 14)
    .padding(.vertical, 10)
    .background(.ultraThinMaterial, in: .capsule)  // FROSTED, not liquid glass
    .padding(.horizontal, 12)
    .padding(.bottom, 6)
    .padding(.top, 2)
}
```

## Connection Status
```
HStack(spacing: 6) {
    Circle().fill(green/red).frame(width: 7, height: 7)
        .shadow(color: green/red.opacity(0.6), radius: 3)
    Text("Connected"/"Offline").font(.caption).foregroundStyle(.secondary)
}
.offset(y: -38)  // Aligns with toolbar buttons
.padding(.trailing, 16)
```
Background: EllipticalGradient (green/red → clear), `.ignoresSafeArea(.all, edges: .top)`

## Message Rendering

### User Messages
- Right-aligned with `Spacer(minLength: 80)`
- Background: `Color.accentColor.opacity(0.15)`
- Corner radius: 16
- Padding: horizontal 14, vertical 10

### Assistant Messages
- Left-aligned, NO bubble background
- Max width: 680pt
- Horizontal padding: 20pt
- Full markdown rendering (non-streaming) or plain Text (streaming)
- Streaming cursor: 2pt wide blinking rectangle

### Thinking Section
- Purple theme (`Color.purple.opacity(0.1)` background)
- Brain icon (SF Symbol)
- Expandable with spring animation
- Rotating status messages: "pondering...", "considering options...", etc.
- Animated dots (3 circles, staggered opacity)

### Tool Calls
- Color-coded by status: blue (running), green (done), red (error)
- 19 tool icons mapped to SF Symbols
- Expandable arguments view
- Progress spinner while running

### Code Blocks
- Header: language label + copy button
- Background: `Color(nsColor: .controlBackgroundColor)` header, `.textBackgroundColor.opacity(0.5)` body
- Horizontal scrolling for long lines
- Monospaced font
- Rounded corners: 8pt with stroke border

### Typography Hierarchy
| Element | Font |
|---------|------|
| H1 | system 26pt bold |
| H2 | system 22pt bold |
| H3 | system 18pt semibold |
| H4 | system 16pt semibold |
| H5-H6 | system 15pt semibold |
| Body | system default |
| Code | system monospaced |
| Sidebar title | system 15pt bold |
| Sidebar preview | system 13pt |
| Timestamp | caption2 |

## Color Palette
- User bubble: `accentColor.opacity(0.15)`
- Connected: `Color.green` with glow
- Offline: `Color.red` with glow
- Thinking: `Color.purple`
- Tool running: `Color.blue`
- Tool done: `Color.green`
- Tool error: `Color.red`
- Separators: `Color.primary.opacity(0.1)`
- Blockquote bar: `Color.secondary.opacity(0.4)`

## Spacing Reference
- Sidebar width: 250-350pt (ideal 280)
- Message horizontal padding: 20pt
- Message vertical padding: 2pt (user), 4pt (assistant)
- Input panel min height: 66pt (3 lines), max 154pt (7 lines)
- Input font: system 16pt
