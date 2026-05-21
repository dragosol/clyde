//
//  DebugView.swift
//  Clyde
//
//  Created by Claude × Dragos on 2026-04-06.
//
//  Unified debug dashboard for the entire Clyde stack.
//  Pulls all data from a single GET /v1/debug endpoint.
//

import SwiftUI

// MARK: - Data Models

struct DebugSnapshot: Codable {
    let timestamp: Double
    let status: String
    let agent: DebugAgent
    let mlx: DebugMLX
    let memory: DebugMemory?
    let compaction: DebugCompaction?
    let conversations: [String: DebugConversation]?
    let recent_errors: [DebugError]?
    let system: DebugSystem?

    enum CodingKeys: String, CodingKey {
        case timestamp, status, agent, mlx, memory, compaction
        case conversations, recent_errors, system
    }
}

struct DebugAgent: Codable {
    let status: String
    let uptime_seconds: Double
    let pid: Int
    let memory_mb: Double
    let active_sessions: Int
    let sessions_on_disk: Int?
    let allowed_folders: [String]?
    let config: DebugAgentConfig?
    let metrics: DebugAgentMetrics?
}

struct DebugAgentConfig: Codable {
    let backend_url: String?
    let max_iterations: Int?
    let max_tokens: Int?
    let temperature: Double?
    let compact_threshold: Int?
}

struct DebugAgentMetrics: Codable {
    let total_turns: Int?
    let total_tool_calls: Int?
    let total_tokens_processed: Int?
    let avg_turn_duration_seconds: Double?
    let failed_turns: Int?
    let active_turn: DebugActiveTurn?
    let tool_usage: [String: Int]?
    let tool_errors: [String: Int]?
}

struct DebugActiveTurn: Codable {
    let turn_id: String?
    let iteration: Int?
    let elapsed_seconds: Double?
}

struct DebugMLX: Codable {
    let status: String
    let model: String?
    let port: Int?
    let process: [String: String]?
    let metrics: DebugMLXMetrics?
}

struct DebugMLXMetrics: Codable {
    let total_inferences: Int?
    let total_tokens_generated: Int?
    let avg_latency_ms: Double?
    let timeout_recoveries: Int?
    let oom_recoveries: Int?
    let crashes: Int?
}

struct DebugMemory: Codable {
    let index_size_bytes: Int?
    let index_modified: Double?
    let total_files: Int?
    let operations: DebugMemoryOps?
    let error: String?
}

struct DebugMemoryOps: Codable {
    let reads: Int?
    let writes: Int?
    let searches: Int?
    let updates: Int?
    let deletes: Int?
}

struct DebugCompaction: Codable {
    let total_triggered: Int?
    let total_failed: Int?
    let recent: [DebugCompactionEvent]?
}

struct DebugCompactionEvent: Codable {
    let timestamp: Double?
    let tokens_before: Int?
    let tokens_after: Int?
    let duration_seconds: Double?
    let success: Bool?
    let method: String?
}

struct DebugConversation: Codable {
    let messages: Int?
    let estimated_tokens: Int?
    let compact_threshold: Int?
    let percent_of_threshold: Double?
    let headroom_tokens: Int?
    let max_response_tokens: Int?
    let turns_until_compaction_worst_case: Int?
    let role_breakdown: DebugConversationRoleBreakdown?
    let tool_call_count: Int?
    let tool_result_count: Int?
    let largest_message_tokens: Int?
    let largest_message_role: String?
    let error: String?
}

struct DebugConversationRoleBreakdown: Codable {
    let system: DebugConversationRoleStat?
    let user: DebugConversationRoleStat?
    let assistant: DebugConversationRoleStat?
    let tool: DebugConversationRoleStat?
}

struct DebugConversationRoleStat: Codable {
    let tokens: Int?
    let messages: Int?
    let text_chars: Int?
}

