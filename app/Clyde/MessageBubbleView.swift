//
//  MessageBubbleView.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//

import SwiftUI

struct MessageBubbleView: View {
    let message: ChatMessage
    var isLastUserMessage: Bool = false
    var onRegenerate: (() -> Void)? = nil
    var onRetry: ((UUID) -> Void)? = nil
    var onEdit: ((UUID, String) -> Void)? = nil
    @State private var isThinkingExpanded = false
    @State private var hasAutoCollapsedThinking = false
    @State private var showCopied = false
    @State private var isEditing = false
    @State private var editText = ""

    var body: some View {
        if message.role == .user {
            userMessageView
        } else {
            assistantMessageView
        }
    }

    // MARK: - User message — right-aligned iMessage-style bubble

    /// Dark grey-ish pine for user message bubbles
    private static let iMessageBlue = Color(red: 0.18, green: 0.28, blue: 0.22)

    private var userMessageView: some View {
        HStack(alignment: .bottom) {
            Spacer(minLength: 80)

            VStack(alignment: .trailing, spacing: 6) {
                // Attachments above the bubble — right-aligned, wrapping grid
                if !message.attachments.isEmpty {
                    let columns = Array(repeating: GridItem(.fixed(60), spacing: 6), count: min(message.attachments.count, 5))
                    LazyVGrid(columns: columns, alignment: .trailing, spacing: 6) {
                        ForEach(message.attachments) { attachment in
                            AttachmentThumbnail(attachment: attachment)
                        }
                    }
                    .frame(maxWidth: 340, alignment: .trailing)
                }

                if !message.content.isEmpty {
                    // Queued messages (typed during an active stream and
                    // waiting their turn) render orange. AppViewModel
                    // wraps the queued→sending flag flip in withAnimation,
                    // so the tint dissolves to iMessageBlue smoothly when
                    // the agent picks the message up.
                    let bubbleColor: Color = message.isQueued
                        ? Color.orange
                        : Self.iMessageBlue
                    Text(message.content)
                        .foregroundStyle(.white)
                        .textSelection(.enabled)
                        .padding(.horizontal, 14)
                        .padding(.top, 8)
                        .padding(.bottom, 15) // extra space for tail
                        .background(
                            ChatBubbleShape(
                                isSent: true,
                                hasTail: true,
                                isGroupTop: false,
                                isGroupMiddle: false,
                                isGroupBottom: false
                            )
                            .fill(bubbleColor)
                        )
                }

                HStack(spacing: 8) {
                    if showCopied {
                        Text("Copied!")
                            .font(.caption2)
                            .foregroundStyle(.green)
                            .transition(.opacity)
                    }

                    if !message.content.isEmpty && !message.isStreaming {
                        Button(action: {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(message.content, forType: .string)
                            withAnimation { showCopied = true }
                            Task {
                                try? await Task.sleep(for: .seconds(2))
                                withAnimation { showCopied = false }
                            }
                        }) {
                            Image(systemName: "doc.on.doc")
                                .font(.caption2)
                                .padding(5)
                                .background(.ultraThinMaterial, in: Circle())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.tertiary)
                        .help("Copy message")

                        Button(action: {
                            editText = message.content
                            isEditing = true
                        }) {
                            Image(systemName: "pencil")
                                .font(.caption2)
                                .padding(5)
                                .background(.ultraThinMaterial, in: Circle())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.tertiary)
                        .help("Edit & resend")

                        Button(action: {
                            onRetry?(message.id)
                        }) {
                            Image(systemName: "arrow.counterclockwise")
                                .font(.caption2)
                                .padding(5)
                                .background(.ultraThinMaterial, in: Circle())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.tertiary)
                        .help("Retry")
                    }

                    Text(message.timestamp, style: .time)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 2)
        .sheet(isPresented: $isEditing) {
            VStack(spacing: 12) {
                Text("Edit Message")
                    .font(.headline)
                TextEditor(text: $editText)
                    .font(.body)
                    .frame(minHeight: 100, maxHeight: 300)
                    .padding(4)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color(.textBackgroundColor)))
                HStack {
                    Button("Cancel") { isEditing = false }
                        .keyboardShortcut(.cancelAction)
                    Spacer()
                    Button("Send") {
                        isEditing = false
                        onEdit?(message.id, editText)
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
            .padding()
            .frame(minWidth: 400)
        }
    }

    // MARK: - Assistant message — left-aligned, no bubble, Claude-style

    private var assistantMessageView: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Thinking section (if present) — always at the top
            // Auto-expands while streaming, collapses when done
            if let thinking = message.thinkingContent, !thinking.isEmpty {
                TieredThinkingView(
                    content: thinking,
                    isStreaming: message.isStreaming,
                    isExpanded: $isThinkingExpanded,
                    hasAutoCollapsed: $hasAutoCollapsedThinking
                )
                .transition(.opacity.combined(with: .move(edge: .top)))
            }

            // Interleaved content blocks — text and tool calls in stream order.
            // Consecutive same-tool calls are auto-grouped into collapsible summaries.
            if !message.contentBlocks.isEmpty {
                let displayBlocks = groupContentBlocks(blocks: message.contentBlocks, toolCalls: message.toolCalls)
                ForEach(Array(displayBlocks.enumerated()), id: \.element.id) { index, displayBlock in
                    let isLastBlock = message.isStreaming && index == displayBlocks.count - 1
                    switch displayBlock {
                    case .single(let block):
                        switch block.content {
                        case .text(let text):
                            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                            if !trimmed.isEmpty {
                                LongTextRenderer(text: trimmed)
                            }
                        case .toolCall(let toolCallId):
                            if let toolCall = message.toolCalls.first(where: { $0.id == toolCallId }) {
                                // ask_user tool calls are handled via .question blocks;
                                // all other single tool calls get the three-tier treatment
                                if toolCall.toolName.lowercased() == "ask_user" {
                                    ToolCallView(toolCall: toolCall)
                                        .transition(.opacity.combined(with: .move(edge: .top)))
                                } else {
                                    TieredToolCallView(toolCall: toolCall, isLastInStreamingTurn: isLastBlock)
                                        .transition(.opacity.combined(with: .move(edge: .top)))
                                }
                            }
                        case .question(let questionId):
                            if let question = message.pendingQuestions.first(where: { $0.id == questionId }) {
                                QuestionCardView(question: question)
                                    .transition(.opacity.combined(with: .scale(scale: 0.95)))
                            }
                        case .permissionRequest(let permId):
                            if let perm = message.pendingPermissions.first(where: { $0.id == permId }) {
                                PermissionCardView(permission: perm)
                                    .transition(.opacity.combined(with: .scale(scale: 0.95)))
                            }
                        case .plan(let planId):
                            if let plan = message.plans.first(where: { $0.id == planId }) {
                                PlanCardView(plan: plan)
                                    .transition(.opacity.combined(with: .scale(scale: 0.95)))
                            }
                        }
                    case .toolGroup(_, let toolName, let toolCalls, let collapsedText):
                        ToolCallGroupView(
                            toolName: toolName,
                            toolCalls: toolCalls,
                            collapsedTextBlocks: collapsedText,
                            isLastInStreamingTurn: isLastBlock
                        )
                        .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                }
            } else if !message.isStreaming {
                // Fallback for old messages without contentBlocks:
                // render tool calls then content (original behavior)
                if !message.toolCalls.isEmpty {
                    ForEach(message.toolCalls) { toolCall in
                        if toolCall.toolName.lowercased() == "ask_user" {
                            ToolCallView(toolCall: toolCall)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                        } else {
                            TieredToolCallView(toolCall: toolCall)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                }

                if !message.content.isEmpty {
                    LongTextRenderer(text: message.content)
                }
            }

            // Streaming cursor — always at the bottom of the message while streaming.
            // Shows immediately when the assistant message is created (before any content arrives).
            if message.isStreaming {
                StreamingCursor()
                    .padding(.top, 4)
            }

            // Multiple-choice questions from ask_user (fallback for old messages
            // that don't have .question content blocks — new messages render inline above)
            if !message.pendingQuestions.isEmpty {
                let inlineQuestionIds = Set(message.contentBlocks.compactMap { block -> String? in
                    if case .question(let qId) = block.content { return qId }
                    return nil
                })
                let fallbackQuestions = message.pendingQuestions.filter { !inlineQuestionIds.contains($0.id) }
                ForEach(fallbackQuestions) { question in
                    QuestionCardView(question: question)
                        .transition(.opacity.combined(with: .scale(scale: 0.95)))
                }
            }

            // Folder permission requests (fallback for old messages)
            if !message.pendingPermissions.isEmpty {
                let inlinePermIds = Set(message.contentBlocks.compactMap { block -> String? in
                    if case .permissionRequest(let pId) = block.content { return pId }
                    return nil
                })
                let fallbackPerms = message.pendingPermissions.filter { !inlinePermIds.contains($0.id) }
                ForEach(fallbackPerms) { perm in
                    PermissionCardView(permission: perm)
                        .transition(.opacity.combined(with: .scale(scale: 0.95)))
                }
            }

            // Attachments
            if !message.attachments.isEmpty {
                AttachmentsView(attachments: message.attachments)
            }

            // Output files from agent
            if !message.files.isEmpty {
                FileReferencesView(files: message.files)
            }

            // Timestamp + action bar
            HStack(spacing: 8) {
                Text(message.timestamp, style: .time)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)

                if showCopied {
                    Text("Copied!")
                        .font(.caption2)
                        .foregroundStyle(.green)
                        .transition(.opacity)
                }

                // Action buttons — always visible, subtle
                if !message.isStreaming && !message.content.isEmpty {
                    Button(action: { copyContent(asMarkdown: false) }) {
                        Image(systemName: "doc.on.doc")
                            .font(.caption2)
                            .padding(5)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.tertiary)
                    .help("Copy response")

                    Button(action: { copyContent(asMarkdown: true) }) {
                        Image(systemName: "text.badge.checkmark")
                            .font(.caption2)
                            .padding(5)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.tertiary)
                    .help("Copy as Markdown")

                    Button(action: {
                        onRetry?(message.id)
                    }) {
                        Image(systemName: "arrow.counterclockwise")
                            .font(.caption2)
                            .padding(5)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.tertiary)
                    .help("Retry")
                }
            }
        }
        .frame(maxWidth: 680, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20)
        .padding(.vertical, 4)
    }

    private func copyContent(asMarkdown: Bool) {
        let text = asMarkdown ? message.content : message.content
            .replacingOccurrences(of: "```[^\\n]*\\n", with: "", options: .regularExpression)
            .replacingOccurrences(of: "```", with: "")
            .replacingOccurrences(of: "**", with: "")
            .replacingOccurrences(of: "# ", with: "")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)

        withAnimation { showCopied = true }
        Task {
            try? await Task.sleep(for: .seconds(2))
            withAnimation { showCopied = false }
        }
    }
}

// MARK: - Markdown Text

struct MarkdownText: View {
    let content: String

    init(_ content: String) {
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(parseBlocks(content), id: \.id) { block in
                blockView(for: block)
            }
        }
    }

    @ViewBuilder
    private func blockView(for block: MarkdownBlock) -> some View {
        switch block.type {
        case .code(let language):
            CodeBlockView(code: block.content, language: language)

        case .heading(let level):
            headingView(block.content, level: level)

        case .bulletList(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("•")
                            .foregroundStyle(.secondary)
                        Text(parseInline(item))
                            .textSelection(.enabled)
                    }
                }
            }

        case .numberedList(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("\(index + 1).")
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                        Text(parseInline(item))
                            .textSelection(.enabled)
                    }
                }
            }

        case .taskList(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: item.checked ? "checkmark.square.fill" : "square")
                            .foregroundStyle(item.checked ? .green : .secondary)
                            .font(.body)
                        Text(parseInline(item.text))
                            .textSelection(.enabled)
                    }
                }
            }

        case .blockquote:
            HStack(spacing: 0) {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Color.secondary.opacity(0.4))
                    .frame(width: 3)
                    .padding(.trailing, 12)

                Text(parseInline(block.content))
                    .textSelection(.enabled)
                    .foregroundStyle(.secondary)
                    .italic()
            }
            .padding(.vertical, 2)

        case .horizontalRule:
            Divider()
                .padding(.vertical, 4)

        case .table(let header, let rows):
            tableView(header: header, rows: rows)

        case .text:
            if !block.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text(parseInline(block.content))
                    .textSelection(.enabled)
            }
        }
    }

    // MARK: - Heading

    private func headingView(_ text: String, level: Int) -> some View {
        let stripped = parseInline(text)
        let font: Font = switch level {
        case 1: .system(size: 26, weight: .bold)
        case 2: .system(size: 22, weight: .bold)
        case 3: .system(size: 18, weight: .semibold)
        case 4: .system(size: 16, weight: .semibold)
        default: .system(size: 15, weight: .semibold)
        }

        return Text(stripped)
            .font(font)
            .textSelection(.enabled)
            .padding(.top, level <= 2 ? 6 : 2)
    }

    // MARK: - Table

    private func tableView(header: [String], rows: [[String]]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack(spacing: 0) {
                ForEach(Array(header.enumerated()), id: \.offset) { _, col in
                    Text(parseInline(col.trimmingCharacters(in: .whitespaces)))
                        .font(.body.weight(.semibold))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                }
            }
            .background(Color.primary.opacity(0.06))

            Divider()

            // Rows
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(spacing: 0) {
                    ForEach(Array(row.enumerated()), id: \.offset) { _, col in
                        Text(parseInline(col.trimmingCharacters(in: .whitespaces)))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                    }
                }
            }
        }
        .font(.body)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(Color.primary.opacity(0.1), lineWidth: 1)
        )
    }

    // MARK: - Block Parsing

    private func parseBlocks(_ text: String, depth: Int = 0) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var blockId = 0
        let lines = text.components(separatedBy: "\n")
        var i = 0

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // Code block — CommonMark-compliant fence parsing
            // Opening fence: 3+ backticks (or tildes), optionally followed by language info
            // Closing fence: at least as many backticks/tildes as opening, nothing else on the line
            if let fenceMatch = trimmed.firstMatch(of: /^(`{3,}|~{3,})(.*)$/) {
                guard let fenceChar = fenceMatch.1.first else { i += 1; continue }
                let fenceLength = fenceMatch.1.count
                let lang = String(fenceMatch.2).trimmingCharacters(in: .whitespaces)
                var codeLines: [String] = []
                var foundClose = false
                var j = i + 1
                while j < lines.count {
                    let closeTrimmed = lines[j].trimmingCharacters(in: .whitespaces)
                    // Closing fence: only the same fence char, at least fenceLength of them, nothing else
                    if !closeTrimmed.isEmpty
                        && closeTrimmed.allSatisfy({ $0 == fenceChar })
                        && closeTrimmed.count >= fenceLength {
                        foundClose = true
                        break
                    }
                    codeLines.append(lines[j])
                    j += 1
                }
                let code: String
                let advanceTo: Int
                if foundClose {
                    code = codeLines.joined(separator: "\n").trimmingCharacters(in: .newlines)
                    advanceTo = j + 1 // skip past closing fence
                } else {
                    // No closing fence — per CommonMark, capture everything to end
                    code = codeLines.joined(separator: "\n").trimmingCharacters(in: .newlines)
                    advanceTo = j // j == lines.count
                }

                // If no language and content looks like markdown, inline the parsed
                // blocks directly instead of wrapping in a code block. This handles
                // models that wrap demos in bare ``` fences.
                if depth == 0 && lang.isEmpty && !code.isEmpty && looksLikeMarkdown(code) {
                    // Re-parse the code block content as markdown blocks
                    // and inline them directly (no nested view hierarchy).
                    // depth > 0 prevents infinite recursion.
                    let innerBlocks = parseBlocks(code, depth: 1)
                    for inner in innerBlocks {
                        blocks.append(MarkdownBlock(id: blockId, type: inner.type, content: inner.content))
                        blockId += 1
                    }
                } else if !code.isEmpty {
                    blocks.append(MarkdownBlock(id: blockId, type: .code(lang), content: code))
                    blockId += 1
                }
                i = advanceTo
                continue
            }

            // Heading
            if let match = trimmed.firstMatch(of: /^(#{1,6})\s+(.+)$/) {
                let level = match.1.count
                let text = String(match.2)
                blocks.append(MarkdownBlock(id: blockId, type: .heading(level), content: text))
                blockId += 1
                i += 1
                continue
            }

            // Horizontal rule
            if trimmed.allSatisfy({ $0 == "-" || $0 == "*" || $0 == "_" || $0 == " " })
                && trimmed.filter({ $0 != " " }).count >= 3
                && Set(trimmed.filter({ $0 != " " })).count == 1
                && !trimmed.isEmpty {
                blocks.append(MarkdownBlock(id: blockId, type: .horizontalRule, content: ""))
                blockId += 1
                i += 1
                continue
            }

            // Table (pipe-delimited header row followed by separator row with dashes)
            if trimmed.hasPrefix("|") && trimmed.hasSuffix("|") && trimmed.filter({ $0 == "|" }).count >= 2
                && i + 1 < lines.count && lines[i + 1].contains("---") && lines[i + 1].contains("|") {
                var headerCols = trimmed.split(separator: "|").map(String.init)
                if headerCols.first?.trimmingCharacters(in: .whitespaces).isEmpty == true { headerCols.removeFirst() }
                if headerCols.last?.trimmingCharacters(in: .whitespaces).isEmpty == true { headerCols.removeLast() }
                i += 2 // skip header + separator
                var rows: [[String]] = []
                while i < lines.count && lines[i].contains("|") {
                    var cols = lines[i].split(separator: "|").map(String.init)
                    if cols.first?.trimmingCharacters(in: .whitespaces).isEmpty == true { cols.removeFirst() }
                    if cols.last?.trimmingCharacters(in: .whitespaces).isEmpty == true { cols.removeLast() }
                    rows.append(cols)
                    i += 1
                }
                blocks.append(MarkdownBlock(id: blockId, type: .table(headerCols, rows), content: ""))
                blockId += 1
                continue
            }

            // Task list — only match actual checkbox syntax, NOT markdown links like "- [text](url)"
            if trimmed.hasPrefix("- [x] ") || trimmed.hasPrefix("- [X] ") || trimmed.hasPrefix("- [ ] ") {
                var items: [(checked: Bool, text: String)] = []
                while i < lines.count {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    if t.hasPrefix("- [x] ") || t.hasPrefix("- [X] ") {
                        items.append((true, String(t.dropFirst(6))))
                    } else if t.hasPrefix("- [ ] ") {
                        items.append((false, String(t.dropFirst(6))))
                    } else {
                        break
                    }
                    i += 1
                }
                blocks.append(MarkdownBlock(id: blockId, type: .taskList(items), content: ""))
                blockId += 1
                continue
            }

            // Bullet list
            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") {
                var items: [String] = []
                while i < lines.count {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    if t.hasPrefix("- ") {
                        items.append(String(t.dropFirst(2)))
                    } else if t.hasPrefix("* ") {
                        items.append(String(t.dropFirst(2)))
                    } else {
                        break
                    }
                    i += 1
                }
                blocks.append(MarkdownBlock(id: blockId, type: .bulletList(items), content: ""))
                blockId += 1
                continue
            }

            // Numbered list
            if trimmed.firstMatch(of: /^\d+\.\s+/) != nil {
                var items: [String] = []
                while i < lines.count {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    if let m = t.firstMatch(of: /^\d+\.\s+(.*)$/) {
                        items.append(String(m.1))
                    } else {
                        break
                    }
                    i += 1
                }
                blocks.append(MarkdownBlock(id: blockId, type: .numberedList(items), content: ""))
                blockId += 1
                continue
            }

            // Blockquote
            if trimmed.hasPrefix("> ") {
                var quoteLines: [String] = []
                while i < lines.count && lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("> ") {
                    quoteLines.append(String(lines[i].trimmingCharacters(in: .whitespaces).dropFirst(2)))
                    i += 1
                }
                blocks.append(MarkdownBlock(id: blockId, type: .blockquote, content: quoteLines.joined(separator: "\n")))
                blockId += 1
                continue
            }

            // Plain text — accumulate consecutive non-empty lines
            var textLines: [String] = []
            while i < lines.count {
                let t = lines[i]
                let tt = t.trimmingCharacters(in: .whitespaces)
                if tt.isEmpty {
                    textLines.append("")
                    i += 1
                    break
                }
                // Break on lines that look like block-level markdown elements
                let looksLikeFence = tt.firstMatch(of: /^(`{3,}|~{3,})/) != nil
                let looksLikeTable = tt.hasPrefix("|") && tt.hasSuffix("|") && tt.filter({ $0 == "|" }).count >= 2
                if looksLikeFence || tt.hasPrefix("#") || tt.hasPrefix("- ") || tt.hasPrefix("* ")
                    || tt.hasPrefix("> ") || looksLikeTable || tt.firstMatch(of: /^\d+\.\s+/) != nil {
                    break
                }
                textLines.append(t)
                i += 1
            }
            let text = textLines.joined(separator: "\n").trimmingCharacters(in: .newlines)
            if !text.isEmpty {
                blocks.append(MarkdownBlock(id: blockId, type: .text, content: text))
                blockId += 1
            } else if textLines.isEmpty {
                // Only advance if the inner loop didn't run at all
                i += 1
            }
        }
        return blocks
    }

    // MARK: - Markdown Detection

    /// Checks if a code block's content is actually markdown that should be rendered.
    /// This catches the common case where models wrap demos in bare ``` fences.
    private func looksLikeMarkdown(_ text: String) -> Bool {
        let lines = text.components(separatedBy: "\n")
        var markdownSignals = 0
        for line in lines.prefix(30) { // Check first 30 lines
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("# ") || t.hasPrefix("## ") || t.hasPrefix("### ") { markdownSignals += 2 }
            if t.hasPrefix("- ") || t.hasPrefix("* ") || t.hasPrefix("> ") { markdownSignals += 1 }
            if t.hasPrefix("**") && t.contains("**") { markdownSignals += 1 }
            if t.firstMatch(of: /^\d+\.\s+/) != nil { markdownSignals += 1 }
        }
        // If multiple markdown-like patterns found, it's probably markdown
        return markdownSignals >= 4
    }

    // MARK: - Inline Parsing

    private func parseInline(_ text: String) -> AttributedString {
        // Use Apple's built-in Markdown AttributedString parser
        if let attributed = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) {
            return attributed
        }
        return AttributedString(text)
    }
}

struct MarkdownBlock: Identifiable {
    let id: Int
    let type: BlockType
    let content: String

    enum BlockType {
        case text
        case code(String)
        case heading(Int)
        case bulletList([String])
        case numberedList([String])
        case taskList([(checked: Bool, text: String)])
        case blockquote
        case horizontalRule
        case table([String], [[String]])
    }
}

// MARK: - Code Block

struct CodeBlockView: View {
    let code: String
    let language: String
    @State private var isCopied = false
    
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
            
            // Code content
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
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

// MARK: - Thinking View

// MARK: - Three-Tier Thinking Wrapper

/// Three-tier treatment for the thinking section:
/// - While streaming: shows ThinkingView normally (rolling preview + animated dots)
/// - Once done: Tier 1 = tiny "Show thinking >" text
///              Tier 2 = hover fades in the collapsed banner
///              Tier 3 = click expands full thinking content
struct TieredThinkingView: View {
    let content: String
    let isStreaming: Bool
    @Binding var isExpanded: Bool
    @Binding var hasAutoCollapsed: Bool
    @State private var showBanner = false

    var body: some View {
        if isStreaming {
            // While streaming: show full ThinkingView with rolling preview
            ThinkingView(content: content, isStreaming: isStreaming, isExpanded: $isExpanded)
                .onChange(of: isStreaming) { _, newValue in
                    if !newValue && !hasAutoCollapsed && isExpanded {
                        withAnimation(.spring(response: 0.3)) {
                            isExpanded = false
                        }
                        hasAutoCollapsed = true
                    }
                }
        } else if showBanner {
            // Tier 2→3: Show the thinking banner (faded in from hover)
            ThinkingView(content: content, isStreaming: false, isExpanded: $isExpanded)
                .transition(.opacity)
                .onHover { hovering in
                    if !hovering && !isExpanded {
                        withAnimation(.easeOut(duration: 0.25)) {
                            showBanner = false
                        }
                    }
                }
        } else {
            // Tier 1: Small plain text — "Show thinking >"
            HStack(spacing: 4) {
                Text("Show thinking")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
            .onHover { hovering in
                if hovering {
                    withAnimation(.easeIn(duration: 0.2)) {
                        showBanner = true
                    }
                }
            }
        }
    }
}

struct ThinkingView: View {
    let content: String
    let isStreaming: Bool
    @Binding var isExpanded: Bool

    /// Whether thinking is still in progress (streaming and no final answer yet)
    private var isThinkingActive: Bool {
        isStreaming
    }

    /// During streaming, show only the last 3 lines as a rolling window.
    /// This prevents the thinking box from growing unbounded and causing
    /// LazyVStack deallocation / scroll instability.
    private var rollingPreview: String {
        let lines = content.components(separatedBy: .newlines)
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        let lastLines = lines.suffix(3)
        return lastLines.joined(separator: "\n")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button(action: { withAnimation(.spring(response: 0.3)) { isExpanded.toggle() } }) {
                HStack(spacing: 8) {
                    Image(systemName: "brain")
                        .foregroundStyle(.purple)

                    if isExpanded {
                        Text("Thinking")
                            .foregroundStyle(.secondary)
                    } else if isThinkingActive {
                        // Still thinking — show animated indicator
                        HStack(spacing: 4) {
                            Text("Thinking")
                                .foregroundStyle(.secondary)
                            ThinkingDotsView()
                        }
                    } else {
                        // Done thinking — static label
                        Text("Show thinking")
                            .foregroundStyle(.secondary)
                    }

                    Spacer()

                    Image(systemName: isExpanded ? "chevron.up" : "chevron.right")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
            .buttonStyle(.plain)
            .background(Color.purple.opacity(0.1))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            // Thinking content: 3 modes
            // 1. Streaming + not expanded → 3-line rolling preview (compact)
            // 2. Expanded (streaming or not) → full content, scrollable
            // 3. Not streaming + not expanded → hidden (just the header)
            if isExpanded {
                // Full view — scrollable so even massive thinking blocks
                // don't blow up the bubble height
                ScrollView {
                    Text(content)
                        .font(.body)
                        .italic()
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 300)
                .padding(12)
                .background(Color(nsColor: .controlBackgroundColor).opacity(0.5))
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .transition(.opacity.combined(with: .move(edge: .top)))
            } else if isThinkingActive {
                // Rolling 3-line preview during streaming — small fixed size
                Text(rollingPreview)
                    .font(.callout)
                    .italic()
                    .foregroundStyle(.tertiary)
                    .lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.purple.opacity(0.05))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .contentTransition(.numericText())
            }
        }
    }
}

struct ThinkingDotsView: View {
    @State private var animatingDots = [false, false, false]
    
    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<3) { index in
                Circle()
                    .fill(Color.secondary)
                    .frame(width: 4, height: 4)
                    .opacity(animatingDots[index] ? 1 : 0.3)
            }
        }
        .onAppear {
            startAnimation()
        }
    }
    
    private func startAnimation() {
        for index in 0..<3 {
            withAnimation(
                .easeInOut(duration: 0.6)
                .repeatForever()
                .delay(Double(index) * 0.2)
            ) {
                animatingDots[index] = true
            }
        }
    }
}

// MARK: - Tool Call View

struct ToolCallView: View {
    let toolCall: ToolCall
    @State private var isExpanded = false

    /// Maps all 19 Clyde agent tools to SF Symbols
    private var toolIcon: String {
        switch toolCall.toolName.lowercased() {
        // Shell & filesystem
        case "bash":                    return "terminal"
        case "read_file":               return "doc.text"
        case "write_file":              return "doc.badge.plus"
        case "edit_file":               return "pencil.line"
        case "glob_search":             return "folder.badge.questionmark"
        case "grep_search":             return "text.magnifyingglass"
        // Memory system
        case "memory_read":             return "brain"
        case "memory_write":            return "brain.filled.head.profile"
        case "memory_update":           return "brain"
        case "memory_delete":           return "brain"
        case "memory_search":           return "brain"
        case "memory_list":             return "brain"
        // Search & web
        case "transcript_search":       return "text.magnifyingglass"
        case "web_search":              return "magnifyingglass"
        case "web_fetch":               return "globe"
        // Document creation
        case "create_document":         return "doc.richtext"
        case "create_spreadsheet":      return "tablecells"
        case "create_presentation":     return "rectangle.on.rectangle"
        case "create_pdf":              return "doc.text.fill"
        // Interactive
        case "ask_user":                return "questionmark.circle"
        // Fallback patterns
        default:
            if toolCall.toolName.contains("search") { return "magnifyingglass" }
            if toolCall.toolName.contains("file")   { return "doc" }
            if toolCall.toolName.contains("web")    { return "globe" }
            if toolCall.toolName.contains("memory") { return "brain" }
            return "wrench.and.screwdriver"
        }
    }

    private var statusIcon: String {
        switch toolCall.status {
        case .running: return "circle.dotted"
        case .done:    return "checkmark.circle.fill"
        case .error:   return "exclamationmark.circle.fill"
        }
    }

    private var statusColor: Color {
        switch toolCall.status {
        case .running: return .blue
        case .done:    return .green
        case .error:   return .red
        }
    }

    private var toolColor: Color {
        switch toolCall.status {
        case .running: return .blue
        case .done:    return .green
        case .error:   return .red
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header row — plain HStack with onTapGesture so the entire row
            // (including spacer whitespace) is tappable. SwiftUI's
            // Button(.plain) + contentShape doesn't cover the whole HStack
            // reliably on macOS, so we skip Button entirely for the header.
            HStack(spacing: 8) {
                Image(systemName: toolIcon)
                    .foregroundStyle(toolColor)
                    .frame(width: 16)

                Text(toolCall.toolName)
                    .font(.caption)
                    .fontWeight(.medium)
                    .foregroundStyle(.primary)

                if toolCall.status == .running {
                    ProgressView()
                        .scaleEffect(0.5)
                        .frame(width: 12, height: 12)
                } else {
                    Image(systemName: statusIcon)
                        .font(.caption2)
                        .foregroundStyle(statusColor)
                }

                // Show args preview inline (e.g. "df -h", "SpaceX launch")
                if !toolCall.arguments.isEmpty {
                    Text(toolCall.arguments.prefix(50) + (toolCall.arguments.count > 50 ? "…" : ""))
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                // Show summary tag (e.g. "8 results", "4.2 KB", "saved")
                if let result = toolCall.result, !result.isEmpty {
                    Text(result)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .fill(statusColor.opacity(0.1))
                        )
                }

                Spacer(minLength: 0)

                Image(systemName: isExpanded ? "chevron.up" : "chevron.right")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() }
            }

            if isExpanded {
                VStack(alignment: .leading, spacing: 8) {
                    // Hairline divider — much subtler than the default Divider
                    Rectangle()
                        .fill(toolColor.opacity(0.12))
                        .frame(height: 0.5)

                    // Command / arguments section — grouped card
                    if !toolCall.arguments.isEmpty {
                        ExpandedToolSection(
                            label: toolCall.toolName.uppercased(),
                            accent: toolColor
                        ) {
                            Text(toolCall.arguments)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .textSelection(.enabled)
                        }
                    }

                    // Full output section (scrollable) — grouped card
                    if let output = toolCall.output, !output.isEmpty {
                        ExpandedToolSection(
                            label: "OUTPUT",
                            accent: toolColor
                        ) {
                            ScrollView {
                                Text(output)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .textSelection(.enabled)
                            }
                            .frame(maxHeight: 300)
                        }
                    }
                }
                .padding(.horizontal, 10)
                .padding(.bottom, 10)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(.ultraThinMaterial)
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(toolColor.opacity(0.10))
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        // NOTE: previously had `.geometryGroup()` here as a post-collapse
        // hitbox fix, but it triggers per-frame geometry republication
        // during streaming and causes the AppKit "more constraint passes
        // than views" crash. The hitbox fix actually comes from the
        // animation change (spring → easeInOut) elsewhere, so geometryGroup
        // is unnecessary AND harmful. Do not re-add.
    }
}

/// A grouped section inside an expanded tool card. Renders a small caption
/// label above its content with a frosted-glass background tinted by the
/// parent's accent color so it reads as nested inside the outer card while
/// matching the same material vocabulary.
private struct ExpandedToolSection<Content: View>: View {
    let label: String
    let accent: Color
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(.system(.caption2, design: .monospaced).weight(.medium))
                .foregroundStyle(accent.opacity(0.75))
                .tracking(0.5)
            content()
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(.ultraThinMaterial)
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(accent.opacity(0.06))
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
    }
}

// MARK: - Friendly Tool Label Helper

/// Maps tool names to friendly (active, done) labels for display.
private func friendlyToolLabels(for toolName: String) -> (active: String, done: String) {
    switch toolName.lowercased() {
    case "organize_update_graph":      return ("Updating graph", "Updated graph")
    case "organize_batch_read":        return ("Reading items", "Read items")
    case "organize_batch_write":       return ("Classifying items", "Classified items")
    case "organize_progress":          return ("Checking progress", "Checked progress")
    case "bash":                       return ("Running command", "Ran command")
    case "read_file":                  return ("Reading file", "Read file")
    case "write_file":                 return ("Writing file", "Wrote file")
    case "edit_file":                  return ("Editing file", "Edited file")
    case "glob_search":               return ("Searching files", "Searched files")
    case "grep_search":               return ("Searching code", "Searched code")
    case "memory_read":                return ("Reading memory", "Read memory")
    case "memory_write":               return ("Writing memory", "Wrote memory")
    case "memory_update":              return ("Updating memory", "Updated memory")
    case "memory_delete":              return ("Deleting memory", "Deleted memory")
    case "memory_search":              return ("Searching memory", "Searched memory")
    case "memory_list":                return ("Listing memory", "Listed memory")
    case "transcript_search":          return ("Searching transcripts", "Searched transcripts")
    case "web_search":                 return ("Searching web", "Searched web")
    case "web_fetch":                  return ("Fetching page", "Fetched page")
    case "create_document":            return ("Creating document", "Created document")
    case "create_spreadsheet":         return ("Creating spreadsheet", "Created spreadsheet")
    case "create_presentation":        return ("Creating presentation", "Created presentation")
    case "create_pdf":                 return ("Creating PDF", "Created PDF")
    case "ask_user":                   return ("Asking", "Asked")
    default:
        let name = toolName.replacingOccurrences(of: "_", with: " ")
        return ("Using \(name)", "Used \(name)")
    }
}

/// Maps tool names to SF Symbols.
private func toolIconName(for toolName: String) -> String {
    switch toolName.lowercased() {
    case "bash":                       return "terminal"
    case "read_file":                  return "doc.text"
    case "write_file":                 return "doc.badge.plus"
    case "edit_file":                  return "pencil.line"
    case "glob_search":               return "folder.badge.questionmark"
    case "grep_search":               return "text.magnifyingglass"
    case "memory_read", "memory_write",
         "memory_search", "memory_list",
         "memory_update", "memory_delete": return "brain"
    case "transcript_search":          return "text.magnifyingglass"
    case "web_search":                 return "magnifyingglass"
    case "web_fetch":                  return "globe"
    case "create_document":            return "doc.richtext"
    case "create_spreadsheet":         return "tablecells"
    case "create_presentation":        return "rectangle.on.rectangle"
    case "create_pdf":                 return "doc.text.fill"
    case "ask_user":                   return "questionmark.circle"
    default:
        if toolName.contains("organize") { return "folder.badge.gearshape" }
        if toolName.contains("search")   { return "magnifyingglass" }
        if toolName.contains("file")     { return "doc" }
        if toolName.contains("web")      { return "globe" }
        if toolName.contains("memory")   { return "brain" }
        return "wrench.and.screwdriver"
    }
}

// MARK: - Three-Tier Tool Call Wrapper (single tool call)

/// Wraps a single ToolCallView in the three-tier interaction:
/// - Tier 1 (default): Small plain text "Read file >" — no banner
/// - Tier 2 (hover): Mouse-over fades in the collapsed banner
/// - Tier 3 (click): Click the banner to expand tool details
struct TieredToolCallView: View {
    let toolCall: ToolCall
    var isLastInStreamingTurn: Bool = false
    @State private var showBanner = false

    private var labels: (active: String, done: String) {
        friendlyToolLabels(for: toolCall.toolName)
    }

    private var isRunning: Bool {
        toolCall.status == .running
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if isRunning {
                // While running, show the label with spinner inline (no banner needed)
                HStack(spacing: 6) {
                    Text(labels.active)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ProgressView()
                        .scaleEffect(0.5)
                        .frame(width: 12, height: 12)
                }
                .padding(.vertical, 2)
            } else if showBanner {
                // Tier 2→3: Show the full banner (faded in via hover, clickable to expand)
                ToolCallView(toolCall: toolCall)
                    .transition(.opacity)
                    .onHover { hovering in
                        if !hovering {
                            withAnimation(.easeOut(duration: 0.25)) {
                                showBanner = false
                            }
                        }
                    }
            } else if isLastInStreamingTurn {
                // Tier 1 + shimmer: active work indicator
                ShimmerText(text: labels.done, count: nil)
                    .contentShape(Rectangle())
                    .onHover { hovering in
                        if hovering {
                            withAnimation(.easeIn(duration: 0.2)) {
                                showBanner = true
                            }
                        }
                    }
            } else {
                // Tier 1: Small plain text disclosure — "Read file >"
                HStack(spacing: 4) {
                    Text(labels.done)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(.tertiary)
                }
                .padding(.vertical, 2)
                .contentShape(Rectangle())
                .onHover { hovering in
                    if hovering {
                        withAnimation(.easeIn(duration: 0.2)) {
                            showBanner = true
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Tool Call Group View (collapses repeated tool calls)

/// Three-tier interaction for grouped consecutive same-tool calls:
/// - Tier 1 (default): Small plain text "Read items (8) >" — no banner
/// - Tier 2 (hover): Mouse-over fades in the collapsed banner (green pill)
/// - Tier 3 (click): Click the banner to expand and show individual tool call banners
struct ToolCallGroupView: View {
    let toolName: String
    let toolCalls: [ToolCall]
    let collapsedTextBlocks: [String]
    var isLastInStreamingTurn: Bool = false
    @State private var showBanner = false
    @State private var isExpanded = false

    /// Uses mixed-group label when the group contains different tool names
    private var labels: (active: String, done: String) {
        mixedGroupLabel(toolCalls: toolCalls)
    }

    private var isAnyRunning: Bool {
        toolCalls.contains { $0.status == .running }
    }

    private var hasError: Bool {
        toolCalls.contains { $0.status == .error }
    }

    private var accentColor: Color {
        if hasError { return .red }
        if isAnyRunning { return .blue }
        return .green
    }

    private var icon: String {
        toolIconName(for: toolName)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if isAnyRunning {
                // While running: show label with spinner (no banner)
                HStack(spacing: 6) {
                    Text(labels.active)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if toolCalls.count > 1 {
                        Text("(\(toolCalls.count))")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    ProgressView()
                        .scaleEffect(0.5)
                        .frame(width: 12, height: 12)
                }
                .padding(.vertical, 2)
            } else if showBanner {
                // Tier 2→3: Full banner with expand capability
                VStack(alignment: .leading, spacing: 0) {
                    // Collapsed banner header
                    HStack(spacing: 8) {
                        Image(systemName: icon)
                            .foregroundStyle(accentColor)
                            .frame(width: 16)

                        Text(labels.done)
                            .font(.caption)
                            .fontWeight(.medium)
                            .foregroundStyle(.primary)

                        if toolCalls.count > 1 {
                            Text("(\(toolCalls.count))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(
                                    Capsule()
                                        .fill(accentColor.opacity(0.12))
                                )
                        }

                        if let lastResult = toolCalls.last?.result, !lastResult.isEmpty {
                            Text(lastResult)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(
                                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                                        .fill(accentColor.opacity(0.1))
                                )
                                .lineLimit(1)
                        }

                        Spacer(minLength: 0)

                        Image(systemName: isExpanded ? "chevron.up" : "chevron.right")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() }
                    }

                    // Tier 3: Expanded — show all individual tool calls
                    if isExpanded {
                        VStack(alignment: .leading, spacing: 4) {
                            Rectangle()
                                .fill(accentColor.opacity(0.12))
                                .frame(height: 0.5)

                            ForEach(toolCalls) { tc in
                                ToolCallView(toolCall: tc)
                            }
                        }
                        .padding(.horizontal, 6)
                        .padding(.bottom, 6)
                        .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                }
                .background(
                    ZStack {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(.ultraThinMaterial)
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(accentColor.opacity(0.06))
                    }
                )
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .transition(.opacity)
                .onHover { hovering in
                    if !hovering && !isExpanded {
                        withAnimation(.easeOut(duration: 0.25)) {
                            showBanner = false
                        }
                    }
                }
            } else if isLastInStreamingTurn {
                // Tier 1 + shimmer: active work indicator for last group in streaming turn
                ShimmerText(text: labels.done, count: toolCalls.count > 1 ? toolCalls.count : nil)
                    .contentShape(Rectangle())
                    .onHover { hovering in
                        if hovering {
                            withAnimation(.easeIn(duration: 0.2)) {
                                showBanner = true
                            }
                        }
                    }
            } else {
                // Tier 1: Small plain text disclosure — "Read & classified items (15) >"
                HStack(spacing: 4) {
                    Text(labels.done)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if toolCalls.count > 1 {
                        Text("(\(toolCalls.count))")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(.tertiary)
                }
                .padding(.vertical, 2)
                .contentShape(Rectangle())
                .onHover { hovering in
                    if hovering {
                        withAnimation(.easeIn(duration: 0.2)) {
                            showBanner = true
                        }
                    }
                }
            }
        }
    }
}


// MARK: - Display Block Grouping

/// A display-level grouping of content blocks. Consecutive tool calls (same tool
/// or related tools like read+write) are collapsed into a `.toolGroup`. Everything
/// else passes through as `.single`.
enum DisplayBlock: Identifiable {
    case single(MessageBlock)
    /// A group of 2+ tool calls. `toolName` is the primary tool for label/icon;
    /// `toolCalls` may contain multiple different tool names when related tools alternate.
    case toolGroup(id: UUID, toolName: String, toolCalls: [ToolCall], collapsedText: [String])

    var id: UUID {
        switch self {
        case .single(let block): return block.id
        case .toolGroup(let id, _, _, _): return id
        }
    }
}

/// Tool families: tools that commonly alternate in a workflow and should be grouped together.
/// Returns a family key if the tool belongs to a known family, nil otherwise.
private func toolFamily(for toolName: String) -> String? {
    let lower = toolName.lowercased()
    // Organizer pipeline: batch_read ↔ batch_write ↔ update_graph ↔ progress
    if lower.hasPrefix("organize_") { return "organize" }
    // File operations: read ↔ write ↔ edit
    if lower == "read_file" || lower == "write_file" || lower == "edit_file" { return "file_ops" }
    // Memory operations
    if lower.hasPrefix("memory_") { return "memory" }
    // Search operations
    if lower == "glob_search" || lower == "grep_search" || lower == "web_search" || lower == "transcript_search" { return "search" }
    return nil
}

/// Whether two tool names should be grouped together in the same collapsed block.
private func toolsAreGroupable(_ a: String, _ b: String) -> Bool {
    // Same tool — always groupable
    if a.lowercased() == b.lowercased() { return true }
    // Same family — groupable
    if let fa = toolFamily(for: a), let fb = toolFamily(for: b), fa == fb { return true }
    return false
}

/// Picks a combined friendly label for a mixed-tool group.
private func mixedGroupLabel(toolCalls: [ToolCall]) -> (active: String, done: String) {
    // Count distinct tool verbs
    var toolNames: [String] = []
    for tc in toolCalls {
        let lower = tc.toolName.lowercased()
        if !toolNames.contains(lower) { toolNames.append(lower) }
    }

    // If all same tool, use standard label
    if toolNames.count == 1 {
        return friendlyToolLabels(for: toolNames[0])
    }

    // For organize pipeline: combine the verbs
    let family = toolFamily(for: toolNames[0])
    if family == "organize" {
        let hasRead = toolNames.contains("organize_batch_read")
        let hasWrite = toolNames.contains("organize_batch_write")
        let hasGraph = toolNames.contains("organize_update_graph")
        if hasRead && hasWrite && hasGraph {
            return ("Reading, classifying & updating graph", "Read, classified & updated graph")
        } else if hasRead && hasWrite {
            return ("Reading & classifying items", "Read & classified items")
        } else if hasRead && hasGraph {
            return ("Reading items & updating graph", "Read items & updated graph")
        } else if hasWrite && hasGraph {
            return ("Classifying & updating graph", "Classified & updated graph")
        }
    }

    if family == "file_ops" {
        return ("Working with files", "Worked with files")
    }
    if family == "memory" {
        return ("Working with memory", "Worked with memory")
    }
    if family == "search" {
        return ("Searching", "Searched")
    }

    // Generic fallback: use first tool's label
    return friendlyToolLabels(for: toolNames[0])
}

/// Groups consecutive content blocks for display. Rules:
/// - 2+ consecutive tool calls with the same or related toolName → `.toolGroup`
/// - Text blocks between tool calls that match repetitive filler patterns get absorbed
/// - Everything else stays as `.single`
func groupContentBlocks(blocks: [MessageBlock], toolCalls: [ToolCall]) -> [DisplayBlock] {
    var result: [DisplayBlock] = []
    var i = 0

    while i < blocks.count {
        let block = blocks[i]

        // Check if this is a tool call
        if case .toolCall(let tcId) = block.content,
           let tc = toolCalls.first(where: { $0.id == tcId }) {

            // Look ahead: group consecutive groupable tool calls (with optional filler text)
            var groupToolCalls: [ToolCall] = [tc]
            var groupText: [String] = []
            var j = i + 1

            while j < blocks.count {
                let next = blocks[j]
                switch next.content {
                case .toolCall(let nextTcId):
                    if let nextTc = toolCalls.first(where: { $0.id == nextTcId }),
                       toolsAreGroupable(tc.toolName, nextTc.toolName) {
                        groupToolCalls.append(nextTc)
                        j += 1
                        continue
                    } else {
                        break // Unrelated tool — stop grouping
                    }
                case .text(let text):
                    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                    if isRepetitiveToolText(trimmed) {
                        groupText.append(text)
                        j += 1
                        continue
                    } else {
                        break // Meaningful text — stop grouping
                    }
                default:
                    break // Other block type — stop grouping
                }
                break
            }

            if groupToolCalls.count >= 2 {
                // Collapse into group — use first tool call's ID for stable identity
                result.append(.toolGroup(
                    id: groupToolCalls[0].id,
                    toolName: tc.toolName,
                    toolCalls: groupToolCalls,
                    collapsedText: groupText
                ))
                i = j
            } else {
                // Single tool call — render normally, don't absorb anything
                result.append(.single(block))
                i += 1
            }
        } else {
            // Non-tool-call block — pass through
            result.append(.single(block))
            i += 1
        }
    }

    return result
}

/// Detects repetitive filler text that the model generates between tool calls.
/// Tool-name-agnostic — catches "Let me use X...", "I'll call Y...", etc. for ANY tool.
private func isRepetitiveToolText(_ text: String) -> Bool {
    let lower = text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
    // Very short filler patterns
    if lower.count < 80 && lower.hasPrefix("let me") { return true }
    if lower.count < 80 && lower.hasPrefix("i'll ") && (lower.contains("use") || lower.contains("call")) { return true }
    if lower.count < 80 && lower.hasPrefix("now ") && (lower.contains("use") || lower.contains("call") || lower.contains("let me")) { return true }
    if lower.count < 80 && lower.hasPrefix("next") && (lower.contains("use") || lower.contains("call") || lower.contains("batch")) { return true }
    if lower.count < 80 && lower.hasPrefix("continuing") { return true }
    if lower.count < 80 && lower.hasPrefix("moving on") { return true }
    // Catch "Let me use organize_batch_write..." style
    if lower.contains("let me use") && lower.count < 100 { return true }
    if lower.contains("i'll use") && lower.count < 100 { return true }
    return false
}


// MARK: - Question Card View (ask_user)

struct QuestionCardView: View {
    let question: QuestionData
    @EnvironmentObject private var viewModel: AppViewModel
    @State private var otherText = ""
    @State private var showOther = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Question header
            HStack(spacing: 8) {
                Image(systemName: "questionmark.circle.fill")
                    .foregroundStyle(.blue)
                    .font(.title3)
                Text(question.question)
                    .font(.body)
                    .fontWeight(.medium)
            }

            if question.isAnswered {
                // Answered state — show selection
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                    Text(question.selectedChoice ?? "")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.green.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                // Unanswered — show choice buttons
                VStack(spacing: 6) {
                    ForEach(Array(question.choices.enumerated()), id: \.offset) { index, choice in
                        Button(action: {
                            print("[QuestionCard] Choice tapped: \(choice) questionId=\(question.id)")
                            Task {
                                await viewModel.answerQuestion(questionId: question.id, answer: choice)
                            }
                        }) {
                            choiceLabel(index: index, text: choice)
                        }
                        .buttonStyle(.plain)
                    }

                    // "Other" option
                    if question.allowOther {
                        if showOther {
                            HStack(spacing: 8) {
                                TextField("Type your answer...", text: $otherText)
                                    .textFieldStyle(.plain)
                                    .font(.callout)
                                    .padding(.horizontal, 12)
                                    .padding(.vertical, 8)
                                    .background(Color.secondary.opacity(0.06))
                                    .clipShape(RoundedRectangle(cornerRadius: 8))
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 8)
                                            .stroke(Color.secondary.opacity(0.15), lineWidth: 1)
                                    )
                                    .onSubmit {
                                        print("[QuestionCard] onSubmit fired, otherText='\(otherText)' questionId=\(question.id)")
                                        guard !otherText.trimmingCharacters(in: .whitespaces).isEmpty else {
                                            print("[QuestionCard] onSubmit: text is empty, returning")
                                            return
                                        }
                                        Task {
                                            await viewModel.answerQuestion(questionId: question.id, answer: otherText)
                                        }
                                    }

                                Button(action: {
                                    print("[QuestionCard] Send button tapped, otherText='\(otherText)' questionId=\(question.id)")
                                    guard !otherText.trimmingCharacters(in: .whitespaces).isEmpty else {
                                        print("[QuestionCard] Send button: text is empty, returning")
                                        return
                                    }
                                    Task {
                                        await viewModel.answerQuestion(questionId: question.id, answer: otherText)
                                    }
                                }) {
                                    Image(systemName: "arrow.up.circle.fill")
                                        .font(.title3)
                                        .foregroundStyle(Color.accentColor)
                                }
                                .buttonStyle(.plain)
                                .disabled(otherText.trimmingCharacters(in: .whitespaces).isEmpty)
                            }
                        } else {
                            Button(action: { withAnimation(.spring(response: 0.25)) { showOther = true } }) {
                                HStack(spacing: 10) {
                                    Image(systemName: "text.cursor")
                                        .foregroundStyle(.secondary)
                                        .frame(width: 22)

                                    Text("Other...")
                                        .font(.callout)
                                        .foregroundStyle(.secondary)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .padding(.horizontal, 12)
                                .padding(.vertical, 8)
                                .background(Color.secondary.opacity(0.04))
                                .clipShape(RoundedRectangle(cornerRadius: 8))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 8)
                                        .stroke(Color.secondary.opacity(0.1), lineWidth: 1)
                                )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .padding(14)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(.ultraThinMaterial)
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color.blue.opacity(0.08))
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    @ViewBuilder
    private func choiceLabel(index: Int, text: String) -> some View {
        let letterBadge = Text(choiceLetter(index))
            .font(.caption)
            .fontWeight(.bold)
            .foregroundStyle(.white)
            .frame(width: 22, height: 22)
            .background(Circle().fill(Color.accentColor))

        let choiceText = Text(text)
            .font(.callout)
            .foregroundStyle(.primary)
            .frame(maxWidth: .infinity, alignment: .leading)

        HStack(spacing: 10) {
            letterBadge
            choiceText
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.accentColor.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.accentColor.opacity(0.15), lineWidth: 1)
        )
    }

    private func choiceLetter(_ index: Int) -> String {
        let letters = ["A", "B", "C", "D", "E", "F"]
        return index < letters.count ? letters[index] : "\(index + 1)"
    }
}

// MARK: - Permission Card View

/// Inline card shown when the agent needs access to a folder.
/// Mimics Claude's folder permission UX: folder icon, path, Allow/Deny buttons.
struct PermissionCardView: View {
    let permission: PermissionRequestData
    @EnvironmentObject private var viewModel: AppViewModel

    /// Pretty-print the folder path (replace $HOME with ~)
    private var displayPath: String {
        let folder = permission.folder
        if let homeRange = folder.range(of: "/Users/") {
            let afterUsers = folder[homeRange.upperBound...]
            if let slashIdx = afterUsers.firstIndex(of: "/") {
                return "~" + String(afterUsers[slashIdx...])
            }
            return "~"
        }
        return folder
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack(spacing: 8) {
                Image(systemName: "folder.badge.questionmark")
                    .foregroundStyle(.orange)
                    .font(.title3)
                Text("Folder Access Required")
                    .font(.body)
                    .fontWeight(.medium)
            }

            // Path info
            HStack(spacing: 8) {
                Image(systemName: "folder.fill")
                    .foregroundStyle(.secondary)
                    .font(.callout)
                Text(displayPath)
                    .font(.callout.monospaced())
                    .foregroundStyle(.primary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.06))
            .clipShape(RoundedRectangle(cornerRadius: 8))

            if let granted = permission.granted {
                // Resolved state
                HStack(spacing: 8) {
                    Image(systemName: granted ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(granted ? .green : .red)
                    Text(granted ? "Access granted" : "Access denied")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background((granted ? Color.green : Color.red).opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                // Pending — show Allow/Deny buttons. Use native macOS button
                // styles (.borderedProminent / .bordered) so they pick up the
                // system's gradient + glass treatment automatically.
                HStack(spacing: 10) {
                    Button(action: {
                        Task {
                            await viewModel.respondToPermission(
                                permissionId: permission.id,
                                granted: true,
                                path: permission.folder
                            )
                        }
                    }) {
                        Label("Allow", systemImage: "checkmark")
                            .labelStyle(.titleAndIcon)
                            .font(.callout.weight(.semibold))
                            .frame(minWidth: 72)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .tint(.accentColor)

                    Button(action: {
                        Task {
                            await viewModel.respondToPermission(
                                permissionId: permission.id,
                                granted: false,
                                path: permission.folder
                            )
                        }
                    }) {
                        Label("Deny", systemImage: "xmark")
                            .labelStyle(.titleAndIcon)
                            .font(.callout.weight(.medium))
                            .frame(minWidth: 72)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.large)

                    Spacer()
                }
            }
        }
        .padding(14)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(.ultraThinMaterial)
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color.orange.opacity(0.08))
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}


// MARK: - Plan Card View (task_plan / task_update / task_complete)

/// Inline card showing the live state of a multi-step task plan.
/// Updates automatically as the agent emits plan_state events for each
/// task_update / task_expand / task_complete call.
struct PlanCardView: View {
    let plan: PlanData
    @State private var isExpanded: Bool = true

    private var statusColor: Color {
        switch plan.status {
        case "pending_approval": return .yellow
        case "active":           return .blue
        case "completed":        return .green
        case "failed":           return .red
        case "cancelled":        return .gray
        default:                 return .secondary
        }
    }

    private var headerIcon: String {
        switch plan.status {
        case "pending_approval": return "questionmark.circle.fill"
        case "active":           return "play.circle.fill"
        case "completed":        return "checkmark.circle.fill"
        case "failed":           return "xmark.circle.fill"
        case "cancelled":        return "minus.circle.fill"
        default:                 return "list.bullet.rectangle"
        }
    }

    private var statusLabel: String {
        switch plan.status {
        case "pending_approval": return "Awaiting approval"
        case "active":           return "In progress"
        case "completed":        return "Completed"
        case "failed":           return "Failed"
        case "cancelled":        return "Cancelled"
        default:                 return plan.status.capitalized
        }
    }

    private var displayedOutputFile: String? {
        guard let f = plan.outputFile, !f.isEmpty else { return nil }
        if let homeRange = f.range(of: "/Users/") {
            let after = f[homeRange.upperBound...]
            if let slash = after.firstIndex(of: "/") {
                return "~" + String(after[slash...])
            }
        }
        return f
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Header: status icon + goal + chevron — plain HStack with
            // onTapGesture so the entire row (including spacer whitespace)
            // is tappable.
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: headerIcon)
                    .font(.title3)
                    .foregroundStyle(statusColor)
                    .padding(.top, 1)

                VStack(alignment: .leading, spacing: 4) {
                    Text(plan.goal)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.primary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .multilineTextAlignment(.leading)

                    HStack(spacing: 6) {
                        Text(statusLabel)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(statusColor)
                        Text("·")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                        Text("\(plan.doneSteps)/\(plan.totalSteps) steps")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if let outFile = displayedOutputFile {
                            Text("·")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                            Image(systemName: "doc")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                            Text(outFile)
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                    }
                }

                Spacer(minLength: 0)

                Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
                    .frame(width: 22, height: 22)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture {
                withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() }
            }

            // Progress bar — capsule track with a glossy gradient fill that
            // picks up the plan's status color. Animates with a soft spring
            // so step transitions feel tactile.
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule(style: .continuous)
                        .fill(Color.primary.opacity(0.06))
                        .overlay(
                            Capsule(style: .continuous)
                                .strokeBorder(Color.primary.opacity(0.05), lineWidth: 0.5)
                        )
                    Capsule(style: .continuous)
                        .fill(
                            LinearGradient(
                                colors: [
                                    statusColor.opacity(0.95),
                                    statusColor.opacity(0.70),
                                ],
                                startPoint: .leading,
                                endPoint: .trailing
                            )
                        )
                        .frame(width: max(0, geo.size.width * plan.progressFraction))
                        .animation(.spring(response: 0.45, dampingFraction: 0.85),
                                   value: plan.progressFraction)
                }
            }
            .frame(height: 6)

            // Steps
            if isExpanded {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(plan.steps) { step in
                        PlanStepRow(step: step)
                    }
                }
                .padding(.top, 4)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(14)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(.ultraThinMaterial)
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(statusColor.opacity(0.08))
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        // Removed .geometryGroup() — see ToolCallView for the explanation.
    }
}

private struct PlanStepRow: View {
    let step: PlanStepData

    private var icon: String {
        if step.isDone { return "checkmark.circle.fill" }
        if step.isInProgress { return "circle.dotted" }
        if step.isFailed { return "xmark.circle.fill" }
        if step.isSkipped { return "arrow.right.circle" }
        return "circle"
    }

    private var color: Color {
        if step.isDone { return .green }
        if step.isInProgress { return .blue }
        if step.isFailed { return .red }
        if step.isSkipped { return .gray }
        return .secondary
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .font(.callout)
                .foregroundStyle(color)
                .frame(width: 18)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(step.description)
                        .font(.callout)
                        .foregroundStyle(step.isDone ? .secondary : .primary)
                        .strikethrough(step.isSkipped, color: .secondary)
                    if step.wasInterrupted {
                        Text("interrupted")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.orange)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.orange.opacity(0.12))
                            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    }
                    if step.type == "user_interjection" {
                        Text("user query")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.purple)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.purple.opacity(0.12))
                            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                    }
                    Spacer(minLength: 0)
                }

                if !step.notes.isEmpty {
                    Text(step.notes)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(2)
                }

                if step.isFailed && !step.failedAttempts.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(step.failedAttempts.enumerated()), id: \.offset) { _, attempt in
                            HStack(alignment: .top, spacing: 4) {
                                Image(systemName: "exclamationmark.triangle.fill")
                                    .font(.caption2)
                                    .foregroundStyle(.red.opacity(0.7))
                                Text(attempt)
                                    .font(.caption2)
                                    .foregroundStyle(.red.opacity(0.8))
                                    .lineLimit(2)
                            }
                        }
                    }
                }
            }
        }
    }
}


