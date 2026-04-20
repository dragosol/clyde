//
//  ContentView.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//  Reconstructed 2026-04-14 after bindfs-mount write incident. Includes
//  the backend-agnostic notification pill (no MLX-specific labels —
//  uses `agentManager.agentStatus` which is generic across all backend
//  kinds surfaced via BackendKind).
//

import SwiftUI

struct ContentView: View {
    @EnvironmentObject var agentManager: AgentManager
    @EnvironmentObject var viewModel: AppViewModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    @State private var columnVisibility: NavigationSplitViewVisibility = .all
    @State private var chatAreaCenter: CGPoint = CGPoint(x: 400, y: 300)

    /// Width the inspector adds/removes when toggled.
    private let inspectorWidth: CGFloat = 300

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 250, ideal: 280, max: 350)
        } detail: {
            detailContent
        }
        .toolbar {
            // New conversation — leading side (next to the sidebar toggle).
            ToolbarItem(placement: .navigation) {
                Button(action: { viewModel.createNewConversation() }) {
                    Image(systemName: "square.and.pencil")
                        .font(.body.weight(.medium))
                        .offset(y: -2)
                }
                .help("New Conversation (⌘N)")
                .keyboardShortcut("n", modifiers: .command)
            }
        }
        .background {
            NeuralNetBackgroundView(
                isActive: viewModel.isStreaming,
                cursorTracker: viewModel.cursorTracker,
                fallbackOrigin: chatAreaCenter
            )
            .allowsHitTesting(false)
        }
        .environmentObject(viewModel)
        .sheet(isPresented: $viewModel.showDebug) {
            DebugView(agentManager: agentManager)
                .frame(minWidth: 700, minHeight: 500)
        }
        .onChange(of: viewModel.showInspector) { _, isShowing in
            resizeWindowForInspector(showing: isShowing)
        }
        // Open Settings → Permissions when the agent requests it
        .onChange(of: viewModel.shouldOpenPermissions) { _, shouldOpen in
            if shouldOpen {
                viewModel.shouldOpenPermissions = false
                openSettings()
            }
        }
        .onAppear {
            viewModel.connectAgentManager(agentManager)
            // Clean up empty conversations from previous launches
            // that were created eagerly but never used.
            viewModel.conversations
                .filter { $0.messages.isEmpty }
                .forEach { viewModel.deleteConversation($0) }
            // Don't auto-select or create a conversation on launch.
            // Show centered input instead. Conversation created only
            // when user sends first message.
            viewModel.selectedConversation = nil
        }
    }

    // MARK: - Detail content (chat + inspector + overlays)

    @State private var welcomeInputText = ""
    /// Drives the center → bottom slide when the user sends their
    /// first message from the welcome screen. The bottom Spacer
    /// disappears, the spring layout animation slides the input
    /// down, and after a short delay we create the real conversation.
    @State private var welcomeInputAtBottom = false

    @ViewBuilder
    private var detailContent: some View {
        Group {
            if let conversation = viewModel.selectedConversation {
                ChatView(conversation: conversation)
                    .id(conversation.id)
            } else {
                // ── Welcome state: centered input, no conversation yet ──
                VStack {
                    Spacer()
                    VStack(spacing: 0) {
                        InputTextEditor(text: $welcomeInputText, onSend: {
                            let text = welcomeInputText.trimmingCharacters(in: .whitespacesAndNewlines)
                            guard !text.isEmpty else { return }
                            // Guard against double-send: clear text immediately
                            // so a second onSend (rapid Enter) sees empty and bails.
                            let captured = text
                            welcomeInputText = ""
                            // Phase 1: animate input to bottom position
                            withAnimation(.spring(response: 0.45, dampingFraction: 0.82)) {
                                welcomeInputAtBottom = true
                            }
                            // Phase 2: after the slide completes, create
                            // conversation and send — ChatView takes over
                            // with its own input already at the bottom.
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                                welcomeInputAtBottom = false
                                viewModel.createNewConversation()
                                Task {
                                    await viewModel.sendMessage(content: captured)
                                }
                            }
                        })
                        .padding(.horizontal, 12)
                        .padding(.top, 8)
                        HStack {
                            Spacer()
                        }
                        .padding(.horizontal, 12)
                        .padding(.bottom, 8)
                        .padding(.top, 4)
                    }
                    .frame(maxWidth: 600)
                    .glassEffect(.regular, in: .rect(cornerRadius: 16))
                    .padding(.horizontal, 8)
                    // Bottom spacer only present when centered. Removing
                    // it slides the input to the bottom of the container.
                    if !welcomeInputAtBottom {
                        Spacer()
                    } else {
                        Spacer().frame(height: 8)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .transition(.opacity)
            }
        }
        // Top pill overlay — centers on the chat area, stays aligned with
        // the text input regardless of the inspector's state. Uses
        // liquid-glass (.glassEffect) rather than frosted material.
        .overlay(alignment: .top) {
            ZStack {
                if let bannerInfo = agentBannerInfo {
                    HStack(spacing: 6) {
                        if bannerInfo.isAnimating {
                            ProgressView()
                                .scaleEffect(0.5)
                                .frame(width: 12, height: 12)
                        } else {
                            Circle()
                                .fill(bannerInfo.color)
                                .frame(width: 6, height: 6)
                        }
                        Text(bannerInfo.text)
                            .font(.caption)
                            .foregroundStyle(.primary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    .glassEffect(.regular, in: .capsule)
                    .padding(.top, 8)
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            .animation(.easeInOut(duration: 0.2), value: agentBannerInfo?.text)
        }
        // Connection status is surfaced via the notification pill
        // (agentBannerInfo) when in an abnormal state — no always-on
        // "Connected" chip cluttering the toolbar area.
        .inspector(isPresented: $viewModel.showInspector) {
            InspectorPanelView(onExpandToWindow: {
                openWindow(id: "asset-graph")
            })
            .inspectorColumnWidth(min: 260, ideal: 300, max: 360)
        }
        // Inspector toggle — flush-right to match Xcode's inspector
        // button. `ToolbarSpacer(.flexible)` (macOS 26) pushes the
        // button past any intervening toolbar items so it sits against
        // the window's trailing edge, regardless of inspector state.
        // Plain native toolbar icon — no glass pill — to match Xcode.
        .toolbar {
            ToolbarSpacer(.flexible, placement: .primaryAction)
            ToolbarItem(placement: .primaryAction) {
                Button(action: {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        viewModel.showInspector.toggle()
                    }
                }) {
                    Image(systemName: "sidebar.trailing")
                        .symbolVariant(viewModel.showInspector ? .fill : .none)
                        .font(.body.weight(.medium))
                }
                .help(viewModel.showInspector ? "Hide Inspector" : "Show Inspector")
                .keyboardShortcut("i", modifiers: [.command, .option])
            }
        }
        .background(
            GeometryReader { proxy in
                Color.clear
                    .onAppear { chatAreaCenter = CGPoint(x: proxy.size.width / 2, y: proxy.size.height / 2) }
                    .onChange(of: proxy.size) { _, newSize in
                        chatAreaCenter = CGPoint(x: newSize.width / 2, y: newSize.height / 2)
                    }
            }
        )
    }

    // MARK: - Window resize for inspector

    /// Smoothly expands/contracts the window so the inspector doesn't
    /// compress the chat area. Uses NSAnimationContext to match SwiftUI's
    /// animation duration.
    private func resizeWindowForInspector(showing: Bool) {
        guard let window = NSApplication.shared.keyWindow else { return }
        var frame = window.frame
        let screen = window.screen?.visibleFrame ?? NSScreen.main!.visibleFrame

        if showing {
            // Try expanding rightward first
            if frame.maxX + inspectorWidth <= screen.maxX {
                frame.size.width += inspectorWidth
            } else if frame.origin.x - inspectorWidth >= screen.minX {
                // Expand leftward if no room on the right
                frame.size.width += inspectorWidth
                frame.origin.x -= inspectorWidth
            } else {
                // Screen too narrow — expand as much as possible
                frame = NSRect(x: screen.minX, y: frame.origin.y,
                               width: screen.width, height: frame.height)
            }
        } else {
            // Contract: shrink from whichever side we expanded toward
            frame.size.width = max(frame.size.width - inspectorWidth, 700)
        }

        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.25
            ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            window.animator().setFrame(frame, display: true)
        }
    }

    // MARK: - Banner (backend-agnostic)

    /// Derives a compact pill description from `agentManager.agentStatus`.
    /// All labels use backend-neutral wording — no mention of MLX,
    /// llama.cpp, Ollama, or any specific runtime. The underlying
    /// `AgentStatus` enum already describes lifecycle states abstractly;
    /// this layer just renders them.
    ///
    /// Returns nil when there's nothing worth surfacing (idle, running,
    /// inferring — the cursor already animates during those states).
    private var agentBannerInfo: (isAnimating: Bool, color: Color, text: String)? {
        // Live metrics now live in the inspector's PerformanceTile — no
        // need to duplicate them on the pill. Pill is reserved for
        // transient + lifecycle signals.
        // Transient confirmation messages take priority over status.
        if let t = agentManager.transientMessage {
            return (false, .green, t)
        }
        switch agentManager.agentStatus {
        case .idle:
            // Lazy-load hint — visible until the user sends a message
            return (false, .gray, "Send a message to wake the model and start chatting")
        case .starting:
            return (true, .yellow, "Loading agent...")
        case .loadingModel:
            return (true, .yellow, "Loading model...")
        case .running, .inferring:
            // Healthy — no pill. Streaming cursor handles inference.
            return nil
        case .restarting:
            return (true, .orange, "Restarting backend...")
        case .hibernating:
            return (true, .gray, "Hibernating...")
        case .hibernated:
            return (false, .gray, "Send a message to wake the model and start chatting")
        case .waking:
            return (true, .yellow, "Waking up...")
        case .stopping:
            return (false, .gray, "Stopping...")
        case .stopped:
            return (false, .red, "Stopped")
        case .error(let detail):
            return (false, .red, "Error: \(detail)")
        }
    }
}

// MARK: - Empty State

struct EmptyStateView: View {
    @EnvironmentObject var viewModel: AppViewModel

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 80))
                .foregroundStyle(.tertiary)

            Text("Welcome to Clyde")
                .font(.largeTitle)
                .fontWeight(.bold)

            Text("Your premium AI chat interface")
                .font(.title3)
                .foregroundStyle(.secondary)

            Button(action: { viewModel.createNewConversation() }) {
                Label("New Conversation", systemImage: "plus.circle.fill")
                    .font(.headline)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 10)
                    .glassEffect(.regular, in: .capsule)
            }
            .buttonStyle(.plain)
            .keyboardShortcut("n", modifiers: .command)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
