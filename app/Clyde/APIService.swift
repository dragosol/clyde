//
//  APIService.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//

import Foundation
import Combine

@MainActor
class APIService: ObservableObject {
    @Published var isConnected = false

    private let baseURL: String
    private let model: String
    private let temperature: Double
    private let maxTokens: Int

    /// Active streaming task — cancelled on stopStreaming()
    private var streamTask: Task<Void, Never>?

    init(
        baseURL: String = "http://127.0.0.1:8801",
        model: String = "clyde-qwen",
        temperature: Double = 0.7,
        maxTokens: Int = 4096
    ) {
        // Strip trailing slashes to prevent double-slash in URL paths
        var cleanURL = baseURL
        while cleanURL.hasSuffix("/") { cleanURL.removeLast() }
        self.baseURL = cleanURL
        self.model = model
        self.temperature = temperature
        self.maxTokens = maxTokens

        Task { await checkConnection() }
    }

    // MARK: - Connection Check

    func checkConnection() async {
        guard let url = URL(string: "\(baseURL)/v1/models") else {
            isConnected = false
            return
        }

        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            if let httpResponse = response as? HTTPURLResponse {
                isConnected = (200...299).contains(httpResponse.statusCode)
            }
        } catch {
            isConnected = false
        }
    }

    // MARK: - Streaming Chat

    func streamChat(messages: [ChatMessage], conversationId: UUID? = nil) -> AsyncThrowingStream<StreamDelta, Error> {
        // Cancel any existing stream before starting a new one
        streamTask?.cancel()

        // Capture self weakly for storing the task from inside the closure.
        // The inner @MainActor Task re-captures [weak self] explicitly —
        // without this, Swift 6's strict concurrency flags the outer
        // `self?` reference inside a concurrently-executing closure.
        let storeTask: @Sendable (Task<Void, Never>) -> Void = { [weak self] task in
            Task { @MainActor [weak self] in
                self?.streamTask = task
            }
        }

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    // Build API messages — skip system messages (agent injects its own)
                    // and skip empty assistant placeholders
                    let apiMessages = messages.compactMap { message -> APIMessage? in
                        if message.role == .system { return nil }
                        if message.role == .assistant && message.content.isEmpty && !message.isStreaming {
                            return nil
                        }

                        var contentParts: [ContentPart] = []

                        // Add text content
                        if !message.content.isEmpty {
                            contentParts.append(.text(message.content))
                        }

                        // Add attachments as data URLs
                        // The agent routes these based on MIME prefix:
                        //   data:image/* → vision sidecar
                        //   data:video/* → video sidecar
                        //   data:application/* or other → document sidecar
                        for attachment in message.attachments {
                            // Embed filename via ;name= so the agent can preserve it
                            let safeName = attachment.fileName
                                .addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? attachment.fileName
                            let dataURL = "data:\(attachment.mimeType);name=\(safeName);base64,\(attachment.base64Data)"
                            contentParts.append(.imageUrl(ContentPart.ImageURL(url: dataURL)))
                        }

                        if contentParts.isEmpty {
                            contentParts.append(.text(""))
                        }

                        return APIMessage(role: message.role.rawValue, content: contentParts)
                    }

                    let request = ChatCompletionRequest(
                        model: model,
                        messages: apiMessages,
                        stream: true,
                        temperature: temperature,
                        maxTokens: maxTokens
                    )

                    guard let url = URL(string: "\(baseURL)/v1/chat/completions") else {
                        throw APIError.invalidURL
                    }

                    var urlRequest = URLRequest(url: url)
                    urlRequest.httpMethod = "POST"
                    urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    if let convId = conversationId {
                        urlRequest.setValue(convId.uuidString, forHTTPHeaderField: "X-Conversation-Id")
                    }
                    urlRequest.timeoutInterval = 300 // Agent can take a while with tool calls
                    urlRequest.httpBody = try JSONEncoder().encode(request)

                    let (bytes, response) = try await URLSession.shared.bytes(for: urlRequest)

                    guard let httpResponse = response as? HTTPURLResponse,
                          (200...299).contains(httpResponse.statusCode) else {
                        let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                        throw APIError.requestFailed(statusCode: statusCode)
                    }

                    // Parse SSE stream from agent
                    var thinkingBuffer = ""
                    var isInThinkBlock = false
                    var contentBuffer = ""
                    var hasEmittedContent = false

                    for try await line in bytes.lines {
                        // Check for cancellation
                        if Task.isCancelled { break }

                        // SSE format: "data: {json}" or "data: [DONE]"
                        guard line.hasPrefix("data: ") else { continue }
                        let data = String(line.dropFirst(6))

                        if data == "[DONE]" {
                            // Flush any remaining content
                            if !contentBuffer.isEmpty {
                                continuation.yield(StreamDelta(type: .content(contentBuffer)))
                                contentBuffer = ""
                            }
                            continuation.yield(StreamDelta(type: .done))
                            break
                        }

                        // Parse JSON chunk
                        guard let jsonData = data.data(using: .utf8) else { continue }

                        let chunk: ChatCompletionChunk
                        do {
                            chunk = try JSONDecoder().decode(ChatCompletionChunk.self, from: jsonData)
                        } catch {
                            #if DEBUG
                            print("[APIService] JSON decode error: \(error.localizedDescription) — data: \(data.prefix(200))")
                            #endif
                            continue
                        }

                        // Live metrics arrive on a SEPARATE channel
                        // (delta.clyde_metrics) — never via delta.content,
                        // so they can't leak into thinking/content streams.
                        if let mp = chunk.choices.first?.delta.clyde_metrics {
                            let m = LiveMetrics(
                                phase: mp.phase ?? "",
                                tokens: mp.tokens ?? 0,
                                tps: mp.tps ?? 0,
                                elapsed: mp.elapsed ?? 0,
                                thinkingChars: mp.thinking_chars ?? 0,
                                contentChars: mp.content_chars ?? 0
                            )
                            continuation.yield(StreamDelta(type: .metrics(m)))
                            continue
                        }

                        // Context pressure — conversation tokens vs the
                        // agent's compaction threshold. Also on a dedicated
                        // channel (delta.clyde_context).
                        if let cp = chunk.choices.first?.delta.clyde_context {
                            let snapshot = ContextPressure(
                                tokens: cp.tokens ?? 0,
                                threshold: cp.threshold ?? 0,
                                messages: cp.messages ?? 0,
                                timestamp: Date()
                            )
                            continuation.yield(StreamDelta(type: .contextSnapshot(snapshot)))
                            continue
                        }

                        guard let content = chunk.choices.first?.delta.content,
                              !content.isEmpty else {
                            continue
                        }

                        // Process content through the status marker parser
                        let deltas = processStreamContent(
                            content,
                            thinkingBuffer: &thinkingBuffer,
                            isInThinkBlock: &isInThinkBlock,
                            contentBuffer: &contentBuffer,
                            hasEmittedContent: &hasEmittedContent
                        )

                        for delta in deltas {
                            continuation.yield(delta)
                        }
                    }

                    continuation.finish()

                } catch {
                    if !Task.isCancelled {
                        continuation.finish(throwing: error)
                    } else {
                        continuation.finish()
                    }
                }
            }

            storeTask(task)

            continuation.onTermination = { @Sendable _ in
                task.cancel()
            }
        }
    }

    func cancelStream() {
        streamTask?.cancel()
        streamTask = nil
    }

    // MARK: - Stop Turn (user pressed stop button)

    /// Tell the agent's runtime to interrupt the current turn for a conversation.
    /// The runtime checks the stop flag at safe points (between iterations,
    /// mid-stream) and exits cleanly. If a plan is active, the in-progress
    /// step is marked as interrupted so the user can resume from artifact state.
    func requestStop(conversationId: UUID) async {
        guard let url = URL(string: "\(baseURL)/v1/stop") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 5
        let body: [String: String] = ["conversation_id": conversationId.uuidString]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let httpResponse = response as? HTTPURLResponse,
               !(200...299).contains(httpResponse.statusCode) {
                print("[APIService] /v1/stop returned \(httpResponse.statusCode)")
            }
        } catch {
            print("[APIService] /v1/stop request failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Answer Submission (ask_user)

    /// Submit a user's answer to an ask_user question.
    /// The agent blocks its run_turn loop until this arrives.
    func submitAnswer(conversationId: UUID, questionId: String, answer: String) async throws {
        guard let url = URL(string: "\(baseURL)/v1/answer") else {
            throw APIError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10

        let body: [String: String] = [
            "conversation_id": conversationId.uuidString,
            "question_id": questionId,
            "answer": answer,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (_, response) = try await URLSession.shared.data(for: request)
        if let httpResponse = response as? HTTPURLResponse,
           !(200...299).contains(httpResponse.statusCode) {
            throw APIError.requestFailed(statusCode: httpResponse.statusCode)
        }
    }

    // MARK: - Permission Submission

    /// Submit the user's response to a folder permission request.
    /// The agent blocks its tool execution until this arrives.
    func submitPermission(conversationId: UUID, permissionId: String, granted: Bool, path: String) async throws {
        guard let url = URL(string: "\(baseURL)/v1/grant_folder") else {
            throw APIError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10

        let body: [String: Any] = [
            "conversation_id": conversationId.uuidString,
            "permission_id": permissionId,
            "granted": granted,
            "path": path,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (_, response) = try await URLSession.shared.data(for: request)
        if let httpResponse = response as? HTTPURLResponse,
           !(200...299).contains(httpResponse.statusCode) {
            throw APIError.requestFailed(statusCode: httpResponse.statusCode)
        }
    }

    // MARK: - Content Processing

    /// Parses agent status markers from streamed content.
    ///
    /// The Clyde agent sends these markers in SSE chunks:
    ///   *thinking...*\n\n          → thinking status
    ///   *using bash* `ls -la`\n   → tool start with args preview
    ///   *bash done*\n\n           → tool finished
    ///   *bash error*\n\n          → tool errored
    ///   ---\n\n                   → separator before final answer
    ///   <think>...</think>        → thinking block content (from model)
    ///
    private func processStreamContent(
        _ content: String,
        thinkingBuffer: inout String,
        isInThinkBlock: inout Bool,
        contentBuffer: inout String,
        hasEmittedContent: inout Bool
    ) -> [StreamDelta] {
        var deltas: [StreamDelta] = []
        var remaining = content

        // Normalize thinking tags to <think>/<\/think> format for consistent parsing.
        // This handles various model-specific thinking tag formats.
        remaining = remaining
            .replacingOccurrences(of: "<|channel>thought", with: "<think>")
            .replacingOccurrences(of: "<channel|>", with: "</think>")
            .replacingOccurrences(of: "<|channel>", with: "</think>")
            .replacingOccurrences(of: "<|think|>", with: "<think>")
            .replacingOccurrences(of: "<|/think|>", with: "</think>")

        // Strip bare tool call prefix markers to prevent raw text duplication.
        // The tool call card renders separately.
        if let toolRange = remaining.range(of: #"(?m)^tool\s*$"#, options: .regularExpression) {
            remaining.removeSubrange(toolRange)
        }
        // Strip raw function call syntax: func_name(key="val", ...)
        let toolNames = ["read_file", "write_file", "edit_file", "bash", "glob_search",
                         "grep_search", "ask_user", "web_search", "web_fetch",
                         "memory_read", "memory_write", "memory_update", "memory_delete",
                         "memory_search", "memory_list", "transcript_search"]
        for name in toolNames {
            // Match func_name( ... ) with content in parens
            let pattern = #"(?s)"# + NSRegularExpression.escapedPattern(for: name) + #"\(.*?\)\s*"#
            if let regex = try? NSRegularExpression(pattern: pattern),
               let match = regex.firstMatch(in: remaining, range: NSRange(remaining.startIndex..., in: remaining)),
               let range = Range(match.range, in: remaining) {
                remaining.removeSubrange(range)
            }
        }

        // If we're inside a <think> block, buffer until we find </think>
        if isInThinkBlock {
            if let endRange = remaining.range(of: "</think>") {
                let thinkContent = String(remaining[..<endRange.lowerBound])
                thinkingBuffer += thinkContent
                deltas.append(StreamDelta(type: .thinking(thinkingBuffer)))
                // Keep accumulated content with separator for next round
                thinkingBuffer += "\n\n"
                isInThinkBlock = false
                remaining = String(remaining[endRange.upperBound...])
            } else {
                thinkingBuffer += remaining
                // Yield incremental thinking updates so the UI shows progress
                deltas.append(StreamDelta(type: .thinking(thinkingBuffer)))
                return deltas
            }
        }

        // Process remaining content for markers and <think> tags
        while !remaining.isEmpty {
            // Check for <think> opening tag
            if let startRange = remaining.range(of: "<think>") {
                // Emit any content before the tag
                let before = String(remaining[..<startRange.lowerBound])
                if !before.isEmpty {
                    let parsed = parseStatusMarkers(before, hasEmittedContent: &hasEmittedContent)
                    deltas.append(contentsOf: parsed)
                }
                isInThinkBlock = true
                remaining = String(remaining[startRange.upperBound...])

                // Check if </think> is also in this chunk
                if let endRange = remaining.range(of: "</think>") {
                    // Append to buffer (accumulates across rounds)
                    thinkingBuffer += String(remaining[..<endRange.lowerBound])
                    deltas.append(StreamDelta(type: .thinking(thinkingBuffer)))
                    // Keep accumulated content with separator for next round
                    thinkingBuffer += "\n\n"
                    isInThinkBlock = false
                    remaining = String(remaining[endRange.upperBound...])
                } else {
                    // Append to buffer (accumulates across rounds)
                    thinkingBuffer += remaining
                    deltas.append(StreamDelta(type: .thinking(thinkingBuffer)))
                    remaining = ""
                }
            } else {
                // No more <think> tags — parse for status markers
                let parsed = parseStatusMarkers(remaining, hasEmittedContent: &hasEmittedContent)
                deltas.append(contentsOf: parsed)
                remaining = ""
            }
        }

        return deltas
    }

    /// Parse agent status markers from a chunk of text.
    /// Returns StreamDeltas for markers and content.
    /// Buffered tool output from <<tool_output:BASE64>>, paired with the tool
    /// name that was most recently started via `*using X*`. Verified against
    /// the tool name in `*X done*` so overlapping or mismatched markers don't
    /// cross-attribute output.
    private var pendingToolOutput: (toolName: String, output: String)?
    /// Name of the tool most recently started — used to label `<<tool_output>>`
    /// blobs when they arrive, since the marker itself carries no name.
    private var lastStartedToolName: String?

    private func parseStatusMarkers(_ text: String, hasEmittedContent: inout Bool) -> [StreamDelta] {
        var deltas: [StreamDelta] = []

        // Agent status marker patterns:
        //   *thinking...*
        //   *using toolname* `args preview`
        //   <<tool_output:BASE64>>   (full tool output, base64-encoded)
        //   *toolname done*
        //   *toolname error*
        //   --- (only treated as separator if no regular content has been emitted yet,
        //        i.e. it follows tool calls. Otherwise it's a markdown horizontal rule.)

        // Process line by line since markers are line-oriented
        let lines = text.components(separatedBy: "\n")
        var contentLines: [String] = []

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // *thinking...*
            if trimmed == "*thinking...*" {
                // Flush buffered content
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                deltas.append(StreamDelta(type: .thinkingStart))
                continue
            }

            // *using toolname* `args`  or  *using toolname*
            if let match = trimmed.range(of: #"^\*using\s+(\S+)\*(.*)$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                // Extract tool name and optional args preview
                let matched = String(trimmed[match])
                let namePattern = try? NSRegularExpression(pattern: #"^\*using\s+(\S+)\*\s*`?([^`]*)`?$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = namePattern?.firstMatch(in: matched, range: nsRange) {
                    let name = result.range(at: 1).location != NSNotFound
                        ? String(matched[Range(result.range(at: 1), in: matched)!])
                        : "tool"
                    let args = result.range(at: 2).location != NSNotFound
                        ? String(matched[Range(result.range(at: 2), in: matched)!]).trimmingCharacters(in: .whitespaces)
                        : ""
                    // Track the tool name so an incoming <<tool_output>> can be
                    // paired with it. If a previous tool's output never arrived
                    // (e.g., agent skipped it), drop the stale buffer.
                    if pendingToolOutput != nil {
                        print("[APIService] Dropping orphaned tool output from \(pendingToolOutput!.toolName) — new tool \(name) started")
                        pendingToolOutput = nil
                    }
                    lastStartedToolName = name
                    deltas.append(StreamDelta(type: .toolStart(name: name, argsPreview: args)))
                }
                continue
            }

            // <<tool_output:BASE64>> — full tool output, base64-encoded.
            // Paired with whichever tool was most recently started via *using X*.
            if trimmed.hasPrefix("<<tool_output:") && trimmed.hasSuffix(">>") {
                let b64Start = trimmed.index(trimmed.startIndex, offsetBy: 14)
                let b64End = trimmed.index(trimmed.endIndex, offsetBy: -2)
                if b64Start < b64End {
                    let b64String = String(trimmed[b64Start..<b64End])
                    if let data = Data(base64Encoded: b64String),
                       let decoded = String(data: data, encoding: .utf8) {
                        if let owner = lastStartedToolName {
                            pendingToolOutput = (toolName: owner, output: decoded)
                        } else {
                            print("[APIService] Received tool_output with no active tool — discarding")
                        }
                    }
                }
                continue
            }

            // Tool done markers:
            //   *toolname done*                    (no summary)
            //   *toolname error*                   (no summary)
            //   *toolname done · 8 results*        (with summary)
            //   *toolname error · fetch failed*    (error with summary)
            if let match = trimmed.range(of: #"^\*(\S+)\s+(done|error)(?:\s+·\s+(.+))?\*$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let matched = String(trimmed[match])
                let pattern = try? NSRegularExpression(pattern: #"^\*(\S+)\s+(done|error)(?:\s+·\s+(.+))?\*$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = pattern?.firstMatch(in: matched, range: nsRange) {
                    let name = String(matched[Range(result.range(at: 1), in: matched)!])
                    let status = String(matched[Range(result.range(at: 2), in: matched)!])
                    let isError = status == "error"
                    let summary: String
                    if result.range(at: 3).location != NSNotFound {
                        summary = String(matched[Range(result.range(at: 3), in: matched)!])
                    } else {
                        summary = ""
                    }
                    // Attach buffered tool output only if it matches this tool.
                    // If the names disagree, the agent likely interleaved tools
                    // or the output is stale — drop it rather than cross-attribute.
                    var output: String? = nil
                    if let pending = pendingToolOutput {
                        if pending.toolName == name {
                            output = pending.output
                        } else {
                            print("[APIService] Tool output owner mismatch: buffered=\(pending.toolName) done=\(name) — discarding")
                        }
                        pendingToolOutput = nil
                    }
                    if lastStartedToolName == name {
                        lastStartedToolName = nil
                    }
                    deltas.append(StreamDelta(type: .toolDone(name: name, isError: isError, summary: summary, output: output)))
                }
                continue
            }

            // *compacting · 180K/168K tokens* — context compaction started
            if let match = trimmed.range(of: #"^\*compacting\s+·\s+(.+)\*$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let matched = String(trimmed[match])
                let summaryPattern = try? NSRegularExpression(pattern: #"^\*compacting\s+·\s+(.+)\*$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = summaryPattern?.firstMatch(in: matched, range: nsRange),
                   result.range(at: 1).location != NSNotFound {
                    let summary = String(matched[Range(result.range(at: 1), in: matched)!])
                    deltas.append(StreamDelta(type: .compacting(summary: summary)))
                } else {
                    deltas.append(StreamDelta(type: .compacting(summary: "")))
                }
                continue
            }

            // *compact_done · 180K→45K tokens* — compaction finished
            if let match = trimmed.range(of: #"^\*compact_done\s+·\s+(.+)\*$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let matched = String(trimmed[match])
                let summaryPattern = try? NSRegularExpression(pattern: #"^\*compact_done\s+·\s+(.+)\*$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = summaryPattern?.firstMatch(in: matched, range: nsRange),
                   result.range(at: 1).location != NSNotFound {
                    let summary = String(matched[Range(result.range(at: 1), in: matched)!])
                    deltas.append(StreamDelta(type: .compactDone(summary: summary)))
                } else {
                    deltas.append(StreamDelta(type: .compactDone(summary: "")))
                }
                continue
            }

            // *compact_progress · phase · pct% · detail* — compaction progress update
            // Reuse the .compacting delta to update the progress bar summary.
            if let match = trimmed.range(of: #"^\*compact_progress\s+·\s+(.+)\*$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let matched = String(trimmed[match])
                // Parse: "phase · pct% · detail" — extract phase, percentage, and detail
                let progressPattern = try? NSRegularExpression(pattern: #"^\*compact_progress\s+·\s+(\S+)\s+·\s+(\d+)%\s+·\s+(.+)\*$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = progressPattern?.firstMatch(in: matched, range: nsRange) {
                    let phase = result.range(at: 1).location != NSNotFound
                        ? String(matched[Range(result.range(at: 1), in: matched)!]) : ""
                    let pct = result.range(at: 2).location != NSNotFound
                        ? String(matched[Range(result.range(at: 2), in: matched)!]) : "0"
                    let detail = result.range(at: 3).location != NSNotFound
                        ? String(matched[Range(result.range(at: 3), in: matched)!]) : ""
                    deltas.append(StreamDelta(type: .compacting(summary: "\(phase) \(pct)% · \(detail)")))
                } else {
                    deltas.append(StreamDelta(type: .compacting(summary: "")))
                }
                continue
            }

            // *recovering · stage · detail* — self-healing recovery progress
            if let match = trimmed.range(of: #"^\*recovering\s+·\s+(\S+)\s+·\s+(.+)\*$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let matched = String(trimmed[match])
                let recPattern = try? NSRegularExpression(pattern: #"^\*recovering\s+·\s+(\S+)\s+·\s+(.+)\*$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = recPattern?.firstMatch(in: matched, range: nsRange) {
                    let stage = result.range(at: 1).location != NSNotFound
                        ? String(matched[Range(result.range(at: 1), in: matched)!]) : ""
                    let detail = result.range(at: 2).location != NSNotFound
                        ? String(matched[Range(result.range(at: 2), in: matched)!]) : ""
                    deltas.append(StreamDelta(type: .recovering(stage: stage, detail: detail)))
                }
                continue
            }

            // *recovery_done · detail* — recovery completed successfully
            if let match = trimmed.range(of: #"^\*recovery_done\s+·\s+(.+)\*$"#, options: .regularExpression) {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let matched = String(trimmed[match])
                let donePattern = try? NSRegularExpression(pattern: #"^\*recovery_done\s+·\s+(.+)\*$"#)
                let nsRange = NSRange(matched.startIndex..., in: matched)
                if let result = donePattern?.firstMatch(in: matched, range: nsRange),
                   result.range(at: 1).location != NSNotFound {
                    let detail = String(matched[Range(result.range(at: 1), in: matched)!])
                    deltas.append(StreamDelta(type: .recoveryDone(detail: detail)))
                } else {
                    deltas.append(StreamDelta(type: .recoveryDone(detail: "")))
                }
                continue
            }

            // <<metrics:{JSON}>> — live tok/s + phase + elapsed from agent
            if trimmed.hasPrefix("<<metrics:") && trimmed.hasSuffix(">>") {
                let jsonStr = String(trimmed.dropFirst(10).dropLast(2))
                if let jsonData = jsonStr.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                    let m = LiveMetrics(
                        phase: obj["phase"] as? String ?? "",
                        tokens: obj["tokens"] as? Int ?? 0,
                        tps: obj["tps"] as? Double ?? 0,
                        elapsed: obj["elapsed"] as? Double ?? 0,
                        thinkingChars: obj["thinking_chars"] as? Int ?? 0,
                        contentChars: obj["content_chars"] as? Int ?? 0
                    )
                    deltas.append(StreamDelta(type: .metrics(m)))
                }
                continue
            }

            // <<question:{JSON}>> — multiple-choice question from ask_user tool
            if trimmed.hasPrefix("<<question:") && trimmed.hasSuffix(">>") {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let jsonStr = String(trimmed.dropFirst(11).dropLast(2))
                if let jsonData = jsonStr.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                    let qData = QuestionData(
                        id: obj["id"] as? String ?? UUID().uuidString,
                        question: obj["question"] as? String ?? "",
                        choices: obj["choices"] as? [String] ?? [],
                        allowOther: obj["allowOther"] as? Bool ?? true,
                        selectedChoice: nil
                    )
                    deltas.append(StreamDelta(type: .question(qData)))
                }
                continue
            }

            // <<plan_state:{JSON}>> — active plan snapshot from task_plan/update/complete
            // Payload: {"state": "active|pending_approval|completed|...", "plan": {...} | null}
            if trimmed.hasPrefix("<<plan_state:") && trimmed.hasSuffix(">>") {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let jsonStr = String(trimmed.dropFirst(13).dropLast(2))
                if let jsonData = jsonStr.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                    let state = obj["state"] as? String ?? "none"
                    var plan: PlanData? = nil
                    if let planObj = obj["plan"] as? [String: Any],
                       let planData = try? JSONSerialization.data(withJSONObject: planObj),
                       let parsed = try? JSONDecoder().decode(PlanData.self, from: planData) {
                        plan = parsed
                    }
                    deltas.append(StreamDelta(type: .planState(state: state, plan: plan)))
                }
                continue
            }

            // <<permission_request:{JSON}>> — folder permission request from agent
            if trimmed.hasPrefix("<<permission_request:") && trimmed.hasSuffix(">>") {
                if !contentLines.isEmpty {
                    let content = contentLines.joined(separator: "\n")
                    if !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        deltas.append(StreamDelta(type: .content(content)))
                        hasEmittedContent = true
                    }
                    contentLines = []
                }
                let jsonStr = String(trimmed.dropFirst(21).dropLast(2))
                if let jsonData = jsonStr.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                    let pData = PermissionRequestData(
                        id: obj["id"] as? String ?? UUID().uuidString,
                        path: obj["path"] as? String ?? "",
                        folder: obj["folder"] as? String ?? "",
                        granted: nil
                    )
                    deltas.append(StreamDelta(type: .permissionRequest(pData)))
                }
                continue
            }

            // --- separator: only treat as agent separator if we haven't started
            // emitting regular content yet (i.e. it follows tool-call markers).
            // Once content is flowing, --- is a markdown horizontal rule.
            if trimmed == "---" && !hasEmittedContent {
                contentLines = []
                deltas.append(StreamDelta(type: .separator))
                continue
            }

            // Regular content line — last-mile sweep for any embedded
            // `<<metrics:{...}>>` substrings that didn't land on their own
            // line (e.g. concatenated with thinking/content deltas in the
            // same SSE chunk). Extract them as metrics deltas, then keep the
            // surrounding text as normal content.
            var workLine = line
            if let regex = try? NSRegularExpression(pattern: "<<metrics:(\\{[^}]*\\})>>") {
                let nsRange = NSRange(workLine.startIndex..., in: workLine)
                let matches = regex.matches(in: workLine, range: nsRange).reversed()
                for match in matches {
                    if let payloadRange = Range(match.range(at: 1), in: workLine),
                       let fullRange = Range(match.range, in: workLine) {
                        let jsonStr = String(workLine[payloadRange])
                        if let jsonData = jsonStr.data(using: .utf8),
                           let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                            let m = LiveMetrics(
                                phase: obj["phase"] as? String ?? "",
                                tokens: obj["tokens"] as? Int ?? 0,
                                tps: obj["tps"] as? Double ?? 0,
                                elapsed: obj["elapsed"] as? Double ?? 0,
                                thinkingChars: obj["thinking_chars"] as? Int ?? 0,
                                contentChars: obj["content_chars"] as? Int ?? 0
                            )
                            deltas.append(StreamDelta(type: .metrics(m)))
                        }
                        workLine.removeSubrange(fullRange)
                    }
                }
            }
            if !workLine.isEmpty {
                contentLines.append(workLine)
            }
        }

        // Flush remaining content
        if !contentLines.isEmpty {
            let content = contentLines.joined(separator: "\n")
            if !content.isEmpty {
                deltas.append(StreamDelta(type: .content(content)))
            }
        }

        return deltas
    }
}

// MARK: - Errors

enum APIError: LocalizedError {
    case invalidURL
    case requestFailed(statusCode: Int)
    case decodingError

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid API URL"
        case .requestFailed(let code):
            return "API request failed (HTTP \(code))"
        case .decodingError:
            return "Failed to decode response"
        }
    }
}
