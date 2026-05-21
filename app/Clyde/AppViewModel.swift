//
//  AppViewModel.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//

import Foundation
import SwiftUI
import Combine


/// Tracks the screen-space position of the streaming status text / animated
/// logo so the NeuralNetBackgroundView can anchor its waves to it.
///
/// Why this is its OWN ObservableObject instead of a property on AppViewModel:
/// the cursor moves on every layout pass during streaming. If the origin lived
/// on AppViewModel, every cursor movement would fire AppViewModel's
/// objectWillChange — which would re-render every view that observes the view
/// model, INCLUDING the message bubbles that contain the cursor itself. The
/// re-render re-measures the cursor, publishes a new position, fires the
/// observer, etc. Eventually AppKit catches the layout-pass count exceeding
/// the view count and crashes with "more passes than views in window".
///
/// Splitting it into its own ObservableObject means writing the origin only
/// re-renders views that explicitly observe `CursorOriginTracker`. The chat
/// tree never observes it, so no re-render cascade.
@MainActor
final class CursorOriginTracker: ObservableObject {
    @Published var origin: CGPoint? = nil
}

@MainActor
class AppViewModel: ObservableObject {
    @Published var conversations: [Conversation] = []
    @Published var selectedConversation: Conversation?
    @Published var searchText = ""
    @Published var isStreaming = false
    /// Tracks the screen-space position of the streaming status text /
    /// animated logo. Lives on a SEPARATE ObservableObject (not a @Published
    /// here) so writes don't trigger AppViewModel's objectWillChange and
    /// cascade re-renders through the chat tree. Only views that explicitly
    /// observe `cursorTracker` (currently just NeuralNetBackgroundView) re-
    /// render when the origin updates.
    let cursorTracker = CursorOriginTracker()
    @Published var isCompacting = false
    @Published var compactingDone = false
    @Published var compactingSummary = ""
    @Published var isRecovering = false
    @Published var recoveryStage = ""
    @Published var recoveryDetail = ""
    @Published var showSettings = false
    @Published var showDebug = false
    @Published var isConnected = false

    // MARK: - Graph Data (Inspector Panel)

    @Published var graphData: GraphData?
    @Published var selectedGraphNodeId: String?

    // MARK: - Skills Activity (Inspector Panel)

    @Published var activeSkillInfo: SkillActivityInfo?
    private var skillsPollingTask: Task<Void, Never>?

    // MARK: - Thinking Mode (Inspector quick-toggle)

    @Published var thinkingMode: Bool = true
    @Published var isThinkingToggling: Bool = false
    /// Persisted via UserDefaults so the inspector state survives
    /// app restart — if the panel was open when you quit, it reopens.
    @Published var showInspector: Bool {
        didSet { UserDefaults.standard.set(showInspector, forKey: "showInspector") }
    }
    private var graphPollingTask: Task<Void, Never>?

    /// Tracks how many times the current pending message has been retried
    private var replayAttempts = 0
    private let maxReplayAttempts = 2

    /// Set to true when a replay request arrives while a stream is still active.
    /// The in-flight stream's teardown checks this and triggers the replay after
    /// it has cleaned up, so no pending message is silently lost.
    private var deferredReplayPending = false

    /// Handle to the current streaming task — canceled on conversation switch
    var streamingTask: Task<Void, Never>?
    /// ID of the conversation currently being streamed to
    private var streamingConversationId: UUID?

    private let persistence = PersistenceManager.shared
    private var apiService: APIService

    /// Reference to AgentManager for lifecycle control + pending message replay
    var agentManager: AgentManager?
    private var replayObserver: Any?

    /// Throttle persistence writes during streaming to avoid disk I/O on every token
    private var lastSaveTime: Date = .distantPast
    private let saveInterval: TimeInterval = 0.5  // save at most every 500ms during streaming

    var filteredConversations: [Conversation] {
        if searchText.isEmpty {
            return conversations
        }
        return conversations.filter { conversation in
            conversation.title.localizedCaseInsensitiveContains(searchText) ||
            conversation.messages.contains { $0.content.localizedCaseInsensitiveContains(searchText) }
        }
    }
    
    var pinnedConversations: [Conversation] {
        filteredConversations.filter { $0.isPinned }
    }
    
    var unpinnedConversations: [Conversation] {
        filteredConversations.filter { !$0.isPinned }
    }
    