// MARK: - Attachments View

struct AttachmentsView: View {
    let attachments: [Attachment]
    
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 100))], spacing: 8) {
            ForEach(attachments) { attachment in
                AttachmentThumbnail(attachment: attachment)
            }
        }
    }
}

struct AttachmentThumbnail: View {
    let attachment: Attachment
    @State private var isHovered = false
    @State private var eventMonitor: Any?

    private var docInfo: (icon: String, color: Color) {
        let ext = (attachment.fileName as NSString).pathExtension.lowercased()
        switch ext {
        case "pdf":   return ("doc.text.fill", .red)
        case "docx":  return ("doc.richtext", .blue)
        case "xlsx":  return ("tablecells", .green)
        case "pptx":  return ("rectangle.on.rectangle", .orange)
        case "csv":   return ("tablecells", .teal)
        case "json":  return ("curlybraces", .purple)
        case "py":    return ("chevron.left.forwardslash.chevron.right", .yellow)
        case "swift": return ("swift", .orange)
        default:      return ("doc", .secondary)
        }
    }

    private var tempFileURL: URL {
        let tmpDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("clyde-attachments", isDirectory: true)
        try? FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        let url = tmpDir.appendingPathComponent(attachment.fileName)
        if !FileManager.default.fileExists(atPath: url.path),
           let data = Data(base64Encoded: attachment.base64Data) {
            try? data.write(to: url)
        }
        return url
    }

