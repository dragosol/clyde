# Extension Guide

How to extend Clyde with new features.

## Adding a New Setting

### 1. Add to PersistenceManager.swift

```swift
// In the SettingsKey enum
private enum SettingsKey: String {
    // ... existing keys
    case fontSize = "font_size"
}

// Add the property
var fontSize: Double {
    get {
        let value = UserDefaults.standard.double(forKey: SettingsKey.fontSize.rawValue)
        return value == 0 ? 14.0 : value
    }
    set {
        UserDefaults.standard.set(newValue, forKey: SettingsKey.fontSize.rawValue)
    }
}
```

### 2. Add to SettingsView.swift

```swift
// In the Appearance tab Form
@AppStorage("font_size") private var fontSize = 14.0

Section {
    LabeledContent("Font Size") {
        HStack {
            Slider(value: $fontSize, in: 12...20, step: 1)
                .frame(width: 200)
            Text("\(Int(fontSize))pt")
                .monospacedDigit()
                .frame(width: 40)
        }
    }
} header: {
    Text("Text")
        .font(.headline)
}
```

### 3. Use in MessageBubbleView.swift

```swift
@AppStorage("font_size") private var fontSize = 14.0

var body: some View {
    // ...
    Text(content)
        .font(.system(size: fontSize))
}
```

## Adding a New Tool Icon

### In MessageBubbleView.swift

```swift
private var toolIcon: String {
    switch toolCall.toolName.lowercased() {
    case let name where name.contains("search"):
        return "magnifyingglass"
    case let name where name.contains("file"):
        return "doc"
    case let name where name.contains("web"):
        return "globe"
    case let name where name.contains("memory"):
        return "brain"
    case let name where name.contains("calculator"):
        return "function"  // NEW
    case let name where name.contains("terminal"):
        return "terminal"  // NEW
    default:
        return "wrench.and.screwdriver"
    }
}
```

## Adding Markdown Support for Lists

### In MessageBubbleView.swift

Update the `MarkdownBlock` enum:

```swift
enum BlockType {
    case text
    case code(String)
    case unorderedList([String])  // NEW
    case orderedList([String])     // NEW
}
```

Update the `parseBlocks()` method:

```swift
private func parseBlocks() -> [MarkdownBlock] {
    var blocks: [MarkdownBlock] = []
    var currentText = ""
    var currentList: [String] = []
    var inUnorderedList = false
    
    let lines = content.split(separator: "\n", omittingEmptySubsequences: false)
    
    for line in lines {
        let lineStr = String(line)
        
        // Detect unordered list
        if lineStr.hasPrefix("- ") || lineStr.hasPrefix("* ") {
            if !currentText.isEmpty {
                blocks.append(MarkdownBlock(type: .text, content: currentText))
                currentText = ""
            }
            currentList.append(String(lineStr.dropFirst(2)))
            inUnorderedList = true
        } else if inUnorderedList && lineStr.trimmingCharacters(in: .whitespaces).isEmpty {
            // End of list
            blocks.append(MarkdownBlock(type: .unorderedList(currentList), content: ""))
            currentList = []
            inUnorderedList = false
        } else {
            if inUnorderedList {
                blocks.append(MarkdownBlock(type: .unorderedList(currentList), content: ""))
                currentList = []
                inUnorderedList = false
            }
            currentText += lineStr + "\n"
        }
    }
    
    if !currentText.isEmpty {
        blocks.append(MarkdownBlock(type: .text, content: currentText))
    }
    
    return blocks
}
```

Add rendering:

```swift
ForEach(parseBlocks(), id: \.id) { block in
    switch block.type {
    case .text:
        Text(parseInlineMarkdown(block.content))
    case .code(let language):
        CodeBlockView(code: block.content, language: language)
    case .unorderedList(let items):
        VStack(alignment: .leading, spacing: 4) {
            ForEach(items, id: \.self) { item in
                HStack(alignment: .top, spacing: 8) {
                    Text("•")
                    Text(item)
                }
            }
        }
    case .orderedList(let items):
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                HStack(alignment: .top, spacing: 8) {
                    Text("\(index + 1).")
                    Text(item)
                }
            }
        }
    }
}
```

## Adding Export Conversation Feature

### 1. Add to AppViewModel.swift

