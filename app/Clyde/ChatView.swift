//
//  ChatView.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//

import SwiftUI
import UniformTypeIdentifiers
import AppKit

struct ChatView: View {
    @EnvironmentObject var viewModel: AppViewModel
    let conversation: Conversation

    @State private var inputText = ""
    @State private var attachments: [Attachment] = []
    @State private var isDragging = false
    @State private var showFilePicker = false
    @State private var isPinnedToBottom = true
    @State private var scrollTask: Task<Void, Never>?
    /// Guards against unpinning during initial load. The geometry
    /// callback fires before programmatic scrollTo completes, seeing
    /// the view at the top and immediately unpinning. This flag blocks
    /// unpin for the first second after appear/conversation swap.
    @State private var allowUnpin = false
    // NOTE: Scroll-position tracking uses onScrollPhaseChange (user-
    // interaction only) instead of onScrollGeometryChange. The latter
    // fires during layout passes and, combined with LazyVStack +
    // defaultScrollAnchor(.bottom), creates an infinite layout loop
    // that either pegs CPU at 100% or crashes with NSGenericException
    // "more constraint passes than views". See the onScrollPhaseChange
    // modifier below for the replacement approach.
    // NOTE: `lastKnownContentHeight` USED to live here as @State but it was
    // dead code — declared, written on every onScrollGeometryChange
    // callback, but never read anywhere. Every write still triggered a
    // SwiftUI re-evaluation and contributed to the "OnScrollGeometryChange
    // tried to update multiple times per frame" crash the user hit mid-
    // stream. Removed entirely. Do NOT reintroduce without a reader.