    var body: some View {
        VStack(spacing: 3) {
            if attachment.type == .image, let data = Data(base64Encoded: attachment.base64Data),
               let nsImage = NSImage(data: data) {
                Image(nsImage: nsImage)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 56, height: 56)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            } else if attachment.type == .video {
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color.purple.opacity(0.15))
                    .frame(width: 56, height: 56)
                    .overlay(
                        Image(systemName: "film")
                            .font(.body)
                            .foregroundStyle(.purple)
                    )
            } else {
                RoundedRectangle(cornerRadius: 6)
                    .fill(docInfo.color.opacity(0.15))
                    .frame(width: 56, height: 56)
                    .overlay(
                        Image(systemName: docInfo.icon)
                            .font(.body)
                            .foregroundStyle(docInfo.color)
                    )
            }

            Text(attachment.fileName)
                .font(.system(size: 9))
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(width: 60)
        .contentShape(Rectangle())
        .onTapGesture {
            QuickLookManager.shared.show(url: tempFileURL)
        }
        .onHover { hovering in
            isHovered = hovering
            if hovering {
                if eventMonitor == nil {
                    eventMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                        if event.keyCode == 49 {
                            QuickLookManager.shared.show(url: self.tempFileURL)
                            return nil
                        }
                        return event
                    }
                }
            } else {
                if let monitor = eventMonitor {
                    NSEvent.removeMonitor(monitor)
                    eventMonitor = nil
                }
            }
        }
        .draggable(tempFileURL) {
            HStack(spacing: 4) {
                Image(systemName: docInfo.icon)
                    .font(.caption)
                Text(attachment.fileName)
                    .font(.caption)
                    .lineLimit(1)
            }
            .padding(6)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 6))
        }
    }
}