```swift
func exportConversation(_ conversation: Conversation) -> URL? {
    // Create markdown export
    var markdown = "# \(conversation.title)\n\n"
    markdown += "Created: \(conversation.createdAt.formatted())\n\n"
    markdown += "---\n\n"
    
    for message in conversation.messages {
        let role = message.role == .user ? "**You**" : "**Assistant**"
        markdown += "\(role) (\(message.timestamp.formatted(date: .omitted, time: .shortened))):\n\n"
        markdown += message.content + "\n\n"
        
        if let thinking = message.thinkingContent {
            markdown += "> Thinking: \(thinking)\n\n"
        }
        
        markdown += "---\n\n"
    }
    
    // Save to temporary file
    let tempURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("\(conversation.title).md")
    
    do {
        try markdown.write(to: tempURL, atomically: true, encoding: .utf8)
        return tempURL
    } catch {
        print("Export failed: \(error)")
        return nil
    }
}
```

### 2. Add to ConversationRow menu

```swift
Menu {
    Button("Rename") {
        // ...
    }
    
    Button("Export as Markdown") {
        if let url = viewModel.exportConversation(conversation) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }
    
    Divider()
    
    Button("Delete", role: .destructive) {
        // ...
    }
}
```

## Adding Sound Effects

### 1. Add sound files to project
- message_sent.wav
- message_received.wav

### 2. Create SoundManager.swift

```swift
import AVFoundation

class SoundManager {
    static let shared = SoundManager()
    
    private var players: [String: AVAudioPlayer] = [:]
    
    private init() {
        loadSounds()
    }
    
    private func loadSounds() {
        guard let sentURL = Bundle.main.url(forResource: "message_sent", withExtension: "wav"),
              let receivedURL = Bundle.main.url(forResource: "message_received", withExtension: "wav") else {
            return
        }
        
        do {
            players["sent"] = try AVAudioPlayer(contentsOf: sentURL)
            players["received"] = try AVAudioPlayer(contentsOf: receivedURL)
        } catch {
            print("Failed to load sounds: \(error)")
        }
    }
    
    func play(_ sound: String) {
        guard PersistenceManager.shared.soundEffects else { return }
        players[sound]?.play()
    }
}
```

### 3. Use in AppViewModel.swift

```swift
func sendMessage(content: String, attachments: [Attachment] = []) async {
    // ... existing code
    
    SoundManager.shared.play("sent")
    
    // ... after receiving response
    SoundManager.shared.play("received")
}
```

## Adding a Command Palette (⌘K)

### 1. Create CommandPaletteView.swift

```swift
import SwiftUI

struct CommandPaletteView: View {
    @EnvironmentObject var viewModel: AppViewModel
    @Environment(\.dismiss) var dismiss
    @State private var searchText = ""
    
    var commands: [Command] {
        var result: [Command] = [
            Command(title: "New Conversation", icon: "plus.circle", action: {
                viewModel.createNewConversation()
                dismiss()
            }),
            Command(title: "Settings", icon: "gear", action: {
                viewModel.showSettings = true
                dismiss()
            })
        ]
        
        // Add recent conversations
        for conversation in viewModel.conversations.prefix(5) {
            result.append(Command(
                title: "Open: \(conversation.title)",
                icon: "message",
                action: {
                    viewModel.selectedConversation = conversation
                    dismiss()
                }
            ))
        }
        
        return result
    }
    
    var filteredCommands: [Command] {
        if searchText.isEmpty {
            return commands
        }
        return commands.filter { $0.title.localizedCaseInsensitiveContains(searchText) }
    }
    
    var body: some View {
        VStack(spacing: 0) {
            // Search field
            HStack {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Type a command...", text: $searchText)
                    .textFieldStyle(.plain)
            }
            .padding()
            .background(.ultraThinMaterial)
            
            Divider()
            
            // Commands list
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(filteredCommands) { command in
                        Button(action: command.action) {
                            HStack {
                                Image(systemName: command.icon)
                                    .frame(width: 20)
                                Text(command.title)
                                Spacer()
                            }
                            .padding(.horizontal, 16)
                            .padding(.vertical, 12)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .background(Color.clear)
                        .hoverEffect()
                    }
                }
            }
            .frame(maxHeight: 400)
        }
        .frame(width: 500)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .shadow(radius: 20)
    }
}

struct Command: Identifiable {
    let id = UUID()
    let title: String
    let icon: String
    let action: () -> Void
}
```

### 2. Add to ContentView.swift

```swift
@State private var showCommandPalette = false

var body: some View {
    NavigationSplitView {
        // ...
    } detail: {
        // ...
    }
    .environmentObject(viewModel)
    .sheet(isPresented: $viewModel.showSettings) {
        SettingsView()
    }
    .sheet(isPresented: $showCommandPalette) {
        CommandPaletteView()
    }
    .keyboardShortcut("k", modifiers: .command) {
        showCommandPalette = true
    }
}
```

## Adding Syntax Highlighting

### 1. Add Highlightr package

In Xcode:
1. File > Add Packages
2. Search for: `https://github.com/raspu/Highlightr`
3. Add to project