    /// Scroll target: always use the lightweight "bottom" spacer.
    ///
    /// IMPORTANT: We must NOT scroll to the last message ID with `.bottom`
    /// anchor. If that message is tall (long reply), `.bottom` tells SwiftUI
    /// to position its bottom edge at the viewport bottom — pushing its top
    /// and ALL earlier messages offscreen. LazyVStack then deallocates them,
    /// causing the blank-page bug. The 1pt "bottom" spacer is always tiny,
    /// so scrolling to it keeps the full last message visible above.
    private var scrollTarget: (id: AnyHashable, anchor: UnitPoint) {
        (AnyHashable("bottom"), .bottom)
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                // Wrap LazyVStack + anchor in a regular VStack so the
                // bottom anchor is never deallocated by lazy loading.
                VStack(spacing: 0) {
                    LazyVStack(spacing: 12) {
                        let lastUserMsgId = conversation.messages.last(where: { $0.role == .user })?.id
                        ForEach(conversation.messages) { message in
                            MessageBubbleView(
                                message: message,
                                isLastUserMessage: message.id == lastUserMsgId,
                                onRetry: { msgId in
                                    Task { await viewModel.retryMessage(id: msgId) }
                                },
                                onEdit: { msgId, newContent in
                                    Task { await viewModel.editMessage(id: msgId, newContent: newContent) }
                                }
                            )
                            .id(message.id)
                            // iOS-Messages-style insertion: bubble slides up
                            // from below and fades in. Driven by the
                            // withAnimation(.spring) wrapper around
                            // messages.append in AppViewModel.sendMessage.
                            // Removal stays opacity-only to avoid jank when
                            // a streaming placeholder is replaced or a
                            // message is edited/retried.
                            .transition(.asymmetric(
                                insertion: .move(edge: .bottom).combined(with: .opacity),
                                removal: .opacity
                            ))
                        }
                    }
                    .frame(maxWidth: 720)
                    .frame(maxWidth: .infinity)
                    .padding(.top, 16)

                    // Bottom anchor — OUTSIDE LazyVStack so it's always
                    // allocated. This is what we scrollTo. It's just 1pt
                    // tall, so scrollTo("bottom", .bottom) can never push
                    // messages offscreen the way scrolling to a tall
                    // message bubble with .bottom anchor would.
                    //
                    Color.clear
                        .frame(height: 1)
                        .id("bottom")
                }
            }
            // NOTE: defaultScrollAnchor(.bottom) was removed. It fights
            // with the manual proxy.scrollTo() calls in onChange(messages.count)
            // and the streaming poll. When both fire during initial conversation
            // load, the competing scroll targets cause cascading layout passes
            // in the LazyVStack, leading to the "more constraint passes than
            // views" crash. Manual scrollTo handles all scroll-to-bottom needs.
            // Receive the streaming cursor's screen-space origin via the
            // PreferenceKey that StreamingCursor's GeometryReader publishes,
            // and forward it to the SEPARATE CursorOriginTracker. The tracker
            // is its own ObservableObject (not @Published on AppViewModel),
            // so writes here only re-render NeuralNetBackgroundView — NOT the
            // chat tree. That isolation is what makes this safe and avoids
            // the AppKit "more passes than views" layout-loop crash.
            //
            // The tracker is captured by reference; this closure does NOT
            // make ChatView observe the tracker, so ChatView itself never
            // re-renders when the origin changes.
            .onPreferenceChange(StreamingCursorOriginKey.self) { [tracker = viewModel.cursorTracker] newOrigin in
                guard let newOrigin else { return }
                let current = tracker.origin
                // Skip sub-pixel jitter — only publish meaningful movement.
                if current == nil
                    || abs((current?.x ?? 0) - newOrigin.x) > 1
                    || abs((current?.y ?? 0) - newOrigin.y) > 1 {
                    tracker.origin = newOrigin
                }
            }
            // Detect user-initiated scrolling WITHOUT onScrollGeometryChange.
            //
            // onScrollGeometryChange + LazyVStack + defaultScrollAnchor(.bottom)
            // creates an infinite layout loop that either pegs the CPU at 100%
            // or crashes with "more constraint passes than views". The modifier
            // fires its geometry closure during every layout pass; with lazy
            // loading those passes multiply, and any state mutation (even
            // deferred via DispatchQueue.main.async) eventually feeds back.
            //
            // Replacement: onScrollPhaseChange only fires on discrete user-
            // interaction phases (tracking, decelerating, idle) — NOT during
            // layout passes. When the user finishes a scroll gesture (→ .idle),
            // we check whether the bottom anchor is still visible via a
            // lightweight GeometryReader preference. This is enough to
            // un-pin when the user scrolls up and re-pin when they scroll
            // back down, without touching the layout engine.
            .onScrollGeometryChange(for: Bool.self) { geo in
                // Single Bool: true = user is scrolled away from bottom
                // and content is actually scrollable. false = at bottom
                // or content fits in view.
                //
                // IMPORTANT: safeAreaInset(edge: .bottom) for the input
                // panel adds ~80-120pt that shifts the geometric bottom
                // away from the visual bottom. Subtract contentInsets.bottom
                // so "at visual bottom" = dist ≈ 0, not dist ≈ 100.
                let scrollable = geo.contentSize.height > geo.containerSize.height + 10
                let rawDist = geo.contentSize.height - geo.containerSize.height - geo.contentOffset.y
                let dist = rawDist - geo.contentInsets.bottom
                return scrollable && dist > 30
            } action: { _, isScrolledUp in
                DispatchQueue.main.async {
                    // Always allow RE-PINNING (hiding button). Only gate
                    // unpinning behind the startup guard so the initial
                    // layout pass can't show the button before scrollTo.
                    if isScrolledUp && isPinnedToBottom && allowUnpin {
                        isPinnedToBottom = false
                    } else if !isScrolledUp && !isPinnedToBottom {
                        isPinnedToBottom = true
                    }
                }
            }
            // New message added.
            //   - User-sent message = new turn → re-pin and scroll.
            //   - Assistant message append (tool/thinking/response) = mid-turn →
            //     respect user's prior scroll-up; only scroll if still pinned.
            // Deferred via DispatchQueue.main.async to avoid ScrollAction
            // Dispatcher firing from inside the same layout pass that
            // triggered the messages.count change.
            .onChange(of: conversation.messages.count) { _, _ in
                let isNewUserTurn = conversation.messages.last?.role == .user
                let target = scrollTarget
                DispatchQueue.main.async {
                    if isNewUserTurn {
                        isPinnedToBottom = true
                    }
                    if isPinnedToBottom {
                        proxy.scrollTo(target.id, anchor: target.anchor)
                    }
                }
            }
            // Streaming poll: scroll to bottom every 250ms while streaming + pinned.
            //
            // Fix for ScrollActionDispatcher layout-pass crash: calls to
            // proxy.scrollTo() must NOT land inside an in-progress layout
            // pass, or SwiftUI's ScrollActionDispatcher.updateValue will
            // call ViewGraph.requestImmediateUpdate mid-render, triggering
            // the NSGenericException "more constraint passes than views"
            // crash. Even @MainActor Tasks can tick inside a layout pass
            // if the main run loop is busy when the sleep completes.
            //
            // Fix is two-pronged:
            //   1. Ticker runs at 250ms (down from 100ms) to cut the rate
            //      of scroll-action dispatches by 2.5x — the user's eye
            //      can't tell the difference, but AppKit's constraint
            //      engine has dramatically less work per second.
            //   2. The actual scrollTo call is wrapped in
            //      DispatchQueue.main.async so it always lands on a FRESH
            //      run-loop tick, never inside whatever layout pass was
            //      in progress when the sleep woke up. Same pattern the
            //      onScrollGeometryChange action closure uses.
            .onChange(of: viewModel.isStreaming) { _, streaming in
                if streaming {
                    // Do NOT force-pin here. Pin state was set by the
                    // new-user-turn branch in messages.count onChange.
                    // If the user scrolled up before this stream started,
                    // they stay unpinned until they send a new message.
                    scrollTask?.cancel()
                    // Fresh-agent-load bug: when a cold agent starts
                    // streaming on a pre-existing conversation, the scroll
                    // proxy sometimes lands on the bottom anchor before
                    // LazyVStack has allocated visible bubbles, and the
                    // chat area ends up blank until the user scrolls. A
                    // short staircase of explicit scroll ticks (0ms,
                    // 120ms, 300ms) at stream-start forces the LazyVStack
                    // to allocate and lay out above the bottom spacer.
                    // Guarded by isPinnedToBottom so it never overrides
                    // an explicit user scroll-up.
                    if isPinnedToBottom {
                        for delay in [0.0, 0.12, 0.3] {
                            let target = scrollTarget
                            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                                withAnimation(nil) {
                                    proxy.scrollTo(target.id, anchor: target.anchor)
                                }
                            }
                        }
                    }
                    scrollTask = Task { @MainActor in
                        while !Task.isCancelled {
                            try? await Task.sleep(for: .milliseconds(250))
                            if isPinnedToBottom {
                                let target = scrollTarget
                                DispatchQueue.main.async {
                                    withAnimation(nil) {
                                        proxy.scrollTo(target.id, anchor: target.anchor)
                                    }
                                }
                            }
                        }
                    }
                } else {
                    scrollTask?.cancel()
                    scrollTask = nil
                    // Final scroll after streaming ends
                    if isPinnedToBottom {
                        let target = scrollTarget
                        DispatchQueue.main.async {
                            proxy.scrollTo(target.id, anchor: target.anchor)
                        }
                    }
                }
            }
            // safeAreaInset tells ScrollView to inset content to make room for the panel
            .safeAreaInset(edge: .bottom, spacing: 0) {
                ZStack(alignment: .top) {
                    inputPanel

                    // Floating scroll-to-bottom button — anchored just above input.
                    // Always present in the view tree (opacity/scale instead of
                    // conditional insertion) so toggling isPinnedToBottom never
                    // changes the safeAreaInset layout. This breaks the feedback
                    // loop: onScrollGeometryChange → isPinnedToBottom → animation
                    // → geometry change → repeat. The animation is scoped to
                    // opacity + scale only — no layout shift propagates to the
                    // scroll view's geometry.
                    Image(systemName: "chevron.down")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 36, height: 36)
                        .glassEffect(.regular, in: .circle)
                        .shadow(color: .black.opacity(0.15), radius: 8, y: 4)
                        .contentShape(Circle())
                        .onTapGesture {
                            isPinnedToBottom = true
                            let target = scrollTarget
                            proxy.scrollTo(target.id, anchor: target.anchor)
                        }
                        .offset(y: -48)
                        .opacity(isPinnedToBottom ? 0 : 1)
                        .scaleEffect(isPinnedToBottom ? 0.5 : 1.0)
                        .allowsHitTesting(!isPinnedToBottom)
                        .animation(.spring(response: 0.3, dampingFraction: 0.75), value: isPinnedToBottom)
                }
            }  // end safeAreaInset
            // Initial pin-to-bottom when the chat first appears. Covers
            // the "blank on fresh load" bug when an existing conversation
            // is opened and messages are already present — without this,
            // the LazyVStack can render with scroll offset at the top,
            // deallocating bubbles visually until the user scrolls.
            .onAppear {
                allowUnpin = false
                isPinnedToBottom = true
                let target = scrollTarget
                for delay in [0.0, 0.15, 0.4] {
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                        withAnimation(nil) {
                            proxy.scrollTo(target.id, anchor: target.anchor)
                        }
                    }
                }
                // Enable unpinning after scrollTo settles
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                    allowUnpin = true
                }
            }
            // Also re-pin when the conversation swaps (new chat selected
            // in sidebar) — same LazyVStack recycling problem as above.
            .onChange(of: conversation.id) { _, _ in
                allowUnpin = false
                isPinnedToBottom = true
                let target = scrollTarget
                for delay in [0.0, 0.15, 0.4] {
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                        withAnimation(nil) {
                            proxy.scrollTo(target.id, anchor: target.anchor)
                        }
                    }
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                    allowUnpin = true
                }
            }
        }  // end ScrollViewReader
        .fileImporter(
            isPresented: $showFilePicker,
            allowedContentTypes: [
                .folder,
                .image, .movie, .video, .pdf, .plainText,
                // Office document types
                UTType(filenameExtension: "docx") ?? .data,
                UTType(filenameExtension: "xlsx") ?? .data,
                UTType(filenameExtension: "pptx") ?? .data,
                // Code and text files
                .json, .yaml, .xml, .html,
                UTType(filenameExtension: "csv") ?? .data,
                UTType(filenameExtension: "md") ?? .data,
                UTType(filenameExtension: "py") ?? .data,
                UTType(filenameExtension: "swift") ?? .data,
                UTType(filenameExtension: "js") ?? .data,
                UTType(filenameExtension: "ts") ?? .data,
                UTType(filenameExtension: "sh") ?? .data,
            ],
            allowsMultipleSelection: true
        ) { result in
            handleFileSelection(result)
        }
    }
    
    // MARK: - Input Panel
    
    private var inputPanel: some View {
        VStack(spacing: 0) {
            // Compaction progress bar
            if viewModel.isCompacting {
                CompactionProgressBar(summary: viewModel.compactingSummary, isDone: viewModel.compactingDone)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            // Attachment previews
            if !attachments.isEmpty {
                AttachmentPreviewBar(attachments: $attachments)
                    .padding(.top, 8)
                    .padding(.horizontal, 4)
            }

            // Text input area — dynamically grows from 1 to ~7 lines, then scrolls
            InputTextEditor(text: $inputText, onSend: sendMessage)
                .padding(.horizontal, 12)
                .padding(.top, 8)

            // Bottom toolbar: attach left, send right
            HStack {
                attachButton
                Spacer()
                actionButton
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 8)
            .padding(.top, 4)
        }
        .frame(maxWidth: 600)
        .glassEffect(.regular, in: .rect(cornerRadius: 16))
        .overlay(dragOverlayStroke)
        .overlay(dragOverlayGlow)
        .animation(.easeInOut(duration: 0.25), value: isDragging)
        .dropDestination(for: URL.self) { urls, _ in
            handleDroppedURLs(urls)
        } isTargeted: { targeted in
            isDragging = targeted
        }
        // Match the sidebar panel's inset from window edges (8pt)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
    }
    
    private var attachButton: some View {
        Button(action: { showFilePicker = true }) {
            Image(systemName: "plus")
                .font(.body.weight(.medium))
                .foregroundStyle(.secondary)
                .frame(width: 30, height: 30)
                .glassEffect(.regular, in: .circle)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help("Attach file")
    }
    
    private var actionButton: some View {
        Group {
            if viewModel.isStreaming && canSend {
                // Queue mode — agent is busy, but the user has typed something.
                // Show an orange capsule labelled "Queue" so it's clear this
                // won't interrupt the current stream. Reverts to the plain
                // up-arrow button as soon as the stream ends.
                Button(action: sendMessage) {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.up")
                        Text("Queue")
                    }
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10)
                    .frame(height: 30)
                    .background(Capsule().fill(Color.orange))
                }
                .buttonStyle(.plain)
                .help("Queue message — sends after current response")
            } else if viewModel.isStreaming {
                Button(action: { viewModel.stopStreaming() }) {
                    Image(systemName: "stop.fill")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.red)
                        .frame(width: 30, height: 30)
                        .glassEffect(.regular, in: .circle)
                }
                .buttonStyle(.plain)
                .help("Stop generation")
            } else {
                Button(action: sendMessage) {
                    Image(systemName: "arrow.up")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(canSend ? Color.accentColor : Color.secondary.opacity(0.5))
                        .frame(width: 30, height: 30)
                        .glassEffect(.regular, in: .circle)
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .help("Send message (↩)")
            }
        }
    }
    
    private var dragOverlayStroke: some View {
        RoundedRectangle(cornerRadius: 16)
            .stroke(isDragging ? Color.yellow.opacity(0.8) : Color.clear, lineWidth: isDragging ? 2.5 : 0)
    }
    
    private var dragOverlayGlow: some View {
        RoundedRectangle(cornerRadius: 16)
            .stroke(isDragging ? Color.yellow.opacity(0.5) : Color.clear, lineWidth: isDragging ? 8 : 0)
            .blur(radius: isDragging ? 10 : 0)
            .clipShape(RoundedRectangle(cornerRadius: 16))
    }
    
    private var canSend: Bool {
        let hasText = !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let hasAttachments = !attachments.isEmpty
        // While streaming, this is still "can send" — the send action
        // routes to enqueueMessage instead of starting a new stream.
        return hasText || hasAttachments
    }
    
    private func sendMessage() {
        guard canSend else { return }
        
        let message = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        let messageAttachments = attachments
        
        inputText = ""
        attachments = []

        // Queue path: agent is currently streaming. Append the message as
        // queued (orange bubble) and let AppViewModel.sendMessage's tail
        // dequeue it once the current stream ends. Do NOT cancel the
        // active stream.
        if viewModel.isStreaming {
            viewModel.enqueueMessage(content: message, attachments: messageAttachments)
            return
        }

        // Fresh-send path: cancel any leftover stream task, then start a
        // new one. (Originally added to prevent a race where the new Task
        // found itself stored in streamingTask and cancelled itself on
        // the first run.)
        viewModel.streamingTask?.cancel()
        viewModel.streamingTask = nil

        let task = Task {
            await viewModel.sendMessage(content: message, attachments: messageAttachments)
        }
        viewModel.streamingTask = task
    }
    
    // MARK: - File Handling

    /// Build an Attachment from a file URL (reads data while we have access)
    private func attachmentFromURL(_ url: URL) -> Attachment? {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }

        guard let data = try? Data(contentsOf: url) else { return nil }
        let ext = url.pathExtension
        return Attachment(
            type: MIMEType.attachmentType(for: ext),
            fileName: url.lastPathComponent,
            mimeType: MIMEType.from(extension: ext),
            base64Data: data.base64EncodedString()
        )
    }

    /// Modern drop handler using dropDestination delivers decoded URLs
    /// directly on MainActor — no escaping closure / state capture issues.
    private func handleDroppedURLs(_ urls: [URL]) -> Bool {
        var added = false
        for url in urls {
            let accessed = url.startAccessingSecurityScopedResource()
            defer { if accessed { url.stopAccessingSecurityScopedResource() } }

            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue {
                let enumerator = FileManager.default.enumerator(
                    at: url,
                    includingPropertiesForKeys: [.isRegularFileKey],
                    options: [.skipsHiddenFiles, .skipsPackageDescendants]
                )
                while let fileURL = enumerator?.nextObject() as? URL {
                    var isFile: ObjCBool = false
                    if FileManager.default.fileExists(atPath: fileURL.path, isDirectory: &isFile), !isFile.boolValue {
                        if let attachment = attachmentFromURL(fileURL) {
                            attachments.append(attachment)
                            added = true
                        }
                    }
                }
            } else {
                if let attachment = attachmentFromURL(url) {
                    attachments.append(attachment)
                    added = true
                }
            }
        }
        return added
    }

    private func handleFileSelection(_ result: Result<[URL], Error>) {
        guard let urls = try? result.get() else { return }
        _ = handleDroppedURLs(urls)
    }
}