// MARK: - File References View

struct FileReferencesView: View {
    let files: [FileReference]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "doc.on.doc")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Generated Files")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.secondary)
            }

            ForEach(files) { file in
                FileReferenceRow(file: file)
            }
        }
        .padding(10)
        .background(Color.blue.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.blue.opacity(0.12), lineWidth: 1)
        )
    }
}

struct FileReferenceRow: View {
    let file: FileReference
    @State private var isHovered = false
    @State private var eventMonitor: Any?

    private var fileURL: URL {
        URL(fileURLWithPath: file.originalPath)
    }

    private var iconColor: Color {
        switch file.docInfo.color {
        case "red":    return .red
        case "blue":   return .blue
        case "green":  return .green
        case "orange": return .orange
        case "teal":   return .teal
        case "purple": return .purple
        case "yellow": return .yellow
        case "pink":   return .pink
        default:       return .secondary
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            // File icon
            Image(systemName: file.docInfo.icon)
                .font(.title3)
                .foregroundStyle(iconColor)
                .frame(width: 28, height: 28)

            // File info
            VStack(alignment: .leading, spacing: 1) {
                Text(file.fileName)
                    .font(.body)
                    .fontWeight(.medium)
                    .lineLimit(1)
                    .truncationMode(.middle)

                HStack(spacing: 6) {
                    if file.fileSize > 0 {
                        Text(file.formattedSize)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Text(file.originalPath)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.head)
                }
            }

            Spacer()

            // Action buttons
            HStack(spacing: 4) {
                // Quick Look preview (native macOS panel)
                Button(action: { QuickLookManager.shared.show(url: fileURL) }) {
                    Image(systemName: "eye")
                        .font(.caption)
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.purple)
                .help("Quick Look (⎵)")

                // Open in default app
                Button(action: openFile) {
                    Image(systemName: "arrow.up.forward.square")
                        .font(.caption)
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.blue)
                .help("Open with default app")

                // Save As…
                Button(action: saveAs) {
                    Image(systemName: "square.and.arrow.down")
                        .font(.caption)
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.green)
                .help("Save As…")

                // Reveal in Finder
                Button(action: revealInFinder) {
                    Image(systemName: "folder")
                        .font(.caption)
                        .frame(width: 24, height: 24)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Reveal in Finder")
            }
            .opacity(isHovered ? 1.0 : 0.6)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(isHovered ? Color.primary.opacity(0.06) : Color.clear)
        )
        .contentShape(Rectangle())
        .onHover { hovering in
            isHovered = hovering
            // Add/remove spacebar monitor only while hovering to avoid leaks
            if hovering {
                if eventMonitor == nil {
                    eventMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                        if event.keyCode == 49 { // 49 = spacebar
                            QuickLookManager.shared.show(url: self.fileURL)
                            return nil // consume the event
                        }
                        return event
                    }
                }
            } else {
                if let monitor = eventMonitor {
                    NSEvent.removeMonitor(monitor)
                    eventMonitor = nil
                }
            }
        }
        .onTapGesture(count: 2) { openFile() }
        .draggable(fileURL) {
            HStack(spacing: 4) {
                Image(systemName: file.docInfo.icon)
                    .font(.caption)
                Text(file.fileName)
                    .font(.caption)
                    .lineLimit(1)
            }
            .padding(6)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 6))
        }
    }

    private func openFile() {
        NSWorkspace.shared.open(fileURL)
    }

    private func revealInFinder() {
        NSWorkspace.shared.activateFileViewerSelecting([fileURL])
    }

    private func saveAs() {
        let sourceURL = fileURL
        let fileName = file.fileName
        DispatchQueue.main.async {
            let panel = NSSavePanel()
            panel.nameFieldStringValue = fileName
            panel.canCreateDirectories = true
            panel.title = "Save As"

            guard panel.runModal() == .OK, let destURL = panel.url else { return }
            do {
                try FileManager.default.copyItem(at: sourceURL, to: destURL)
                print("[SaveAs] File saved to: \(destURL.path)")
            } catch {
                print("[SaveAs] Failed to save file: \(error)")
            }
        }
    }
}

