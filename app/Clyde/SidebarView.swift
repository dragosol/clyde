//
//  SidebarView.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//  Updated 2026-04-14 — Settings button summons native Settings window (⌘,).
//  Fixed  2026-04-14 — use SettingsLink, per macOS 26 runtime guidance
//    ("Please use SettingsLink for opening the Settings scene."). The prior
//    NSApp.sendAction(Selector("showSettingsWindow:")) path silently no-ops
//    under SwiftUI's Scene-based apps on modern macOS.
//

import SwiftUI
import AppKit

struct SidebarView: View {
    @EnvironmentObject var viewModel: AppViewModel

    /// Binding that bridges the List's UUID? selection to viewModel.selectedConversation.
    /// Uses switchToConversation to properly cancel any in-flight stream.
    private var selection: Binding<UUID?> {
        Binding(
            get: { viewModel.selectedConversation?.id },
            set: { id in
                let conversation = viewModel.conversations.first { $0.id == id }
                viewModel.switchToConversation(conversation)
            }
        )
    }

    var body: some View {
        List(selection: selection) {
            // Pinned section
            if !viewModel.pinnedConversations.isEmpty {
                Section("Pinned") {
                    ForEach(Array(viewModel.pinnedConversations.enumerated()), id: \.element.id) { index, conversation in
                        ConversationRow(conversation: conversation)
                            .tag(conversation.id)
                            .listRowSeparator(separatorVisibility(for: conversation, in: viewModel.pinnedConversations, at: index))
                            .listRowSeparatorTint(Color.primary.opacity(0.1))
                            .alignmentGuide(.listRowSeparatorLeading) { _ in 0 }
                    }
                }
            }

            // Recent
            Section {
                ForEach(Array(viewModel.unpinnedConversations.enumerated()), id: \.element.id) { index, conversation in
                    ConversationRow(conversation: conversation)
                        .tag(conversation.id)
                        .listRowSeparator(separatorVisibility(for: conversation, in: viewModel.unpinnedConversations, at: index))
                        .listRowSeparatorTint(Color.primary.opacity(0.1))
                        .alignmentGuide(.listRowSeparatorLeading) { _ in 0 }
                }
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .top, spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                    .font(.system(size: 13))

                TextField("Search", text: $viewModel.searchText)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14, weight: .medium))
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(.ultraThinMaterial, in: .capsule)
            .padding(.horizontal, 12)
            .padding(.bottom, 6)
            .padding(.top, 2)
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                // SettingsLink is the macOS 14+ canonical way to summon the
                // `Settings { }` scene registered in ClydeApp. The runtime
                // explicitly requests this API over NSApp.sendAction.
                // The standard ⌘, shortcut is wired automatically by the
                // Settings scene — no explicit keyboardShortcut needed here.
                SettingsLink {
                    Image(systemName: "gear")
                }
                .help("Settings (⌘,)")
            }
        }
    }

    /// Hide the bottom separator if this row or the next row is selected
    private func separatorVisibility(for conversation: Conversation, in list: [Conversation], at index: Int) -> Visibility {
        let selectedID = viewModel.selectedConversation?.id

        // Hide if this row is selected
        if conversation.id == selectedID { return .hidden }

        // Hide if the next row is selected
        if index + 1 < list.count, list[index + 1].id == selectedID { return .hidden }

        return .visible
    }
}

// MARK: - Conversation Row

struct ConversationRow: View {
    @EnvironmentObject var viewModel: AppViewModel
    let conversation: Conversation

    @State private var showDeleteConfirmation = false
    @State private var isEditingTitle = false
    @State private var editedTitle = ""

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MM-dd-yyyy"
        return f
    }()

    /// Last message preview text
    private var previewText: String {
        if let last = conversation.messages.last {
            let text = last.content.trimmingCharacters(in: .whitespacesAndNewlines)
            let firstLine = text.components(separatedBy: .newlines).first ?? text
            return String(firstLine.prefix(80))
        }
        return "No messages yet"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                if isEditingTitle {
                    TextField("Title", text: $editedTitle)
                        .textFieldStyle(.plain)
                        .font(.system(size: 15, weight: .bold))
                        .onSubmit(saveTitle)
                } else {
                    Text(conversation.title)
                        .font(.system(size: 15, weight: .bold))
                        .lineLimit(1)
                }

                Spacer(minLength: 8)

                Text(Self.dateFormatter.string(from: conversation.updatedAt))
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
            }

            Text(previewText)
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .padding(.vertical, 4)
        .contextMenu {
            Button(conversation.isPinned ? "Unpin" : "Pin") {
                viewModel.togglePin(conversation)
            }

            Button("Rename") {
                editedTitle = conversation.title
                isEditingTitle = true
            }

            Button("Show in Finder") {
                showInFinder()
            }

            Button("Export to Markdown") {
                exportToMarkdown()
            }

            Divider()

            Button("Delete", role: .destructive) {
                showDeleteConfirmation = true
            }
        }
        .confirmationDialog(
            "Delete this conversation?",
            isPresented: $showDeleteConfirmation,
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive) {
                viewModel.deleteConversation(conversation)
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This action cannot be undone.")
        }
    }

    private func saveTitle() {
        isEditingTitle = false
        if !editedTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            viewModel.updateConversationTitle(conversation, title: editedTitle)
        }
    }

    private func showInFinder() {
        guard let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return }
        let fileURL = appSupport
            .appendingPathComponent("Clyde")
            .appendingPathComponent("conversations")
            .appendingPathComponent("\(conversation.id.uuidString).json")
        if FileManager.default.fileExists(atPath: fileURL.path) {
            NSWorkspace.shared.activateFileViewerSelecting([fileURL])
        }
    }

    private func exportToMarkdown() {
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd"
        let dateStr = dateFormatter.string(from: conversation.createdAt)

        let timeFormatter = DateFormatter()
        timeFormatter.dateFormat = "HH:mm"

        // YAML frontmatter
        var md = "---\n"
        md += "title: \"\(conversation.title)\"\n"
        md += "platform: clyde\n"
        md += "date: \(dateStr)\n"
        md += "Category: \"[[AI Chats]]\"\n"
        md += "---\n\n"

        // Messages
        for message in conversation.messages {
            let role = message.role == .user ? "User" : "Clyde"
            let time = timeFormatter.string(from: message.timestamp)

            md += "### \(role)\n"
            md += "*\(time)*\n\n"

            // Main content
            if !message.content.isEmpty {
                md += "\(message.content)\n\n"
            }

            // Tool calls
            for tool in message.toolCalls {
                let status = tool.status == .done ? "done" : tool.status == .error ? "error" : "pending"
                md += "> **Tool: \(tool.toolName)** [\(status)]\n"
                if let result = tool.result, !result.isEmpty {
                    let preview = result.count > 200 ? String(result.prefix(200)) + "..." : result
                    md += "> \(preview)\n"
                }
                md += "\n"
            }

            md += "---\n\n"
        }

        // Save to Downloads
        let safeTitle = conversation.title
            .replacingOccurrences(of: "[^a-zA-Z0-9 \\-_]", with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
            .prefix(60)
        let filename = "\(safeTitle).md"

        let downloadsURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first!
        let fileURL = downloadsURL.appendingPathComponent(filename)

        do {
            try md.write(to: fileURL, atomically: true, encoding: .utf8)
            NSWorkspace.shared.activateFileViewerSelecting([fileURL])
        } catch {
            print("Export failed: \(error)")
        }
    }
}