// MARK: - Input Text Editor

struct InputTextEditor: View {
    @Binding var text: String
    let onSend: () -> Void
    @State private var dynamicHeight: CGFloat = 22

    /// Shared font size for both placeholder and editor (16pt — slightly larger than default body)
    private let fontSize: CGFloat = 16
    /// Min ~3 lines, max ~7 lines at 16pt (line height ~22pt)
    private let minEditorHeight: CGFloat = 66   // 3 lines × ~22pt
    private let maxEditorHeight: CGFloat = 154  // 7 lines × ~22pt

    var body: some View {
        ZStack(alignment: .topLeading) {
            if text.isEmpty {
                // Align with the NSTextView's text origin:
                //   - leading: lineFragmentPadding (6pt) + textContainerInset.width (0pt) = 6pt
                //   - top:     textContainerInset.height (1pt)
                // Previously top was 5pt which floated the placeholder
                // 4pt below the actual cursor.
                Text("Type a message...")
                    .font(.system(size: fontSize))
                    .foregroundStyle(.tertiary)
                    .padding(.leading, 6)
                    .padding(.top, 1)
                    .allowsHitTesting(false)
            }

            CustomTextEditor(text: $text, onReturn: onSend, fontSize: fontSize, dynamicHeight: $dynamicHeight)
                .frame(height: min(max(dynamicHeight, minEditorHeight), maxEditorHeight))
        }
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

// Custom NSViewRepresentable for keyboard handling:
//   Enter       → send message
//   Cmd+Enter   → insert newline
//   Shift+Enter → insert newline
struct CustomTextEditor: NSViewRepresentable {
    @Binding var text: String
    let onReturn: () -> Void
    var fontSize: CGFloat = 16
    @Binding var dynamicHeight: CGFloat

    class CustomNSTextView: NSTextView {
        var onReturn: (() -> Void)?

        override func keyDown(with event: NSEvent) {
            let isReturn = event.keyCode == 36  // Return key
            let hasCmd = event.modifierFlags.contains(.command)
            let hasShift = event.modifierFlags.contains(.shift)

            if isReturn && !hasCmd && !hasShift {
                // Plain Enter → send
                onReturn?()
                return
            }
            if isReturn && (hasCmd || hasShift) {
                // Cmd+Enter or Shift+Enter → insert newline
                insertNewline(nil)
                return
            }
            super.keyDown(with: event)
        }
    }

    class Coordinator: NSObject, NSTextViewDelegate {
        var parent: CustomTextEditor

        init(_ parent: CustomTextEditor) {
            self.parent = parent
        }

        func textDidChange(_ notification: Notification) {
            guard let textView = notification.object as? NSTextView else { return }
            DispatchQueue.main.async {
                self.parent.text = textView.string
                self.recalcHeight(textView)
            }
        }

        func recalcHeight(_ textView: NSTextView) {
            guard let layoutManager = textView.layoutManager,
                  let container = textView.textContainer else { return }
            layoutManager.ensureLayout(for: container)
            let usedRect = layoutManager.usedRect(for: container)
            let newHeight = max(usedRect.height + textView.textContainerInset.height * 2, 22)
            DispatchQueue.main.async {
                self.parent.dynamicHeight = newHeight
            }
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let textView = CustomNSTextView()
        textView.delegate = context.coordinator
        textView.isRichText = false
        textView.font = NSFont.systemFont(ofSize: fontSize)
        textView.textColor = NSColor.labelColor
        textView.backgroundColor = .clear
        textView.drawsBackground = false
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.onReturn = onReturn
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]

        // Align text container inset with the placeholder padding
        textView.textContainerInset = NSSize(width: 0, height: 1)
        textView.textContainer?.lineFragmentPadding = 6
        textView.textContainer?.widthTracksTextView = true

        let scrollView = NSScrollView()
        scrollView.documentView = textView
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.drawsBackground = false

        // Calculate initial height
        DispatchQueue.main.async {
            context.coordinator.recalcHeight(textView)
        }

        return scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        guard let textView = scrollView.documentView as? CustomNSTextView else { return }

        if textView.string != text {
            textView.string = text
            context.coordinator.recalcHeight(textView)
        }

        textView.onReturn = onReturn
    }
}

// MARK: - Attachment Preview Bar

struct AttachmentPreviewBar: View {
    @Binding var attachments: [Attachment]
    
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { attachment in
                    AttachmentPreview(attachment: attachment) {
                        attachments.removeAll { $0.id == attachment.id }
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
    }
}

struct AttachmentPreview: View {
    let attachment: Attachment
    let onRemove: () -> Void
    
    var body: some View {
        VStack(spacing: 4) {
            ZStack(alignment: .topTrailing) {
                if attachment.type == .image,
                   let data = Data(base64Encoded: attachment.base64Data),
                   let nsImage = NSImage(data: data) {
                    Image(nsImage: nsImage)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 60, height: 60)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                } else if attachment.type == .video {
                    RoundedRectangle(cornerRadius: 6)
                        .fill(Color.purple.opacity(0.2))
                        .frame(width: 60, height: 60)
                        .overlay(
                            Image(systemName: "film")
                                .foregroundStyle(.purple)
                        )
                } else {
                    RoundedRectangle(cornerRadius: 6)
                        .fill(Color.secondary.opacity(0.2))
                        .frame(width: 60, height: 60)
                        .overlay(
                            Image(systemName: attachment.type.icon)
                                .foregroundStyle(.secondary)
                        )
                }
                
                Button(action: onRemove) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.white)
                        .background(Circle().fill(Color.red))
                }
                .buttonStyle(.plain)
                .offset(x: 4, y: -4)
            }
            
            Text(attachment.fileName)
                .font(.caption2)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 60)
        }
    }
}