// MARK: - Quick Look Manager (native QLPreviewPanel)

import Quartz

/// Singleton that drives the native macOS Quick Look panel — the same floating
/// preview window you get when pressing spacebar on a file in Finder.
/// Bypasses the NSResponder chain by directly assigning itself as data source.
class QuickLookManager: NSObject, QLPreviewPanelDataSource, QLPreviewPanelDelegate {
    static let shared = QuickLookManager()

    private var previewURL: URL?

    func show(url: URL) {
        previewURL = url
        guard let panel = QLPreviewPanel.shared() else { return }
        panel.dataSource = self
        panel.delegate = self

        if panel.isVisible {
            // Already open — just reload with new file
            panel.reloadData()
        } else {
            panel.makeKeyAndOrderFront(nil)
        }
    }

    // MARK: - QLPreviewPanelDataSource

    func numberOfPreviewItems(in panel: QLPreviewPanel!) -> Int {
        return previewURL != nil ? 1 : 0
    }

    func previewPanel(_ panel: QLPreviewPanel!, previewItemAt index: Int) -> (any QLPreviewItem)! {
        return previewURL as? NSURL
    }
}


// MARK: - Spiral Brush Path

/// The Clyde spiral brushstroke path, normalized to a 0–1 unit square.
/// Original viewBox: -20 -15 140 145 → offset & scale applied.
struct SpiralBrushPath: Shape {
    func path(in rect: CGRect) -> Path {
        let w = rect.width
        let h = rect.height
        // Map from original SVG coords (viewBox -20 -15 140 145) to rect
        func pt(_ x: Double, _ y: Double) -> CGPoint {
            CGPoint(
                x: rect.minX + (x + 20) / 140 * w,
                y: rect.minY + (y + 15) / 145 * h
            )
        }
        var p = Path()
        p.move(to: pt(84, 16))
        p.addCurve(to: pt(96, 92),  control1: pt(104, 36), control2: pt(112, 70))
        p.addCurve(to: pt(22, 106), control1: pt(80, 114), control2: pt(48, 120))
        p.addCurve(to: pt(0, 36),   control1: pt(-4, 92),  control2: pt(-14, 60))
        p.addCurve(to: pt(66, -2),  control1: pt(14, 12),  control2: pt(42, -4))
        p.addCurve(to: pt(98, 32),  control1: pt(84, 0),   control2: pt(98, 14))
        p.addCurve(to: pt(64, 72),  control1: pt(98, 52),  control2: pt(82, 68))
        p.addCurve(to: pt(22, 46),  control1: pt(44, 76),  control2: pt(26, 64))
        p.addCurve(to: pt(46, 12),  control1: pt(18, 28),  control2: pt(30, 14))
        p.addCurve(to: pt(72, 36),  control1: pt(60, 10),  control2: pt(72, 22))
        p.addCurve(to: pt(52, 56),  control1: pt(72, 48),  control2: pt(62, 56))
        return p
    }
}