    init() {
        // Restore inspector state from previous session
        self.showInspector = UserDefaults.standard.bool(forKey: "showInspector")

        self.apiService = APIService(
            baseURL: persistence.apiEndpoint,
            model: persistence.modelName,
            temperature: persistence.temperature,
            maxTokens: persistence.maxTokens
        )
        loadConversations()

        // Listen for agent restart + pending message replay
        replayObserver = NotificationCenter.default.addObserver(
            forName: .agentReadyForReplay, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                await self?.replayPendingMessage()
            }
        }
    }

    /// Connect this ViewModel to the AgentManager. Called once from ContentView.
    func connectAgentManager(_ manager: AgentManager) {
        self.agentManager = manager
        // Sync connection + MLX recovery state from AgentManager → ViewModel
        Task {
            while !Task.isCancelled {
                let connected = manager.isConnected
                if self.isConnected != connected {
                    self.isConnected = connected
                }

                // Sync MLX recovery state to the recovery pill UI
                if case .recovering(let detail) = manager.mlxStatus {
                    if !self.isRecovering {
                        self.isRecovering = true
                        self.recoveryStage = "restarting"
                        self.recoveryDetail = detail
                    } else if self.recoveryDetail != detail {
                        self.recoveryDetail = detail
                    }
                } else if self.isRecovering && !self.isStreaming {
                    if manager.mlxStatus == .running || manager.mlxStatus == .busy {
                        self.recoveryStage = "recovered"
                        self.recoveryDetail = "MLX server is back online"
                        try? await Task.sleep(for: .seconds(2))
                        self.isRecovering = false
                        self.recoveryStage = ""
                        self.recoveryDetail = ""
                    }
                }

                try? await Task.sleep(for: .seconds(2))
            }
        }
        // Fetch initial thinking mode from agent
        Task { await fetchThinkingMode() }
        // NOTE: pre-warm removed. ensureReady() launches local backends
        // (llama-server, MLX) which waste GPU memory when using external
        // endpoints (LM Studio, custom). Agent starts on first message.
    }

    // MARK: - Graph Polling

    func startGraphPolling(for conversationId: UUID) {
        graphPollingTask?.cancel()
        graphPollingTask = Task {
            while !Task.isCancelled {
                await fetchGraphData(conversationId: conversationId)
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func stopGraphPolling() {
        graphPollingTask?.cancel()
        graphPollingTask = nil
    }

    private func fetchGraphData(conversationId: UUID) async {
        guard isConnected else { return }
        let base = persistence.apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(base)/v1/graph/\(conversationId.uuidString)") else { return }

        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else { return }
            let decoded = try JSONDecoder().decode(GraphData.self, from: data)

            // Read the locally-persisted graph from disk — this is always
            // up-to-date and avoids cross-thread issues with @Published.
            let localDisk = persistence.loadGraphData(for: conversationId)
            let localHasNodes = !(localDisk?.nodes.isEmpty ?? true)
            let remoteIsEmpty = decoded.nodes.isEmpty

            // Never let an empty agent response overwrite locally-known
            // graph data.  The agent's in-memory store resets on restart,
            // but Clyde persists graphs to disk — trust the local copy
            // when the agent has nothing to offer.
            if localHasNodes && remoteIsEmpty {
                // Agent lost its state (restart?).  Re-seed it.
                print("[Graph] Agent returned empty but disk has \(localDisk!.nodes.count) nodes — re-seeding agent")
                await seedAgentGraph(conversationId: conversationId, localData: localDisk!)
                return
            }

            if self.graphData != decoded {
                DispatchQueue.main.async {
                    self.graphData = decoded
                }
                // Persist to disk so graph survives app restart
                if !decoded.nodes.isEmpty {
                    persistence.saveGraphData(decoded, for: conversationId)
                }
            }
        } catch { }
    }

    /// Push the locally-persisted graph back to the agent so its in-memory
    /// store is re-populated after a restart.
    private func seedAgentGraph(conversationId: UUID, localData: GraphData) async {
        guard !localData.nodes.isEmpty else { return }
        let base = persistence.apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(base)/v1/graph/\(conversationId.uuidString)") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 5
        request.httpBody = try? JSONEncoder().encode(localData)
        do {
            let (_, resp) = try await URLSession.shared.data(for: request)
            let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
            print("[Graph] Seeded agent with \(localData.nodes.count) nodes — HTTP \(status)")
        } catch {
            print("[Graph] seedAgentGraph failed: \(error)")
        }
    }

    // MARK: - Skills Polling

    func startSkillsPolling(for conversationId: UUID) {
        skillsPollingTask?.cancel()
        skillsPollingTask = Task {
            while !Task.isCancelled {
                await fetchSkillActivity(conversationId: conversationId)
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stopSkillsPolling() {
        skillsPollingTask?.cancel()
        skillsPollingTask = nil
    }

    private func fetchSkillActivity(conversationId: UUID) async {
        guard isConnected else { return }
        let base = persistence.apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(base)/v1/skills/active/\(conversationId.uuidString)") else { return }

        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else { return }
            let decoded = try JSONDecoder().decode(SkillActivityInfo.self, from: data)
            if self.activeSkillInfo != decoded {
                self.activeSkillInfo = decoded
                // Persist to conversation so it survives chat switches
                if let conv = self.selectedConversation,
                   let idx = self.conversations.firstIndex(where: { $0.id == conv.id }) {
                    self.conversations[idx].lastActiveSkill = decoded.activeSkill
                    self.conversations[idx].lastActiveSkillInfo = decoded
                    self.persistence.saveConversation(self.conversations[idx])
                }
            }
        } catch {
            // Silently fail — skills info is non-critical
        }
    }

    // MARK: - Thinking Mode Toggle

    /// Fetch current thinking_mode from the agent's /v1/settings endpoint.
    func fetchThinkingMode() async {
        guard isConnected else { return }
        let base = persistence.apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(base)/v1/settings") else { return }
        do {
            var req = URLRequest(url: url)
            req.timeoutInterval = 4
            let (data, response) = try await URLSession.shared.data(for: req)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else { return }
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let env = json["env"] as? [String: Any],
               let mode = env["thinking_mode"] as? String {
                let isOn = (mode.lowercased() != "off")
                await MainActor.run {
                    self.thinkingMode = isOn
                    // Persist to conversation
                    if let conv = self.selectedConversation,
                       let idx = self.conversations.firstIndex(where: { $0.id == conv.id }),
                       self.conversations[idx].thinkingEnabled != isOn {
                        self.conversations[idx].thinkingEnabled = isOn
                        self.persistence.saveConversation(self.conversations[idx])
                    }
                }
            }
        } catch { }
    }

    /// Toggle thinking_mode on the agent via PUT /v1/settings.
    func toggleThinkingMode() async {
        await MainActor.run { isThinkingToggling = true }
        let newMode = !thinkingMode
        let base = persistence.apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(base)/v1/settings") else {
            await MainActor.run { isThinkingToggling = false }
            return
        }
        var req = URLRequest(url: url)
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 5
        let payload: [String: Any] = ["env": ["thinking_mode": newMode ? "on" : "off"]]
        do {
            req.httpBody = try JSONSerialization.data(withJSONObject: payload)
            let (_, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                await MainActor.run {
                    self.thinkingMode = newMode
                    // Persist to conversation so it survives chat switches
                    if let conv = self.selectedConversation,
                       let idx = self.conversations.firstIndex(where: { $0.id == conv.id }) {
                        self.conversations[idx].thinkingEnabled = newMode
                        self.persistence.saveConversation(self.conversations[idx])
                    }
                }
            }
        } catch { }
        await MainActor.run { isThinkingToggling = false }
    }

    // MARK: - Conversation Management

    func loadConversations() {
        conversations = persistence.loadConversations()

        // ── Stale isStreaming cleanup ──
        // If Clyde crashed or was force-quit while a stream was in flight,
        // the conversation JSON on disk will have a message with
        // isStreaming = true. That message renders a spinning cursor
        // forever with no actual stream behind it. Clean it up on launch.
        var fixed = false
        for i in conversations.indices {
            for j in conversations[i].messages.indices {
                if conversations[i].messages[j].isStreaming {
                    conversations[i].messages[j].isStreaming = false
                    fixed = true
                }
            }
        }
        if fixed {
            print("[AppViewModel] Cleaned up stale isStreaming flags from previous session")
            // Re-save the fixed conversations
            for conv in conversations where conv.messages.contains(where: { !$0.isStreaming }) {
                persistence.saveConversation(conv)
            }
        }
    }
    
    /// Switch to a different conversation WITHOUT canceling any in-flight
    /// stream. The stream continues in the background — the user can
    /// browse other conversations and come back to see the results.
    ///
    /// The stream is only canceled when the user sends a MESSAGE in a
    /// different conversation (handled in sendMessage()), or when the
    /// user explicitly hits the Stop button (handled in stopStreaming()).
    ///
    /// The sidebar shows a pulsing pine-green indicator on conversations
    /// that have background work in progress, via the
    /// `backgroundStreamingConversationId` computed property.
    func switchToConversation(_ conversation: Conversation?) {
        // ── Save outgoing conversation's inspector state ──
        if let outgoing = selectedConversation,
           let idx = conversations.firstIndex(where: { $0.id == outgoing.id }) {
            conversations[idx].thinkingEnabled = thinkingMode
            if let skill = activeSkillInfo {
                conversations[idx].lastActiveSkill = skill.activeSkill
                conversations[idx].lastActiveSkillInfo = skill
            }
            persistence.saveConversation(conversations[idx])
        }

        selectedConversation = conversation
        // Load persisted graph data immediately, then start polling for updates
        if let conv = conversation {
            selectedGraphNodeId = nil
            // Load from disk first so the graph appears instantly
            graphData = persistence.loadGraphData(for: conv.id)

            // Restore per-conversation inspector state (default ON for new chats)
            if let savedThinking = conv.thinkingEnabled {
                thinkingMode = savedThinking
            } else {
                thinkingMode = true
            }
            activeSkillInfo = conv.lastActiveSkillInfo

            // Always start graph polling — even when the inspector is hidden
            // the agent may be accumulating new nodes from tool calls.
            startGraphPolling(for: conv.id)
            if showInspector {
                startSkillsPolling(for: conv.id)
            }
        } else {
            graphData = nil
            activeSkillInfo = nil
            stopGraphPolling()
            stopSkillsPolling()
        }
    }

    /// The conversation ID that has a background stream running (if any).
    /// Non-nil when isStreaming is true and the user has navigated away
    /// from the streaming conversation. Used by SidebarView to show the
    /// "work in progress" indicator.
    var backgroundStreamingConversationId: UUID? {
        guard isStreaming, let streamId = streamingConversationId else { return nil }
        if streamId != selectedConversation?.id { return streamId }
        return nil
    }

    func createNewConversation() {
        let conversation = Conversation()
        conversations.insert(conversation, at: 0)
        switchToConversation(conversation)
        persistence.saveConversation(conversation)
    }
    
    func deleteConversation(_ conversation: Conversation) {
        // If the deleted conversation is actively streaming, stop it
        if conversation.id == selectedConversation?.id && isStreaming {
            stopStreaming()
            apiService.cancelStream()
        }
        persistence.deleteConversation(conversation)
        persistence.deleteGraphData(for: conversation.id)
        conversations.removeAll { $0.id == conversation.id }
        if selectedConversation?.id == conversation.id {
            selectedConversation = conversations.first
        }
    }
    
    func togglePin(_ conversation: Conversation) {
        if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
            conversations[index].isPinned.toggle()
            conversations[index].updatedAt = Date()
            persistence.saveConversation(conversations[index])
            
            // Update selected conversation if it's the same
            if selectedConversation?.id == conversation.id {
                selectedConversation = conversations[index]
            }
        }
    }
    
    func updateConversationTitle(_ conversation: Conversation, title: String) {
        if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
            conversations[index].title = title
            conversations[index].updatedAt = Date()
            persistence.saveConversation(conversations[index])
            
            if selectedConversation?.id == conversation.id {
                selectedConversation = conversations[index]
            }
        }
    }
    
    // MARK: - Retry / Edit

    /// Retry the assistant's response: delete the last assistant message
    /// and re-send the last user message. If called on a user message,
    /// deletes everything from that message onward and re-sends it.
    func retryMessage(id: UUID) async {
        guard var conversation = selectedConversation else { return }

        // Find the message index
        guard let idx = conversation.messages.firstIndex(where: { $0.id == id }) else { return }

        let targetMessage = conversation.messages[idx]

        if targetMessage.role == .assistant {
            // Remove this assistant message, re-send the user message before it
            conversation.messages.remove(at: idx)
            // Find the user message just before
            if let userIdx = conversation.messages[..<idx].lastIndex(where: { $0.role == .user }) {
                let userContent = conversation.messages[userIdx].content
                let userAttachments = conversation.messages[userIdx].attachments
                // Remove everything from userIdx onward
                conversation.messages = Array(conversation.messages[..<userIdx])
                updateConversation(conversation, forceSave: true)
                await sendMessage(content: userContent, attachments: userAttachments)
            }
        } else if targetMessage.role == .user {
            // Remove this message and everything after, then re-send
            let content = targetMessage.content
            let attachments = targetMessage.attachments
            conversation.messages = Array(conversation.messages[..<idx])
            updateConversation(conversation, forceSave: true)
            await sendMessage(content: content, attachments: attachments)
        }
    }

    /// Edit a user message: update content, delete everything after, re-send.
    func editMessage(id: UUID, newContent: String) async {
        guard var conversation = selectedConversation else { return }
        guard let idx = conversation.messages.firstIndex(where: { $0.id == id }) else { return }
        guard conversation.messages[idx].role == .user else { return }

        let attachments = conversation.messages[idx].attachments
        // Remove this message and everything after
        conversation.messages = Array(conversation.messages[..<idx])
        updateConversation(conversation, forceSave: true)
        await sendMessage(content: newContent, attachments: attachments)
    }

    // MARK: - Message Management

    func sendMessage(
        content: String,
        attachments: [Attachment] = [],
        reusingMessageId: UUID? = nil,
        conversationId: UUID? = nil
    ) async {
        // Resolve conversation: explicit id (used by the queued-message
        // dequeue path) wins over current UI selection so a queued
        // message still processes even if the user switched chats.
        let resolvedConv: Conversation? = {
            if let cid = conversationId {
                return conversations.first(where: { $0.id == cid })
            }
            return selectedConversation
        }()
        guard var conversation = resolvedConv else {
            return
        }
        let targetConversationId = conversation.id  // Capture at call time for safety

        // Cancel any previous stream before starting a new one.
        // This is the ONLY place that cancels a background stream —
        // switchToConversation() deliberately does NOT cancel, so
        // the user can browse other chats while a stream is running.
        // The stream is only killed when the user starts TALKING in
        // a different conversation (i.e., right here).
        if let existing = streamingTask,
           let bgConvId = streamingConversationId,
           bgConvId != targetConversationId {
            print("[AppViewModel] Canceling background stream for \(bgConvId.uuidString.prefix(8)) — user sent message in different conversation")
            existing.cancel()
            apiService.cancelStream()
            // Clean up the background conversation's streaming message
            if var bgConv = conversations.first(where: { $0.id == bgConvId }),
               let lastIdx = bgConv.messages.lastIndex(where: { $0.isStreaming }) {
                bgConv.messages[lastIdx].isStreaming = false
                bgConv.messages[lastIdx].content += "\n\n⚠️ Stream stopped — you started a new conversation."
                updateConversation(bgConv, forceSave: true)
            }
            isStreaming = false
            streamingTask = nil
            streamingConversationId = nil
            agentManager?.endInference()
        }
        // NOTE: Same-conversation cancellation (user sent a follow-up
        // while previous message was still streaming) is now handled by
        // ChatView.sendMessage() BEFORE creating the new Task. Doing it
        // here would cancel our OWN Task because ChatView stores us in
        // streamingTask before we start executing.
        streamingConversationId = targetConversationId

        // Add user message FIRST — show it in the UI immediately, before
        // ensureReady() blocks waiting for the agent to boot. This is the
        // iOS-Messages "your bubble appears the instant you tap send"
        // behavior. Animation is driven by withAnimation here + the
        // .transition modifier on MessageBubbleView in ChatView.
        //
        // Two paths:
        //   - reusingMessageId == nil → fresh send, append a brand-new user
        //     message bubble (slides up from below, blue).
        //   - reusingMessageId != nil → dequeue path; the queued bubble is
        //     already on screen (orange). Flip its isQueued flag so the
        //     tint resolves to the regular user color.
        if let reuseId = reusingMessageId,
           let idx = conversation.messages.firstIndex(where: { $0.id == reuseId }) {
            withAnimation(.easeInOut(duration: 0.3)) {
                conversation.messages[idx].isQueued = false
                conversation.updatedAt = Date()
                updateConversation(conversation, forceSave: true)
            }
        } else {
            let userMessage = ChatMessage(
                role: .user,
                content: content,
                attachments: attachments
            )

            withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) {
                conversation.messages.append(userMessage)
                conversation.updatedAt = Date()

                // Auto-generate title from first message
                if conversation.messages.count == 1 && persistence.autoTitle {
                    conversation.title = persistence.generateTitle(from: content)
                }
                // Refine title after 3 messages if it still looks like a raw first-message title
                else if conversation.messages.count == 3 && persistence.autoTitle {
                    let refined = persistence.generateRefinedTitle(from: conversation.messages)
                    if !refined.isEmpty && refined != "New Conversation" {
                        conversation.title = refined
                    }
                }

                updateConversation(conversation, forceSave: true)
            }
        }

        // Track as pending in case agent crashes mid-response
        agentManager?.setPendingMessage(
            content: content, attachments: attachments,
            conversationId: conversation.id
        )

        // Ensure the full stack is ready (handles idle, hibernated, error, etc.)
        if let manager = agentManager {
            manager.recordActivity()
            let ready = await manager.ensureReady()
            if !ready {
                print("[AppViewModel] Stack not ready, proceeding anyway for retry loop")
            }
        }

        // Create assistant message placeholder
        let assistantMessage = ChatMessage(
            role: .assistant,
            content: "",
            isStreaming: true
        )

        conversation.messages.append(assistantMessage)
        updateConversation(conversation, forceSave: false)

        // NOTE: isStreaming is set to true here so the UI shows the
        // assistant placeholder immediately (with streaming cursor).
        // If the connection fails and never produces tokens, the
        // teardown at the end of this function sets isStreaming = false
        // and the cursor disappears. The cursor is a "waiting for
        // response" indicator, not a "response is actively arriving"
        // indicator — it's intentional that it shows during the
        // retry loop while waiting for the agent to come up.
        isStreaming = true
        agentManager?.beginInference()

        // Stream response — with automatic retry on connection failure.
        // If the agent isn't ready yet, we keep the streaming cursor visible
        // and wait for it to come up instead of showing an error.
        //
        // Backoff is exponential with jitter, capped at `maxBackoff`. Total budget
        // is `maxTotalWait` seconds — once exceeded, we give up. This is friendlier
        // than a fixed 20×3s loop when the agent is fast OR when it's permanently down.
        let maxTotalWait: TimeInterval = 60
        let baseBackoff: TimeInterval = 0.5
        let maxBackoff: TimeInterval = 8.0
        let retryStart = Date()
        var attempt = 0
        var streamSucceeded = false

        retryLoop: while true {
            if attempt > 0 {
                let elapsed = Date().timeIntervalSince(retryStart)
                if elapsed >= maxTotalWait {
                    print("[AppViewModel] Retry budget exhausted after \(Int(elapsed))s — giving up")
                    break retryLoop
                }
                // Exponential backoff: 0.5, 1, 2, 4, 8, 8, ... with ±25% jitter
                let raw = min(baseBackoff * pow(2.0, Double(attempt - 1)), maxBackoff)
                let jitter = Double.random(in: 0.75...1.25)
                let backoff = min(raw * jitter, maxTotalWait - elapsed)
                print("[AppViewModel] Connection failed, backing off \(String(format: "%.1f", backoff))s (attempt \(attempt))...")
                let ready = await waitForAgentReady(timeout: max(1, Int(backoff.rounded(.up))))
                if !ready && Date().timeIntervalSince(retryStart) >= maxTotalWait {
                    break retryLoop
                }
            }
            attempt += 1

            do {
                // Don't send the empty streaming placeholder to the API.
                // Also fold any trailing run of user messages (multiple
                // queued bubbles that just dequeued) into one combined
                // user message so the model sees them as a single turn.
                let messagesToSend: [ChatMessage]
                if let conv = selectedConversation {
                    let filtered = conv.messages.filter { !$0.isStreaming }
                    messagesToSend = foldTrailingUserMessages(filtered)
                } else {
                    break retryLoop
                }
                let stream = apiService.streamChat(messages: messagesToSend, conversationId: conversation.id)

                // ── Streaming UI throttle state ──
                // Mutating @Published `conversations` on every delta drives
                // the SwiftUI view-graph re-evaluation faster than AppKit can
                // flush constraint passes. The crash signature is:
                //
                //   "The window has been marked as needing another Update
                //    Constraints in Window pass, but it has already had more
                //    Update Constraints in Window passes than there are views
                //    in the window."
                //
                // Reading the stack trace, the loop is INSIDE NSHostingView's
                // layout call: layout → flushTransactions → propagate_dirty →
                // graphDidChange → setNeedsUpdate → setNeedsUpdateConstraints
                // on the view tree we're currently laying out. This happens
                // because new @Published mutations land DURING a layout pass
                // when the view tree is large enough that one layout pass
                // takes longer than the throttle window.
                //
                // The fix is two-pronged:
                //   1. Throttle to 200ms (well above the worst-case layout
                //      pass time even for ~300-view trees). At 5 Hz the user
                //      still sees smooth-looking streaming text but layout
                //      always finishes between mutations.
                //   2. Dispatch the @Published mutation via DispatchQueue.main
                //      .async, which schedules it on the NEXT run-loop tick.
                //      This guarantees no mutation lands mid-layout — the
                //      current run loop iteration (including any in-progress
                //      layout) finishes first, then the mutation runs cleanly
                //      on a fresh tick.
                //
                // Structural events (tool calls, questions, plan state, stream
                // done) still bypass the throttle and flush immediately so
                // state transitions appear instantly.
                var pendingConvUpdate: Conversation? = nil
                var lastUIFlushTime = Date.distantPast
                let uiFlushInterval: TimeInterval = 0.200  // 5 Hz
                // Tracks whether we've already told AgentManager the backend
                // is healthy for this stream. Must only fire ONCE per stream:
                // calling it on every delta mutates @Published properties on
                // AgentManager mid-layout-pass and crashes via the same
                // "modifying state during view update" path documented above.
                var didNotifyHealthy = false

                for try await delta in stream {
                    // Check if this stream was canceled (user switched conversations)
                    if Task.isCancelled {
                        print("[AppViewModel] Stream canceled (conversation switched)")
                        break retryLoop
                    }

                    // Look up the conversation by captured ID, but prefer the
                    // accumulated local copy if there's a pending update — that
                    // way mutations from previous (un-flushed) deltas are not
                    // lost when we re-fetch fresh state.
                    let baseConv: Conversation? = pendingConvUpdate
                        ?? conversations.first(where: { $0.id == targetConversationId })
                    guard var currentConversation = baseConv,
                          let lastIndex = currentConversation.messages.lastIndex(where: { $0.id == assistantMessage.id }) else {
                        break retryLoop
                    }

                    // If we got any delta, the connection succeeded
                    streamSucceeded = true
                    // Stream is delivering tokens → backend is unambiguously
                    // healthy. Tell AgentManager to clear any stale recovery
                    // banner / restart counters from a previous false-positive
                    // health-check failure (e.g. "Restarting MLX (attempt 1/5)"
                    // hanging on screen while a real stream is mid-flight).
                    //
                    // Two safety constraints:
                    //   1. Fire ONCE per stream (not per delta) — mutating
                    //      @Published on AgentManager hundreds of times mid-
                    //      stream re-triggers the layout-pass crash.
                    //   2. Dispatch via DispatchQueue.main.async so the
                    //      mutation lands on the NEXT run loop tick, after
                    //      any in-progress layout pass finishes — same reason
                    //      the conversations mutation below is dispatched.
                    if !didNotifyHealthy {
                        didNotifyHealthy = true
                        DispatchQueue.main.async { [weak self] in
                            self?.agentManager?.notifyBackendHealthy()
                        }
                    }

                    switch delta.type {
                    case .content(let text):
                        currentConversation.messages[lastIndex].content += text
                        // Append to last text block or create a new one
                        if case .text(let existing) = currentConversation.messages[lastIndex].contentBlocks.last?.content {
                            let blockIndex = currentConversation.messages[lastIndex].contentBlocks.count - 1
                            currentConversation.messages[lastIndex].contentBlocks[blockIndex].content = .text(existing + text)
                        } else {
                            currentConversation.messages[lastIndex].contentBlocks.append(
                                MessageBlock(content: .text(text))
                            )
                        }

                        // Permission settings open request is now handled via
                        // Clyde Settings → Permissions tab (no runtime popup)

                    case .toolStart(let name, let argsPreview):
                        let toolCall = ToolCall(toolName: name, arguments: argsPreview, status: .running)
                        currentConversation.messages[lastIndex].toolCalls.append(toolCall)
                        // Add a toolCall block referencing this tool call's id
                        currentConversation.messages[lastIndex].contentBlocks.append(
                            MessageBlock(content: .toolCall(toolCall.id))
                        )

                    case .toolDone(let name, let isError, let summary, let output):
                        // Find the matching running tool call and update its status
                        if let toolIndex = currentConversation.messages[lastIndex].toolCalls.lastIndex(where: {
                            $0.toolName == name && $0.status == .running
                        }) {
                            currentConversation.messages[lastIndex].toolCalls[toolIndex].status = isError ? .error : .done
                            if !summary.isEmpty {
                                currentConversation.messages[lastIndex].toolCalls[toolIndex].result = summary
                            }
                            if let output = output, !output.isEmpty {
                                currentConversation.messages[lastIndex].toolCalls[toolIndex].output = output
                            }
                        }

                    case .thinking(let thought):
                        currentConversation.messages[lastIndex].thinkingContent = thought

                    case .thinkingStart:
                        // Show thinking indicator even before <think> content arrives
                        if currentConversation.messages[lastIndex].thinkingContent == nil {
                            currentConversation.messages[lastIndex].thinkingContent = ""
                        }

                    case .separator:
                        // Agent sends --- before the final answer; strip trailing whitespace
                        // from the flat content string
                        let trimmed = currentConversation.messages[lastIndex].content
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        currentConversation.messages[lastIndex].content = trimmed
                        // Also trim the last text block if present
                        if case .text(let existing) = currentConversation.messages[lastIndex].contentBlocks.last?.content {
                            let blockIndex = currentConversation.messages[lastIndex].contentBlocks.count - 1
                            let trimmedBlock = existing.trimmingCharacters(in: .whitespacesAndNewlines)
                            if trimmedBlock.isEmpty {
                                currentConversation.messages[lastIndex].contentBlocks.removeLast()
                            } else {
                                currentConversation.messages[lastIndex].contentBlocks[blockIndex].content = .text(trimmedBlock)
                            }
                        }

                case .question(let questionData):
                    // Agent is asking the user a multiple-choice question mid-turn.
                    // Store it on the message so the view renders the question UI.
                    currentConversation.messages[lastIndex].pendingQuestions.append(questionData)
                    // Add a content block so the question card renders inline
                    // in sequence with text and tool calls, not at the bottom.
                    currentConversation.messages[lastIndex].contentBlocks.append(
                        MessageBlock(content: .question(questionData.id))
                    )

                case .permissionRequest(let permData):
                    // Agent needs folder access — show permission card inline.
                    currentConversation.messages[lastIndex].pendingPermissions.append(permData)
                    currentConversation.messages[lastIndex].contentBlocks.append(
                        MessageBlock(content: .permissionRequest(permData.id))
                    )

                case .planState(_, let plan):
                    // Plan snapshot from task_plan/update/complete/expand.
                    // Update the existing plan in place if we already have it
                    // (so the card animates rather than replacing), or insert
                    // a new plan + add an inline content block.
                    if let plan = plan {
                        let msgIdx = lastIndex
                        if let existingIdx = currentConversation.messages[msgIdx].plans.firstIndex(where: { $0.id == plan.id }) {
                            currentConversation.messages[msgIdx].plans[existingIdx] = plan
                        } else {
                            currentConversation.messages[msgIdx].plans.append(plan)
                            // First time we see this plan in this message — add an inline block
                            let alreadyBlocked = currentConversation.messages[msgIdx].contentBlocks.contains { block in
                                if case .plan(let pid) = block.content { return pid == plan.id }
                                return false
                            }
                            if !alreadyBlocked {
                                currentConversation.messages[msgIdx].contentBlocks.append(
                                    MessageBlock(content: .plan(plan.id))
                                )
                            }
                        }
                    }

                case .compacting(let summary):
                    isCompacting = true
                    compactingDone = false
                    compactingSummary = summary

                case .compactDone(let summary):
                    compactingSummary = summary
                    compactingDone = true
                    // Brief delay so the user sees the "done" state before it vanishes
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(1.5))
                        isCompacting = false
                        compactingDone = false
                        compactingSummary = ""
                    }

                case .recovering(let stage, let detail):
                    isRecovering = true
                    recoveryStage = stage
                    recoveryDetail = detail

                case .recoveryDone(let detail):
                    recoveryDetail = detail
                    recoveryStage = "recovered"
                    // Brief delay so the user sees "recovered" before it vanishes
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(2.0))
                        isRecovering = false
                        recoveryStage = ""
                        recoveryDetail = ""
                    }

                case .metrics(let m):
                    // Live tok/s + phase — surface on AgentManager so the
                    // Performance inspector tile can render it without
                    // coupling chat data to the status banner.
                    agentManager?.updateLiveMetrics(m)

                case .contextSnapshot(let cp):
                    agentManager?.updateContextPressure(cp)

                case .done:
                    // Clear live metrics when stream completes
                    agentManager?.updateLiveMetrics(nil)
                    currentConversation.messages[lastIndex].isStreaming = false
                    // Mark any still-running tool calls as done
                    for i in currentConversation.messages[lastIndex].toolCalls.indices {
                        if currentConversation.messages[lastIndex].toolCalls[i].status == .running {
                            currentConversation.messages[lastIndex].toolCalls[i].status = .done
                        }
                    }
                    // Clean up any empty trailing text blocks
                    while case .text(let t) = currentConversation.messages[lastIndex].contentBlocks.last?.content,
                          t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        currentConversation.messages[lastIndex].contentBlocks.removeLast()
                    }
                }

                currentConversation.updatedAt = Date()
                // Force save on stream done, otherwise throttle
                let shouldForceSave = {
                    if case .done = delta.type { return true }
                    return false
                }()

                // Decide whether to flush this delta to the published view
                // model immediately or accumulate it. Structural events that
                // change the SHAPE of the message (tool calls starting/ending,
                // questions, plan updates, stream completion) always flush
                // because the user needs to see state transitions instantly.
                // Continuous text/thinking deltas are coalesced at 30Hz to
                // prevent the constraint-pass blowup.
                let isStructuralChange: Bool = {
                    switch delta.type {
                    case .toolStart, .toolDone, .question, .permissionRequest,
                         .planState, .compacting, .compactDone, .recovering,
                         .recoveryDone, .separator, .metrics, .contextSnapshot, .done:
                        return true
                    default:
                        return false
                    }
                }()
                let nowTime = Date()
                let shouldFlushNow = isStructuralChange
                    || (nowTime.timeIntervalSince(lastUIFlushTime) >= uiFlushInterval)

                if shouldFlushNow {
                    // Defer the actual @Published mutation to the NEXT
                    // run-loop tick so it can NEVER land mid-layout. This is
                    // the second prong of the layout-loop fix — even with
                    // the 200ms throttle, a mutation arriving inside an
                    // in-progress layout pass causes SwiftUI's transaction
                    // flush to schedule another layout from inside the
                    // current one. DispatchQueue.main.async schedules on a
                    // fresh main-thread tick AFTER the current run loop
                    // iteration completes (and runs in all run-loop modes,
                    // including tracking mode while the user is scrolling).
                    let snapshot = currentConversation
                    let force = shouldForceSave
                    DispatchQueue.main.async { [weak self] in
                        self?.updateConversation(snapshot, forceSave: force)
                    }
                    pendingConvUpdate = nil
                    lastUIFlushTime = nowTime
                } else {
                    // Hold the mutation in the local accumulator. The next
                    // delta will start from this version instead of re-reading
                    // stale state from `conversations`.
                    pendingConvUpdate = currentConversation
                }
            }

                // Always flush any pending mutations once the stream loop exits
                // so the final tail of text deltas isn't lost.
                if let pending = pendingConvUpdate {
                    updateConversation(pending, forceSave: true)
                    pendingConvUpdate = nil
                }

                // Stream completed normally — break out of retry loop
                streamSucceeded = true
                break retryLoop

            } catch let error as URLError where error.code == .cannotConnectToHost
                                             || error.code == .networkConnectionLost
                                             || error.code == .timedOut
                                             || error.code == .cannotFindHost {
                // Connection error — agent isn't ready yet. Keep streaming cursor
                // visible and retry after waiting for the agent to come up.
                print("[AppViewModel] Connection error (retryable): \(error.localizedDescription)")
                continue  // → top of retryLoop, which waits for agent
            } catch {
                // Non-retryable error (decoding, HTTP 500, cancellation, etc.)
                print("[AppViewModel] Streaming error (non-retryable): \(error)")
                if var currentConversation = conversations.first(where: { $0.id == targetConversationId }),
                   let lastIndex = currentConversation.messages.lastIndex(where: { $0.id == assistantMessage.id }) {
                    if !Task.isCancelled {
                        currentConversation.messages[lastIndex].content += "\n\nError: \(error.localizedDescription)"
                    }
                    currentConversation.messages[lastIndex].isStreaming = false
                    updateConversation(currentConversation, forceSave: true)
                }
                break retryLoop
            }
        } // end retryLoop

        // If we exhausted retries without success, show an error.
        // IMPORTANT: preserve any content already accumulated during partial
        // streaming — APPEND the error, never REPLACE the existing content.
        if !streamSucceeded && !Task.isCancelled {
            if var currentConversation = conversations.first(where: { $0.id == targetConversationId }),
               let lastIndex = currentConversation.messages.lastIndex(where: { $0.id == assistantMessage.id }),
               currentConversation.messages[lastIndex].isStreaming {
                let errorMsg = "Could not connect to the server. Clyde is restarting the backend..."
                if currentConversation.messages[lastIndex].content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    && currentConversation.messages[lastIndex].toolCalls.isEmpty {
                    // No content accumulated — safe to replace
                    currentConversation.messages[lastIndex].content = errorMsg
                } else {
                    // Content already accumulated — append error, don't wipe
                    currentConversation.messages[lastIndex].content += "\n\n⚠️ " + errorMsg
                    currentConversation.messages[lastIndex].contentBlocks.append(
                        MessageBlock(content: .text("\n\n⚠️ " + errorMsg))
                    )
                }
                currentConversation.messages[lastIndex].isStreaming = false
                updateConversation(currentConversation, forceSave: true)
            }

            // PROACTIVE RECOVERY: don't wait for the next 5s health check
            // cycle to notice the agent is dead. The stream just dropped,
            // which is unambiguous evidence the backend went away. Trigger
            // AgentManager's recovery path immediately so the user sees
            // "Restarting agent..." in the banner and the stack relaunches
            // in the background. Without this signal, the user sees
            // "Could not connect" in the chat but the banner stays empty
            // (because AgentManager still thinks everything is fine).
            if let manager = agentManager {
                print("[AppViewModel] Stream failed — signaling AgentManager to recover")
                Task {
                    await manager.requestRecovery(reason: "streaming connection lost")
                }
            }
        }

        // After streaming completes, scan for file paths in BOTH the message
        // content AND tool call arguments/results (the agent often puts the full
        // path only in the tool call, not in the prose).
        if var finalConversation = conversations.first(where: { $0.id == targetConversationId }),
           let lastIdx = finalConversation.messages.lastIndex(where: { $0.id == assistantMessage.id }) {
            let msg = finalConversation.messages[lastIdx]
            var searchText = msg.content
            for tc in msg.toolCalls {
                searchText += "\n" + tc.arguments
                if let result = tc.result {
                    searchText += "\n" + result
                }
            }
            let detectedFiles = detectFilePaths(in: searchText,
                                                 conversationId: finalConversation.id)
            if !detectedFiles.isEmpty {
                finalConversation.messages[lastIdx].files = detectedFiles
                updateConversation(finalConversation, forceSave: true)
            }
        }

        // Only update streaming state if this is still the active stream
        // (not if it was canceled by a conversation switch)
        if streamingConversationId == targetConversationId {
            isStreaming = false
            cursorTracker.origin = nil
            streamingTask = nil
            streamingConversationId = nil
        }

        // If a replay notification arrived while we were still streaming, honor
        // it now that isStreaming is false. Guard with `!wasBackendError` so we
        // don't double-replay when the teardown below already schedules one.
        let replayWasDeferred = deferredReplayPending
        deferredReplayPending = false

        // Tell AgentManager that MLX is no longer busy —
        // health checks can resume normal thresholds.
        agentManager?.endInference()

        // Clear recovery UI state
        isRecovering = false
        recoveryStage = ""
        recoveryDetail = ""

        // If canceled, don't do error checking or replay
        if Task.isCancelled { return }

        // Check if the response was a HARD backend error — meaning MLX or
        // the agent is genuinely unreachable. We're now much more conservative
        // about triggering auto-replay than before, because the previous
        // logic kept misclassifying slow legitimate turns as failures and
        // wiping the user's message. The new policy:
        //
        //   1. Auto-replay ONLY when AgentManager explicitly says the backend
        //      restarted while we were waiting (replayWasDeferred), since
        //      that's unambiguous ground truth. Other "backend error"
        //      strings (timeout, connection refused) get LEFT IN PLACE so
        //      the user sees them and can decide whether to retry.
        //
        //   2. NEVER delete the user message. Even on retry, we leave the
        //      original message visible. The previous deletion behavior
        //      was an attempt to avoid duplicates on replay, but the
        //      cost — losing the user's input on a transient error — was
        //      way too high. Replay now appends a fresh user message
        //      instead of editing in place.
        //
        //   3. Truncated streams (deltas without a .done event) are NO
        //      LONGER treated as errors. Long-thinking turns fall into
        //      this category — the model is grinding on a hard problem
        //      and the stream is just slow, not broken. Killing the
        //      partial message in the middle of a long compute was
        //      directly user-hostile.
        if replayWasDeferred && !streamSucceeded {
            // AgentManager explicitly told us a replay is pending AND we
            // never received a single token from the stream — that's
            // unambiguous evidence the backend died before producing
            // anything, so a replay is the right call.
            print("[AppViewModel] Replay was deferred by AgentManager and stream produced no tokens — triggering")
            Task {
                await waitForHealthyThenReplay()
            }
        } else {
            // For ALL other paths — including the case where AgentManager
            // posted agentReadyForReplay while a stream was still in flight
            // but the stream actually produced output — do not replay.
            // Replaying after a successful stream would duplicate the
            // user's prompt and re-run an entire turn that the user
            // already got a (possibly imperfect) response to.
            //
            // Why this matters: the user reported a case where they got
            // through step 4 of a 5-step plan, the model errored on a
            // task_update call, the agent's health checks misclassified
            // the in-flight stuck-thinking as a backend death, MLX was
            // restarted (firing agentReadyForReplay), and on teardown the
            // pendingMessage got auto-replayed — wiping the user's
            // in-progress conversation by re-running the whole turn from
            // scratch.
            //
            // Hard errors visible in the assistant message content are
            // also handled here: the user sees the error inline and can
            // manually retry by typing again or using the regenerate
            // button. No deletion, no automatic retry, no surprise.
            if replayWasDeferred && streamSucceeded {
                print("[AppViewModel] Replay was deferred but stream produced tokens — skipping replay (would duplicate user prompt)")
            }
            agentManager?.clearPendingMessage()
            replayAttempts = 0
        }

        // Phase 1 queued-message dequeue. If the user typed one OR MORE
        // messages while this stream was running, they were appended with
        // isQueued=true (each as its own orange bubble). Now that the
        // stream is done (and not user-cancelled per the Task.isCancelled
        // check above), flip ALL queued bubbles to non-queued at once and
        // dispatch a single fresh send. The trailing-user fold inside
        // sendMessage's messagesToSend construction collapses the bubbles
        // into one combined user message before they hit the model — the
        // UI keeps them as separate bubbles.
        if let convIdx = conversations.firstIndex(where: { $0.id == targetConversationId }) {
            let queuedMsgs = conversations[convIdx].messages.filter {
                $0.role == .user && $0.isQueued
            }
            if let firstQueued = queuedMsgs.first {
                withAnimation(.easeInOut(duration: 0.3)) {
                    for i in conversations[convIdx].messages.indices
                    where conversations[convIdx].messages[i].role == .user
                          && conversations[convIdx].messages[i].isQueued {
                        conversations[convIdx].messages[i].isQueued = false
                    }
                    if selectedConversation?.id == targetConversationId {
                        selectedConversation = conversations[convIdx]
                    }
                }
                let queuedId = firstQueued.id
                let queuedContent = firstQueued.content
                let queuedAttachments = firstQueued.attachments
                let task = Task { [weak self] in
                    guard let self else { return }
                    await self.sendMessage(
                        content: queuedContent,
                        attachments: queuedAttachments,
                        reusingMessageId: queuedId,
                        conversationId: targetConversationId
                    )
                }
                streamingTask = task
            }
        }
    }

    /// Append a user message in the queued state. Used when the user types
    /// while an agent stream is already running. The queued bubble appears
    /// immediately (orange tint via MessageBubbleView) and is dequeued by
    /// the trailing block of `sendMessage` once the active stream ends.
    ///
    /// Mutates ``conversations[idx]`` in place rather than copying through
    /// ``selectedConversation``. The streaming-delta path *also* writes
    /// back full conversation copies; if we read ``selectedConversation``
    /// here, mutated, and wrote back, a stream delta landing in between
    /// two enqueues would clobber the first queued bubble. In-place
    /// mutation on the shared array element keeps each enqueue
    /// independent and additive — multi-message queueing works.
    func enqueueMessage(content: String, attachments: [Attachment] = []) {
        guard let convId = selectedConversation?.id,
              let idx = conversations.firstIndex(where: { $0.id == convId })
        else { return }
        let queued = ChatMessage(
            role: .user,
            content: content,
            attachments: attachments,
            isQueued: true
        )
        withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) {
            conversations[idx].messages.append(queued)
            conversations[idx].updatedAt = Date()
            if selectedConversation?.id == convId {
                selectedConversation = conversations[idx]
            }
        }
        persistence.saveConversation(conversations[idx])
    }

    /// Collapse the trailing run of ``role == .user`` messages into a
    /// single message so the model sees them as one combined turn. The
    /// UI continues to render them as separate bubbles — only the API
    /// payload is folded.
    ///
    /// Used by ``sendMessage`` when building ``messagesToSend``: if the
    /// user queued multiple messages while the agent was busy, all of
    /// them flip to non-queued on dequeue and the model gets one
    /// concatenated message instead of N separate user turns.
    private func foldTrailingUserMessages(_ msgs: [ChatMessage]) -> [ChatMessage] {
        guard !msgs.isEmpty else { return msgs }
        // Find the last non-user message; everything after it is the
        // trailing user run.
        var lastNonUserIdx = -1
        for i in msgs.indices where msgs[i].role != .user {
            lastNonUserIdx = i
        }
        let trailingStart = lastNonUserIdx + 1
        let trailing = msgs[trailingStart...]
        if trailing.count <= 1 { return msgs }
        let combinedContent = trailing.map(\.content).joined(separator: "\n\n")
        var combinedAttachments: [Attachment] = []
        for m in trailing { combinedAttachments.append(contentsOf: m.attachments) }
        let folded = ChatMessage(
            id: trailing.first!.id,
            role: .user,
            content: combinedContent,
            attachments: combinedAttachments,
            timestamp: trailing.first!.timestamp
        )
        return Array(msgs[..<trailingStart]) + [folded]
    }

    /// Wait up to `timeout` seconds for the agent to become connectable.
    /// Returns true if agent is ready, false if timed out.
    private func waitForAgentReady(timeout: Int) async -> Bool {
        guard let manager = agentManager else { return false }
        // Quick check first
        if manager.isConnected && (manager.agentStatus == .running || manager.agentStatus == .inferring) {
            return true
        }
        // Let ensureReady handle it
        let ready = await manager.ensureReady()
        if ready { return true }
        // Poll as fallback
        for _ in 0..<timeout {
            try? await Task.sleep(for: .seconds(1))
            if manager.isConnected { return true }
        }
        return false
    }

    /// Wait for the agent + MLX to become healthy, then replay the pending message.
    private func waitForHealthyThenReplay() async {
        guard let manager = agentManager else { return }

        // Poll every 3 seconds for up to 2 minutes
        for _ in 0..<40 {
            try? await Task.sleep(for: .seconds(3))
            if manager.isConnected && (manager.agentStatus == .running || manager.agentStatus == .inferring) {
                // Give it one more second to stabilize
                try? await Task.sleep(for: .seconds(1))
                await replayPendingMessage()
                return
            }
        }
        // Gave up — show the error to the user
        print("[AppViewModel] Agent didn't recover in 2 minutes, giving up on auto-retry")
        agentManager?.clearPendingMessage()
    }

    // MARK: - Pending Message Replay

    /// Called when AgentManager restarts after a crash and there's a pending message.
    private func replayPendingMessage() async {
        // If a stream is still active (e.g., health check killed the agent mid-turn
        // while we were waiting for user permission), we CAN'T replay immediately —
        // that would duplicate the user message. Defer it: the teardown in
        // sendMessage() will pick up the flag after cleanup and call us again.
        if isStreaming {
            print("[AppViewModel] Stream still active — deferring replay until teardown")
            deferredReplayPending = true
            return
        }
        guard let pending = agentManager?.pendingMessage else { return }

        // Switch to the conversation that had the pending message
        if let target = conversations.first(where: { $0.id == pending.conversationId }) {
            switchToConversation(target)
        }

        // Small delay to let the UI settle
        try? await Task.sleep(for: .seconds(1))

        // Replay the message
        await sendMessage(content: pending.content, attachments: pending.attachments)
    }

    // MARK: - File Path Detection

    /// Detect file paths in agent response text, verify they exist on disk,
    /// and return FileReference objects with accurate file sizes.
    private func detectFilePaths(in content: String, conversationId: UUID) -> [FileReference] {
        var refs: [FileReference] = []
        var seenPaths: Set<String> = []

        // Regex patterns for Unix file paths with known extensions
        let patterns: [String] = [
            #"(/Users/[\w/\-. ]+\.(?:\w{1,5}))"#,
            #"(~/[\w/\-. ]+\.(?:\w{1,5}))"#,
            #"(/tmp/[\w/\-. ]+\.(?:\w{1,5}))"#,
            #"(/var/[\w/\-. ]+\.(?:\w{1,5}))"#
        ]

        let fm = FileManager.default

        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern) else { continue }
            let nsContent = content as NSString
            let range = NSRange(location: 0, length: nsContent.length)
            let matches = regex.matches(in: content, range: range)

            for match in matches {
                var pathStr = nsContent.substring(with: match.range(at: 1))

                // Strip trailing punctuation that regex might capture
                while pathStr.hasSuffix(")") || pathStr.hasSuffix(".") || pathStr.hasSuffix(",") || pathStr.hasSuffix("`") || pathStr.hasSuffix("\"") {
                    pathStr = String(pathStr.dropLast())
                }

                // Expand ~ to home directory
                let expandedPath = (pathStr as NSString).expandingTildeInPath

                // Check extension is supported
                let ext = (expandedPath as NSString).pathExtension.lowercased()
                guard SupportedFileExtensions.all.contains(ext) else { continue }

                // Deduplicate
                guard !seenPaths.contains(expandedPath) else { continue }
                seenPaths.insert(expandedPath)

                // Verify file actually exists on disk
                guard fm.fileExists(atPath: expandedPath) else {
                    print("[FileDetection] Path found but file does not exist: \(expandedPath)")
                    continue
                }

                let fileName = (expandedPath as NSString).lastPathComponent
                let mimeType = MIMEType.from(extension: ext)

                // Get actual file size
                var fileSize: Int64 = 0
                if let attrs = try? fm.attributesOfItem(atPath: expandedPath),
                   let size = attrs[.size] as? Int64 {
                    fileSize = size
                }

                let fileRef = FileReference(
                    fileName: fileName,
                    mimeType: mimeType,
                    relativePath: "",
                    originalPath: expandedPath,
                    fileSize: fileSize
                )
                refs.append(fileRef)
                print("[FileDetection] Found file: \(expandedPath) (\(fileRef.formattedSize))")
            }
        }

        return refs
    }

    // MARK: - Answer Question (ask_user)

    /// Submit the user's answer to an agent question.
    /// Updates the local question state and POSTs to /v1/answer so
    /// the agent's blocked run_turn loop can resume.
    func answerQuestion(questionId: String, answer: String) async {
        print("[answerQuestion] CALLED questionId=\(questionId) answer=\(answer)")
        guard var conversation = selectedConversation else {
            print("[answerQuestion] ERROR: selectedConversation is nil")
            return
        }
        print("[answerQuestion] conversation=\(conversation.id) messages=\(conversation.messages.count)")

        // Find the message with this pending question and mark it as answered
        var found = false
        for msgIdx in conversation.messages.indices {
            let qs = conversation.messages[msgIdx].pendingQuestions
            if !qs.isEmpty {
                print("[answerQuestion] msg[\(msgIdx)] has \(qs.count) pendingQuestions: \(qs.map { $0.id })")
            }
            if let qIdx = conversation.messages[msgIdx].pendingQuestions.firstIndex(where: { $0.id == questionId }) {
                conversation.messages[msgIdx].pendingQuestions[qIdx].selectedChoice = answer
                found = true
                print("[answerQuestion] FOUND and set selectedChoice at msg[\(msgIdx)].q[\(qIdx)]")
                break
            }
        }
        if !found {
            print("[answerQuestion] WARNING: question \(questionId) not found in any message!")
        }
        updateConversation(conversation, forceSave: true)
        print("[answerQuestion] updateConversation done, posting to agent...")

        // POST to the agent so the blocked thread unblocks
        do {
            try await apiService.submitAnswer(
                conversationId: conversation.id,
                questionId: questionId,
                answer: answer
            )
            print("[answerQuestion] POST /v1/answer succeeded")
        } catch {
            print("[answerQuestion] Failed to submit answer: \(error)")
        }
    }

    // MARK: - Respond to Permission Request

    /// Submit the user's response to a folder permission request.
    /// Updates local state and POSTs to /v1/grant_folder to unblock the agent.
    func respondToPermission(permissionId: String, granted: Bool, path: String) async {
        guard var conversation = selectedConversation else { return }

        // Update local state
        for msgIdx in conversation.messages.indices {
            if let pIdx = conversation.messages[msgIdx].pendingPermissions.firstIndex(where: { $0.id == permissionId }) {
                conversation.messages[msgIdx].pendingPermissions[pIdx].granted = granted
                break
            }
        }
        updateConversation(conversation, forceSave: true)

        // POST to agent
        do {
            try await apiService.submitPermission(
                conversationId: conversation.id,
                permissionId: permissionId,
                granted: granted,
                path: path
            )
            print("[Permission] \(granted ? "Granted" : "Denied") \(path)")
        } catch {
            print("[Permission] Failed to submit: \(error)")
        }
    }

    func stopStreaming() {
        // Tell the agent first so it can mark any in-progress plan step
        // as interrupted before we tear down the local stream. We fire it
        // as a detached task because the local cancellation must happen
        // immediately for the UI to feel responsive.
        if let convId = selectedConversation?.id {
            Task.detached { [apiService] in
                await apiService.requestStop(conversationId: convId)
            }
        }

        isStreaming = false
        agentManager?.endInference()
        apiService.cancelStream()

        // Mark current streaming message as complete
        if var conversation = selectedConversation,
           let lastIndex = conversation.messages.lastIndex(where: { $0.isStreaming }) {
            conversation.messages[lastIndex].isStreaming = false
            updateConversation(conversation)
        }
    }
    
    /// Update in-memory state and optionally persist to disk.
    /// During streaming, disk writes are throttled to `saveInterval`.
    private func updateConversation(_ conversation: Conversation, forceSave: Bool = false) {
        if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
            conversations[index] = conversation
        }
        // Only update selectedConversation if it's still the same conversation.
        // This prevents a background stream from hijacking the selection when
        // the user has already switched to a different conversation.
        if selectedConversation?.id == conversation.id {
            selectedConversation = conversation
        }

        let now = Date()
        if forceSave || now.timeIntervalSince(lastSaveTime) >= saveInterval {
            persistence.saveConversation(conversation)
            lastSaveTime = now
        }
    }
    
    // MARK: - Settings
    
    func updateAPISettings() {
        apiService = APIService(
            baseURL: persistence.apiEndpoint,
            model: persistence.modelName,
            temperature: persistence.temperature,
            maxTokens: persistence.maxTokens
        )
    }
    
    func checkConnection() async {
        if let manager = agentManager {
            isConnected = manager.isConnected
        } else {
            await apiService.checkConnection()
            isConnected = apiService.isConnected
        }
    }

    // MARK: - Permissions

    /// Signals ContentView to open the Permissions settings tab.
    /// ContentView observes this and uses @Environment(\.openSettings)
    /// since AppViewModel can't access SwiftUI environment directly.
    @Published var shouldOpenPermissions = false

    func openPermissionsSettings() {
        shouldOpenPermissions = true
        // Post notification to switch to Permissions tab once window appears
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            NotificationCenter.default.post(name: .openPermissionsTab, object: nil)
        }
    }
}

extension Notification.Name {
    static let openPermissionsTab = Notification.Name("openPermissionsTab")
}