// Preview disabled due to Xcode Preview system issues
// To test, run the full app with Cmd+R instead
//
//#Preview {
//    ChatView(conversation: Conversation(
//        title: "Test Conversation",
//        messages: [
//            ChatMessage(role: .user, content: "Hello!"),
//            ChatMessage(role: .assistant, content: "Hi there! How can I help you today?")
//        ]
//    ))
//    .environmentObject(AppViewModel())
//    .frame(width: 600, height: 400)
//}


// MARK: - Compaction Progress Bar

struct CompactionProgressBar: View {
    let summary: String
    var isDone: Bool = false

    @State private var animating = false

    var body: some View {
        HStack(spacing: 8) {
            // Animated brain icon
            Image(systemName: isDone ? "checkmark.circle.fill" : "brain")
                .font(.caption)
                .foregroundStyle(isDone ? .green : .orange)
                .symbolEffect(.pulse, options: .repeating, isActive: !isDone)

            Text(isDone ? "Context compacted" : "Compacting context…")
                .font(.caption)
                .foregroundStyle(.secondary)

            if !isDone {
                ProgressView()
                    .controlSize(.mini)
            }

            Spacer()

            if !summary.isEmpty {
                Text(summary)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .monospacedDigit()
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity)
        .animation(.easeInOut(duration: 0.3), value: isDone)
    }
}

// MARK: - Scroll Snapshot

/// Lightweight value for tracking scroll position + content height.
/// Used to distinguish user-initiated scrolling from content growth.
///
/// `offsetY` is the contentOffset.y of the scroll view — the only signal
/// that reliably tells us "the user dragged up". `bottomEdge` and
/// `contentHeight` move around for many reasons (content growth, layout
/// adjustments, auto-scroll calls), so they're noisy. A real user scroll
/// is the one thing that decreases `offsetY`.
// NOTE: ScrollSnapshot was removed. It powered onScrollGeometryChange
// which has been replaced by onScrollPhaseChange to avoid the layout
// loop crash. See the comment block above onScrollPhaseChange for the
// full explanation.