// MARK: - Streaming Cursor (Spiral Animations)

/// Animation style for the streaming spiral cursor.
enum SpiralAnimation: CaseIterable {
    case drawingLoop       // A: draws in, holds, erases, loops
    case drawingLoopSpin   // B: same + rotation
    case endlessStroke     // C: segment chases around the path
    case endlessStrokeSpin // D: same + rotation
}

// MARK: - Shimmer Text Effect

/// A text view with a traveling shimmer highlight effect.
/// Used on the last visible tiered tool text when the turn is still streaming,
/// to indicate active work is happening.
struct ShimmerText: View {
    let text: String
    let count: Int?
    @State private var phase: CGFloat = 0

    @ViewBuilder
    private var textContent: some View {
        HStack(spacing: 4) {
            Text(text)
                .font(.caption)
            if let count = count, count > 1 {
                Text("(\(count))")
                    .font(.caption2)
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 8, weight: .semibold))
        }
    }

    var body: some View {
        HStack(spacing: 4) {
            // Base text in secondary color
            textContent
                .foregroundStyle(.secondary)
                .overlay {
                    // Shimmer highlight — gradient masked to letter shapes only
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [
                                .clear,
                                .white.opacity(0.7),
                                .white.opacity(0.9),
                                .white.opacity(0.7),
                                .clear
                            ],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.4)
                        .offset(x: -geo.size.width * 0.2 + phase * (geo.size.width * 1.4))
                    }
                    .mask { textContent }
                }
        }
        .padding(.vertical, 2)
        .onAppear {
            withAnimation(
                .linear(duration: 2.0)
                .repeatForever(autoreverses: false)
            ) {
                phase = 1
            }
        }
    }
}