### 2. Update CodeBlockView.swift

```swift
import Highlightr

struct CodeBlockView: View {
    let code: String
    let language: String
    @State private var isCopied = false
    
    private let highlightr = Highlightr()
    
    var highlightedCode: AttributedString {
        guard !language.isEmpty,
              let highlighted = highlightr?.highlight(code, as: language) else {
            return AttributedString(code)
        }
        return AttributedString(highlighted)
    }
    
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack {
                Text(language.isEmpty ? "code" : language)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                
                Spacer()
                
                Button(action: copyCode) {
                    HStack(spacing: 4) {
                        Image(systemName: isCopied ? "checkmark" : "doc.on.doc")
                        Text(isCopied ? "Copied" : "Copy")
                    }
                    .font(.caption)
                }
                .buttonStyle(.plain)
                .foregroundStyle(isCopied ? .green : .secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(Color(nsColor: .controlBackgroundColor))
            
            Divider()
            
            // Code content with syntax highlighting
            ScrollView(.horizontal, showsIndicators: false) {
                Text(highlightedCode)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(12)
            }
            .background(Color(nsColor: .textBackgroundColor).opacity(0.5))
        }
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color(nsColor: .separatorColor), lineWidth: 1)
        )
    }
    
    private func copyCode() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(code, forType: .string)
        
        withAnimation {
            isCopied = true
        }
        
        Task {
            try? await Task.sleep(for: .seconds(2))
            withAnimation {
                isCopied = false
            }
        }
    }
}
```

## Adding Custom Themes

### 1. Create Theme.swift

```swift
import SwiftUI

struct Theme {
    let name: String
    let accentColor: Color
    let bubbleBackground: Color
    let bubbleUserBackground: Color
    
    static let themes: [Theme] = [
        Theme(
            name: "Blue",
            accentColor: .blue,
            bubbleBackground: Color(nsColor: .controlBackgroundColor),
            bubbleUserBackground: Color.blue.opacity(0.15)
        ),
        Theme(
            name: "Purple",
            accentColor: .purple,
            bubbleBackground: Color(nsColor: .controlBackgroundColor),
            bubbleUserBackground: Color.purple.opacity(0.15)
        ),
        Theme(
            name: "Green",
            accentColor: .green,
            bubbleBackground: Color(nsColor: .controlBackgroundColor),
            bubbleUserBackground: Color.green.opacity(0.15)
        )
    ]
}

// In PersistenceManager
var selectedTheme: String {
    get { UserDefaults.standard.string(forKey: "selected_theme") ?? "Blue" }
    set { UserDefaults.standard.set(newValue, forKey: "selected_theme") }
}

var currentTheme: Theme {
    Theme.themes.first { $0.name == selectedTheme } ?? Theme.themes[0]
}
```

### 2. Add to SettingsView.swift

```swift
@AppStorage("selected_theme") private var selectedTheme = "Blue"

Picker("Accent Color", selection: $selectedTheme) {
    ForEach(Theme.themes, id: \.name) { theme in
        Text(theme.name).tag(theme.name)
    }
}
```

### 3. Apply in ContentView.swift

```swift
var body: some View {
    NavigationSplitView {
        // ...
    }
    .accentColor(PersistenceManager.shared.currentTheme.accentColor)
}
```

---

## Best Practices

### 1. Always Use Main Actor
```swift
@MainActor
class YourViewModel: ObservableObject {
    // UI updates are always safe
}
```

### 2. Prefer Composition
```swift
// Good: Small, focused views
struct MessageView: View {
    var body: some View {
        VStack {
            MessageHeader()
            MessageContent()
            MessageFooter()
        }
    }
}

// Bad: One giant view
struct MessageView: View {
    var body: some View {
        VStack {
            // 200 lines of code...
        }
    }
}
```

### 3. Use Computed Properties for Derived State
```swift
// Good
var filteredMessages: [Message] {
    messages.filter { $0.role == .user }
}

// Bad
@Published var filteredMessages: [Message] = []
// Now you have to keep this in sync manually
```

### 4. Handle Errors Gracefully
```swift
do {
    try await someAsyncOperation()
} catch {
    // Show error to user
    errorMessage = error.localizedDescription
    showError = true
}
```

### 5. Test with Mock Data
```swift
#if DEBUG
extension Conversation {
    static let preview = Conversation(
        title: "Test Conversation",
        messages: [
            ChatMessage(role: .user, content: "Hello"),
            ChatMessage(role: .assistant, content: "Hi there!")
        ]
    )
}
#endif

#Preview {
    ChatView(conversation: .preview)
}
```

---

These examples show how to extend Clyde while maintaining the existing architecture and design patterns. Happy coding! 🚀