struct DebugError: Codable, Identifiable {
    var id: String { "\(timestamp)-\(component)-\(error_type)" }
    let timestamp: Double
    let component: String
    let severity: String
    let error_type: String
    let message: String
    let turn_id: String?
    let resolved: Bool?
}

struct DebugSystem: Codable {
    let python_version: String?
    let platform: String?
}

// MARK: - Debug View

struct DebugView: View {
    @ObservedObject var agentManager: AgentManager
    @State private var data: DebugSnapshot?
    @State private var lastFetchError: String?
    @State private var autoRefresh = true
    @State private var selectedTab: DebugTab = .overview
    @State private var refreshTimer: Timer?

    enum DebugTab: String, CaseIterable {
        case overview = "Overview"
        case agent = "Agent"
        case mlx = "MLX"
        case memory = "Memory"
        case errors = "Errors"
    }

    var body: some View {
        VStack(spacing: 0) {
            // Header bar
            headerBar
            Divider()

            // Tab picker
            Picker("", selection: $selectedTab) {
                ForEach(DebugTab.allCases, id: \.self) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            // Content
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let data {
                        switch selectedTab {
                        case .overview:
                            overviewSection(data)
                        case .agent:
                            agentSection(data.agent)
                        case .mlx:
                            mlxSection(data.mlx)
                        case .memory:
                            memorySection(data)
                        case .errors:
                            errorsSection(data.recent_errors ?? [])
                        }
                    } else if let error = lastFetchError {
                        VStack(spacing: 8) {
                            Image(systemName: "exclamationmark.triangle")
                                .font(.largeTitle)
                                .foregroundColor(.orange)
                            Text("Could not connect to agent")
                                .font(.headline)
                            Text(error)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        .frame(maxWidth: .infinity, minHeight: 200)
                    } else {
                        ProgressView("Loading...")
                            .frame(maxWidth: .infinity, minHeight: 200)
                    }
                }
                .padding()
            }
        }
        .frame(minWidth: 500, minHeight: 400)
        .onAppear { fetchData(); startTimer() }
        .onDisappear { stopTimer() }
        .onChange(of: autoRefresh) { _, v in v ? startTimer() : stopTimer() }
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack {
            Text("Debug Dashboard")
                .font(.headline)

            Spacer()

            // Status pill
            HStack(spacing: 4) {
                Circle()
                    .fill(statusColor(data?.status))
                    .frame(width: 8, height: 8)
                Text(data?.status ?? "...")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Divider().frame(height: 16)

            Toggle("Auto", isOn: $autoRefresh)
                .toggleStyle(.switch)
                .controlSize(.small)

            Button(action: fetchData) {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    // MARK: - Overview

    @ViewBuilder
    private func overviewSection(_ d: DebugSnapshot) -> some View {
        // Component status grid
        LazyVGrid(columns: [
            GridItem(.flexible()), GridItem(.flexible()),
            GridItem(.flexible()), GridItem(.flexible()),
        ], spacing: 12) {
            statusCard("Agent", d.agent.status, icon: "cpu")
            statusCard("MLX", d.mlx.status, icon: "brain")
            statusCard("Memory", d.memory?.error == nil ? "ok" : "error", icon: "memorychip")
            statusCard("Compaction",
                        (d.compaction?.total_failed ?? 0) > 0 ? "degraded" : "ok",
                        icon: "arrow.trianglehead.2.counterclockwise")
        }

        // Quick metrics
        GroupBox("Key Metrics") {
            VStack(alignment: .leading, spacing: 6) {
                metricRow("Uptime", formatDuration(d.agent.uptime_seconds))
                metricRow("Active Sessions", "\(d.agent.active_sessions)")
                metricRow("Total Turns", "\(d.agent.metrics?.total_turns ?? 0)")
                metricRow("Tool Calls", "\(d.agent.metrics?.total_tool_calls ?? 0)")
                metricRow("MLX Inferences", "\(d.mlx.metrics?.total_inferences ?? 0)")
                metricRow("Recoveries",
                          "\(d.mlx.metrics?.timeout_recoveries ?? 0) timeout, \(d.mlx.metrics?.oom_recoveries ?? 0) OOM, \(d.mlx.metrics?.crashes ?? 0) crash")
                metricRow("Compactions", "\(d.compaction?.total_triggered ?? 0)")
            }
        }

        // Active turn (if any)
        if let turn = d.agent.metrics?.active_turn {
            GroupBox("Active Turn") {
                VStack(alignment: .leading, spacing: 4) {
                    metricRow("Turn ID", turn.turn_id ?? "—")
                    metricRow("Iteration", "\(turn.iteration ?? 0)")
                    metricRow("Elapsed", formatDuration(turn.elapsed_seconds ?? 0))
                }
            }
        }

        // Conversations — expanded with role breakdown, headroom, and
        // a visual context-window bar so the user can actually see why
        // compaction "never happens" (short conversations don't come
        // close to the 120k threshold).
        if let convos = d.conversations, !convos.isEmpty {
            GroupBox("Conversations") {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(Array(convos.keys.sorted()), id: \.self) { key in
                        if let c = convos[key] {
                            conversationCard(id: key, c: c)
                        }
                    }
                }
            }
        }

        // Recent errors (last 5)
        let errors = d.recent_errors ?? []
        if !errors.isEmpty {
            GroupBox("Recent Errors (\(errors.count))") {
                ForEach(errors.suffix(5).reversed()) { err in
                    errorRow(err)
                }
            }
        }
    }

    // MARK: - Agent Detail

    @ViewBuilder
    private func agentSection(_ a: DebugAgent) -> some View {
        GroupBox("Process") {
            VStack(alignment: .leading, spacing: 4) {
                metricRow("PID", "\(a.pid)")
                metricRow("Memory", String(format: "%.1f MB", a.memory_mb))
                metricRow("Uptime", formatDuration(a.uptime_seconds))
                metricRow("Status", a.status)
            }
        }

        if let m = a.metrics {
            GroupBox("Metrics") {
                VStack(alignment: .leading, spacing: 4) {
                    metricRow("Total Turns", "\(m.total_turns ?? 0)")
                    metricRow("Failed Turns", "\(m.failed_turns ?? 0)")
                    metricRow("Tool Calls", "\(m.total_tool_calls ?? 0)")
                    metricRow("Tokens Processed", "\(m.total_tokens_processed ?? 0)")
                    metricRow("Avg Turn Duration",
                              String(format: "%.1fs", m.avg_turn_duration_seconds ?? 0))
                }
            }

            if let tools = m.tool_usage, !tools.isEmpty {
                GroupBox("Tool Usage") {
                    ForEach(tools.sorted(by: { $0.value > $1.value }), id: \.key) { name, count in
                        HStack {
                            Text(name).font(.caption.monospaced())
                            Spacer()
                            Text("\(count)")
                                .font(.caption)
                                .foregroundColor(.secondary)
                            if let errs = m.tool_errors?[name], errs > 0 {
                                Text("(\(errs) err)")
                                    .font(.caption2)
                                    .foregroundColor(.red)
                            }
                        }
                    }
                }
            }
        }

        if let cfg = a.config {
            GroupBox("Config") {
                VStack(alignment: .leading, spacing: 4) {
                    metricRow("Backend", cfg.backend_url ?? "—")
                    metricRow("Max Iterations", "\(cfg.max_iterations ?? 0)")
                    metricRow("Max Tokens", "\(cfg.max_tokens ?? 0)")
                    metricRow("Temperature", String(format: "%.2f", cfg.temperature ?? 0))
                    metricRow("Compact Threshold", "\(cfg.compact_threshold ?? 0)")
                }
            }
        }

        if let folders = a.allowed_folders, !folders.isEmpty {
            GroupBox("Allowed Folders") {
                ForEach(folders, id: \.self) { f in
                    Text(f).font(.caption.monospaced())
                }
            }
        }
    }

    // MARK: - MLX Detail

    @ViewBuilder
    private func mlxSection(_ m: DebugMLX) -> some View {
        GroupBox("Status") {
            VStack(alignment: .leading, spacing: 4) {
                metricRow("Status", m.status)
                metricRow("Model", m.model ?? "—")
                metricRow("Port", "\(m.port ?? 0)")
            }
        }

        if let met = m.metrics {
            GroupBox("Performance") {
                VStack(alignment: .leading, spacing: 4) {
                    metricRow("Total Inferences", "\(met.total_inferences ?? 0)")
                    metricRow("Tokens Generated", "\(met.total_tokens_generated ?? 0)")
                    metricRow("Avg Latency", String(format: "%.0f ms", met.avg_latency_ms ?? 0))
                }
            }

            GroupBox("Recovery Events") {
                VStack(alignment: .leading, spacing: 4) {
                    recoveryRow("Timeouts", met.timeout_recoveries ?? 0)
                    recoveryRow("OOM Crashes", met.oom_recoveries ?? 0)
                    recoveryRow("Process Crashes", met.crashes ?? 0)
                }
            }
        }
    }

    // MARK: - Memory & Compaction

    @ViewBuilder
    private func memorySection(_ d: DebugSnapshot) -> some View {
        if let mem = d.memory {
            GroupBox("Memory System") {
                VStack(alignment: .leading, spacing: 4) {
                    metricRow("Index Size", formatBytes(mem.index_size_bytes ?? 0))
                    metricRow("Total Files", "\(mem.total_files ?? 0)")
                    if let ops = mem.operations {
                        metricRow("Reads", "\(ops.reads ?? 0)")
                        metricRow("Writes", "\(ops.writes ?? 0)")
                        metricRow("Searches", "\(ops.searches ?? 0)")
                    }
                    if let err = mem.error {
                        Text("Error: \(err)")
                            .font(.caption)
                            .foregroundColor(.red)
                    }
                }
            }
        }

        if let comp = d.compaction {
            GroupBox("Compaction") {
                VStack(alignment: .leading, spacing: 4) {
                    metricRow("Total Triggered", "\(comp.total_triggered ?? 0)")
                    metricRow("Total Failed", "\(comp.total_failed ?? 0)")
                }
            }

            if let events = comp.recent, !events.isEmpty {
                GroupBox("Recent Compactions") {
                    ForEach(Array(events.enumerated()), id: \.offset) { _, evt in
                        HStack {
                            Image(systemName: (evt.success ?? false) ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundColor((evt.success ?? false) ? .green : .red)
                                .font(.caption)
                            Text("\(evt.tokens_before ?? 0) → \(evt.tokens_after ?? 0)")
                                .font(.caption.monospaced())
                            Spacer()
                            Text(String(format: "%.1fs", evt.duration_seconds ?? 0))
                                .font(.caption)
                                .foregroundColor(.secondary)
                            Text(evt.method ?? "")
                                .font(.caption2)
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }
        }
    }

    // MARK: - Errors

    @ViewBuilder
    private func errorsSection(_ errors: [DebugError]) -> some View {
        if errors.isEmpty {
            Text("No errors recorded")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, minHeight: 100)
        } else {
            ForEach(errors.reversed()) { err in
                GroupBox {
                    errorRow(err)
                }
            }
        }
    }

    // MARK: - Reusable Components

    private func statusCard(_ title: String, _ status: String, icon: String) -> some View {
        VStack(spacing: 6) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundColor(statusColor(status))
            Text(title)
                .font(.caption)
                .fontWeight(.medium)
            Text(status)
                .font(.caption2)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(10)
        .background(statusColor(status).opacity(0.08))
        .cornerRadius(8)
    }

    private func metricRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
                .font(.caption)
                .foregroundColor(.secondary)
            Spacer()
            Text(value)
                .font(.caption.monospaced())
        }
    }

    @ViewBuilder
    private func conversationCard(id: String, c: DebugConversation) -> some View {
        if let err = c.error {
            VStack(alignment: .leading, spacing: 2) {
                Text(id).font(.caption.monospaced())
                Text(err).font(.caption2).foregroundColor(.red)
            }
        } else {
            let total = c.estimated_tokens ?? 0
            let threshold = c.compact_threshold ?? 120000
            let pct = c.percent_of_threshold ?? 0
            let headroom = c.headroom_tokens ?? max(0, threshold - total)
            let maxResp = c.max_response_tokens ?? 32768
            let turnsLeft = c.turns_until_compaction_worst_case ?? (headroom / max(1, maxResp))
            let barColor: Color = pct > 80 ? .red : pct > 50 ? .orange : .green

            VStack(alignment: .leading, spacing: 6) {
                // Header: id + totals
                HStack {
                    Text(id).font(.caption.monospaced())
                    Spacer()
                    Text("\(total.formatted()) / \(threshold.formatted()) tok")
                        .font(.caption.monospaced())
                        .foregroundColor(.secondary)
                    Text("\(Int(pct))%")
                        .font(.caption.monospaced().weight(.semibold))
                        .foregroundColor(barColor)
                }

                // Context-window progress bar
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule()
                            .fill(Color.secondary.opacity(0.15))
                        Capsule()
                            .fill(barColor)
                            .frame(width: max(2, geo.size.width * min(1, pct / 100)))
                    }
                }
                .frame(height: 6)

                // Headroom + turns-until-compaction summary
                HStack(spacing: 12) {
                    Label("\(c.messages ?? 0) msgs", systemImage: "bubble.left.and.bubble.right")
                    if let tc = c.tool_call_count {
                        Label("\(tc) tool calls", systemImage: "wrench.and.screwdriver")
                    }
                    Spacer()
                    Text("\(headroom.formatted()) tok headroom")
                        .foregroundColor(.secondary)
                }
                .font(.caption2)

                // Worst-case turns remaining — "why hasn't compaction fired?"
                if maxResp > 0 {
                    Text("≈ \(turnsLeft) more full-size turns (\(maxResp.formatted()) tok each) before compaction")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                // Role breakdown (only roles that actually have content)
                if let rb = c.role_breakdown {
                    VStack(alignment: .leading, spacing: 2) {
                        Divider().padding(.vertical, 2)
                        roleRow("system", stat: rb.system, total: total)
                        roleRow("user", stat: rb.user, total: total)
                        roleRow("assistant", stat: rb.assistant, total: total)
                        roleRow("tool", stat: rb.tool, total: total)
                    }
                }

                // Largest-single-message callout — helps spot a giant
                // blob dominating the context (e.g. a huge tool result)
                if let lm = c.largest_message_tokens, lm > 0,
                   let lr = c.largest_message_role {
                    let lmPct = Double(lm) / Double(max(1, total)) * 100
                    if lmPct > 25 {
                        HStack(spacing: 4) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .font(.caption2)
                                .foregroundColor(.orange)
                            Text("Largest single \(lr) message: \(lm.formatted()) tok (\(Int(lmPct))% of total)")
                                .font(.caption2)
                                .foregroundColor(.orange)
                        }
                    }
                }
            }
            .padding(8)
            .background(Color.secondary.opacity(0.05))
            .cornerRadius(6)
        }
    }

    @ViewBuilder
    private func roleRow(_ label: String, stat: DebugConversationRoleStat?, total: Int) -> some View {
        let tokens = stat?.tokens ?? 0
        if tokens > 0 || (stat?.messages ?? 0) > 0 {
            let pct = total > 0 ? Double(tokens) / Double(total) * 100 : 0
            HStack {
                Text(label)
                    .font(.caption2.monospaced())
                    .foregroundColor(.secondary)
                    .frame(width: 70, alignment: .leading)
                Text("\(tokens.formatted()) tok")
                    .font(.caption2.monospaced())
                Text("(\(stat?.messages ?? 0) msgs)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Spacer()
                Text(String(format: "%.0f%%", pct))
                    .font(.caption2.monospaced())
                    .foregroundColor(.secondary)
            }
        }
    }

    private func recoveryRow(_ label: String, _ count: Int) -> some View {
        HStack {
            Circle()
                .fill(count > 0 ? .orange : .green)
                .frame(width: 6, height: 6)
            Text(label)
                .font(.caption)
            Spacer()
            Text("\(count)")
                .font(.caption.monospaced())
                .foregroundColor(count > 0 ? .orange : .secondary)
        }
    }

    private func errorRow(_ err: DebugError) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Image(systemName: err.severity == "error" ? "xmark.circle.fill" :
                                  err.severity == "warning" ? "exclamationmark.triangle.fill" :
                                  "info.circle.fill")
                    .font(.caption)
                    .foregroundColor(err.severity == "error" ? .red :
                                     err.severity == "warning" ? .orange : .blue)
                Text(err.component)
                    .font(.caption)
                    .fontWeight(.semibold)
                Text(err.error_type)
                    .font(.caption2)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(Color.secondary.opacity(0.15))
                    .cornerRadius(3)
                Spacer()
                Text(formatTimestamp(err.timestamp))
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            Text(err.message)
                .font(.caption2)
                .foregroundColor(.secondary)
                .lineLimit(2)
        }
    }

    // MARK: - Helpers

    private func statusColor(_ status: String?) -> Color {
        switch status {
        case "healthy", "ok", "running": return .green
        case "degraded", "busy", "loading": return .orange
        case "error", "down": return .red
        default: return .gray
        }
    }

    private func formatDuration(_ seconds: Double) -> String {
        if seconds < 60 { return String(format: "%.0fs", seconds) }
        if seconds < 3600 { return String(format: "%.0fm %.0fs", seconds / 60, seconds.truncatingRemainder(dividingBy: 60)) }
        return String(format: "%.0fh %.0fm", seconds / 3600, (seconds / 60).truncatingRemainder(dividingBy: 60))
    }

    private func formatBytes(_ bytes: Int) -> String {
        if bytes < 1024 { return "\(bytes) B" }
        if bytes < 1024 * 1024 { return String(format: "%.1f KB", Double(bytes) / 1024) }
        return String(format: "%.1f MB", Double(bytes) / (1024 * 1024))
    }

    private func formatTimestamp(_ ts: Double) -> String {
        let date = Date(timeIntervalSince1970: ts)
        let fmt = DateFormatter()
        fmt.dateFormat = "HH:mm:ss"
        return fmt.string(from: date)
    }

    // MARK: - Data Fetching

    private func fetchData() {
        Task {
            do {
                guard let url = URL(string: "http://127.0.0.1:8801/v1/debug") else { return }
                let config = URLSessionConfiguration.ephemeral
                config.timeoutIntervalForRequest = 5
                let session = URLSession(configuration: config)
                let (rawData, _) = try await session.data(from: url)
                let decoded = try JSONDecoder().decode(DebugSnapshot.self, from: rawData)
                await MainActor.run {
                    self.data = decoded
                    self.lastFetchError = nil
                }
            } catch {
                await MainActor.run {
                    self.lastFetchError = error.localizedDescription
                }
            }
        }
    }

    private func startTimer() {
        stopTimer()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in
            fetchData()
        }
    }

    private func stopTimer() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }
}

// MARK: - Preview

#Preview {
    DebugView(agentManager: AgentManager())
        .frame(width: 600, height: 700)
}