struct StreamingCursor: View {
    @EnvironmentObject private var viewModel: AppViewModel

    /// Which animation variant to show. Randomly chosen on init.
    @State private var variant: SpiralAnimation

    // Drawing loop states
    @State private var trimEnd: CGFloat = 0
    @State private var trimStart: CGFloat = 0
    @State private var opacity: Double = 1.0

    // Endless stroke states
    @State private var dashPhase: CGFloat = 0

    // Spin state
    @State private var rotation: Double = 0

    // Breathing glow
    @State private var glowOpacity: Double = 0.85

    // Timer for drawing loop cycle
    @State private var loopTimer: Timer?
    @State private var phase: Int = 0  // 0=draw, 1=hold, 2=erase

    // Rotating status phrases
    @State private var currentPhraseIndex: Int = Int.random(in: 0..<100)
    @State private var phraseTimer: Timer?
    @State private var phraseOpacity: Double = 1.0

    private static let phrases: [String] = [
        "Thinking really hard...",
        "Consulting the void...",
        "Rummaging through neurons...",
        "Summoning coherence...",
        "Untangling spaghetti thoughts...",
        "Defragmenting brain...",
        "Asking the universe...",
        "Wrangling entropy...",
        "Bribing the muse...",
        "Herding semicolons...",
        "Negotiating with logic...",
        "Vibing with the data...",
        "Chasing rabbit holes...",
        "Charging flux capacitor...",
        "Staring into the abyss...",
        "Assembling nonsense...",
        "Tickling the tokens...",
        "Overthinking it...",
        "Communing with electrons...",
        "Wrestling the algorithm...",
        "Plucking ideas from thin air...",
        "Calibrating vibes...",
        "Untangling the obvious...",
        "Manifesting an answer...",
        "Having a little think...",
        "Counting to infinity...",
        "Consulting ancient scrolls...",
        "Making stuff up...",
        "Doing math in my head...",
        "Pretending to know things...",
        "Chasing loose thoughts...",
        "Reorganizing the chaos...",
        "Warming up the GPU...",
        "Reading the fine print...",
        "Redistributing neurons...",
        "Googling it internally...",
        "Sifting through the noise...",
        "Folding the probability space...",
        "Chewing on this one...",
        "Running on vibes...",
        "Connecting the dots...",
        "Inventing new words...",
        "Pulling levers...",
        "Guesstimating confidently...",
        "Resisting the urge to hallucinate...",
        "Mainlining attention heads...",
        "Parsing your chaos...",
        "Performing digital jazz...",
        "Having a moment...",
        "Almost there... probably...",
        "Quantum tunneling through your question...",
        "Reorganizing the furniture upstairs...",
        "Consulting the ancient tokens...",
        "Applying unnecessary complexity...",
        "Rerouting through nonsense...",
        "Decrypting your vibes...",
        "Simulating confidence...",
        "Gazing into the training data...",
        "Inventing plausible-sounding things...",
        "Letting the chaos settle...",
        "Achieving enlightenment briefly...",
        "Arguing with myself internally...",
        "Building a tiny mental model...",
        "Checking if this makes sense...",
        "Definitely not hallucinating...",
        "Asking my imaginary colleagues...",
        "Refreshing the vibe cache...",
        "Locating my train of thought...",
        "Assembling the pieces badly, then well...",
        "Translating feelings into words...",
        "Performing cognitive jazz...",
        "Extracting signal from noise...",
        "Thinking at maximum velocity...",
        "Borrowing wisdom from the void...",
        "Making something out of nothing...",
        "Cross-referencing the unknowable...",
        "Synthesizing pure speculation...",
        "Knitting thoughts into sentences...",
        "Defying the heat death of ideas...",
        "Suspending disbelief momentarily...",
        "Polishing a rough draft in my head...",
        "Chasing the perfect word...",
        "Warming up the answer oven...",
        "Consulting my inner committee...",
        "Sorting through infinite possibilities...",
        "Applying liberal amounts of logic...",
        "Waking up the sleepy neurons...",
        "Calculating something impressive...",
        "Loading personality module...",
        "Trying very hard right now...",
        "Absolutely cooking...",
        "Becoming one with the question...",
        "Channeling productive confusion...",
        "Resolving a minor existential crisis...",
        "Finishing my thought any second now...",
        "Constructing elaborate sentences...",
        "Wrestling with word order...",
        "Confidently uncertain...",
        "Reaching peak deliberation...",
        "Worth the wait, probably...",
    ]

    private let pineGradient = LinearGradient(
        colors: [
            Color(red: 0.24, green: 0.48, blue: 0.26).opacity(0.8),  // #3d7a42
            Color(red: 0.42, green: 0.72, blue: 0.42)                 // #6ab86a
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    init() {
        _variant = State(initialValue: SpiralAnimation.allCases.randomElement() ?? .drawingLoop)
    }

    var body: some View {
        HStack(spacing: 8) {
            Group {
                switch variant {
                case .drawingLoop:
                    drawingLoopView(spinning: false)
                case .drawingLoopSpin:
                    drawingLoopView(spinning: true)
                case .endlessStroke:
                    endlessStrokeView(spinning: false)
                case .endlessStrokeSpin:
                    endlessStrokeView(spinning: true)
                }
            }
            .frame(width: 18, height: 18)

            Text(Self.phrases[currentPhraseIndex])
                .font(.caption)
                .foregroundStyle(.secondary)
                .opacity(phraseOpacity)
                .animation(.easeInOut(duration: 0.3), value: phraseOpacity)
        }
        .onAppear {
            // Start phrase cycling — random shuffle every 3 seconds
            phraseTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in
                // Fade out
                withAnimation(.easeInOut(duration: 0.3)) {
                    phraseOpacity = 0.0
                }
                // After fade out, pick a new random phrase and fade back in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                    var nextIndex: Int
                    repeat {
                        nextIndex = Int.random(in: 0..<Self.phrases.count)
                    } while nextIndex == currentPhraseIndex && Self.phrases.count > 1
                    currentPhraseIndex = nextIndex
                    withAnimation(.easeInOut(duration: 0.3)) {
                        phraseOpacity = 1.0
                    }
                }
            }
        }
        .onDisappear {
            phraseTimer?.invalidate()
            phraseTimer = nil
        }
        // Publish the cursor's screen-space position via PreferenceKey so
        // NeuralNetBackgroundView's tracking lines can anchor to it.
        //
        // Earlier this block was removed entirely because SwiftUI's
        // bound-preference system was propagating every tiny change as a
        // separate update, firing "Bound preference tried to update
        // multiple times per frame" under streaming load. The actual
        // crash source turned out to be a DIFFERENT codepath (the
        // ScrollActionDispatcher's proxy.scrollTo from the 100ms auto-
        // scroll task landing mid-layout). With that fixed, this block
        // is safe to restore — with two defensive measures:
        //
        //   1. Round the published coordinates to a 4pt grid. Sub-pixel
        //      jitter from layout reflow during streaming no longer
        //      produces "different" CGPoint values, so SwiftUI's built-
        //      in preference-key dedup catches the updates and doesn't
        //      propagate them. The 4pt grid is coarse enough to swallow
        //      all the real-world jitter but fine enough that the user
        //      can't see the lines snap.
        //   2. The GeometryReader's frame read happens in `.background`
        //      so it doesn't participate in the StreamingCursor's own
        //      layout calculation — purely an observer.
        .background(GeometryReader { geo in
            Color.clear
                .preference(
                    key: StreamingCursorOriginKey.self,
                    value: {
                        let midX = geo.frame(in: .global).midX
                        let midY = geo.frame(in: .global).midY
                        // Round to nearest 4pt grid to swallow sub-pixel jitter
                        return CGPoint(
                            x: (midX / 4).rounded() * 4,
                            y: (midY / 4).rounded() * 4
                        )
                    }()
                )
        })
    }

    // MARK: - Drawing Loop

    @ViewBuilder
    private func drawingLoopView(spinning: Bool) -> some View {
        SpiralBrushPath()
            .trim(from: trimStart, to: trimEnd)
            .stroke(
                pineGradient,
                style: StrokeStyle(lineWidth: 2.2, lineCap: .round)
            )
            .opacity(glowOpacity)
            .rotationEffect(.degrees(spinning ? rotation : 0))
            .onAppear {
                startDrawingLoop()
                if spinning {
                    withAnimation(.linear(duration: 3.5).repeatForever(autoreverses: false)) {
                        rotation = 360
                    }
                }
                withAnimation(.easeInOut(duration: 2.5).repeatForever()) {
                    glowOpacity = 1.0
                }
            }
            .onDisappear { loopTimer?.invalidate() }
    }

    private func startDrawingLoop() {
        // Phase 0: Draw in
        trimEnd = 0
        trimStart = 0
        withAnimation(.easeOut(duration: 1.26)) {
            trimEnd = 1.0
        }

        // Schedule hold → erase → restart
        loopTimer?.invalidate()
        loopTimer = Timer.scheduledTimer(withTimeInterval: 1.26, repeats: false) { _ in
            // Phase 1: Hold for 0.56s, then erase
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.56) {
                // Phase 2: Erase (move trimStart to 1)
                withAnimation(.easeIn(duration: 0.98)) {
                    trimStart = 1.0
                }
                // Phase 3: Reset and restart
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                    trimEnd = 0
                    trimStart = 0
                    startDrawingLoop()
                }
            }
        }
    }

    // MARK: - Endless Stroke

    @ViewBuilder
    private func endlessStrokeView(spinning: Bool) -> some View {
        SpiralBrushPath()
            .stroke(
                pineGradient,
                style: StrokeStyle(
                    lineWidth: 2.2,
                    lineCap: .round,
                    dash: [28, 65],  // ~30% visible, ~70% gap (scaled for shape)
                    dashPhase: dashPhase
                )
            )
            .opacity(glowOpacity)
            .rotationEffect(.degrees(spinning ? rotation : 0))
            .onAppear {
                withAnimation(.linear(duration: 2.2).repeatForever(autoreverses: false)) {
                    dashPhase = -93  // total dash+gap cycle
                }
                if spinning {
                    withAnimation(.linear(duration: 4.0).repeatForever(autoreverses: false)) {
                        rotation = 360
                    }
                }
                withAnimation(.easeInOut(duration: 2.0).repeatForever()) {
                    glowOpacity = 1.0
                }
            }
    }
}

// Preview disabled - run full app with Cmd+R to test
//#Preview {
//    VStack(spacing: 16) {
//        MessageBubbleView(message: ChatMessage(
//            role: .user,
//            content: "Hello! Can you help me with **Swift** programming?"
//        ))
//        
//        MessageBubbleView(message: ChatMessage(
//            role: .assistant,
//            content: "Sure! Here's a simple example:\n\n```swift\nfunc greet(name: String) {\n    print(\"Hello, \\(name)!\")\n}\n```",
//            toolCalls: [ToolCall(toolName: "search")],
//            thinkingContent: "Let me think about the best way to explain this..."
//        ))
//        
//        MessageBubbleView(message: ChatMessage(
//            role: .assistant,
//            content: "Processing",
//            isStreaming: true
//        ))
//    }
//    .padding()
//    .frame(width: 600)
//}

// MARK: - Long text rendering with markdown safety valve

/// Renders text as markdown, but for very long text (>50K chars) defaults to
/// plain `Text` to avoid UI stalls and shows a tappable banner letting the user
/// opt into full markdown parsing anyway.
private struct LongTextRenderer: View {
    let text: String
    private static let markdownCutoff = 50_000

    @State private var forceMarkdown = false

    var body: some View {
        if text.count <= Self.markdownCutoff || forceMarkdown {
            MarkdownText(text)
                .textSelection(.enabled)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                    Text("Markdown formatting disabled — message is \(text.count.formatted()) characters")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Button("Render anyway") {
                        forceMarkdown = true
                    }
                    .font(.caption2)
                    .buttonStyle(.plain)
                    .foregroundStyle(.blue)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .fill(Color.orange.opacity(0.08))
                )

                Text(text)
                    .textSelection(.enabled)
            }
        }
    }
}


// iMessage bubble shape is now in ChatBubbleShape.swift

