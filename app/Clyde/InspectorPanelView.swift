//
//  InspectorPanelView.swift
//  Clyde
//
//  Right-side inspector panel containing tiles for the asset graph,
//  node details, and future extensions. Uses frosted-glass material
//  consistent with the sidebar design.
//

import SwiftUI
import Charts

// MARK: - Inspector Panel

struct InspectorPanelView: View {
    @EnvironmentObject var viewModel: AppViewModel
    @StateObject private var panelController = GraphPanelController()
    @State private var graphHovered = false
    var onExpandToWindow: (() -> Void)?

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                // Tile 1: Thinking Toggle (quick access)
                ThinkingToggleTile()

                // Tile 2: Skills Activity (live feed)
                SkillsActivityTile()

                // Tile 3: Asset Graph (with hover-to-expand)
                graphTile

                // Tile 4: Node Details
                AssetDetailTile()

                // Tile 5: Performance — tok/s, phase, context, prefill
                PerformanceTile()

                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
        }
        // Keep expanded graph panel alive while cursor is anywhere in the inspector
        .onHover { hovering in
            if hovering && panelController.isShowing {
                panelController.cancelDismiss()
            } else if !hovering && panelController.isShowing {
                panelController.scheduleDismiss()
            }
        }
    }

    @ViewBuilder
    private var graphTile: some View {
        InspectorTile(title: "Asset Graph", icon: "point.3.connected.trianglepath.dotted") {
            if let data = viewModel.graphData, !data.nodes.isEmpty {
                AssetGraphView()
                    .aspectRatio(1.0, contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .overlay(alignment: .topTrailing) {
                        // Expand button visible on hover
                        if graphHovered {
                            Button(action: {
                                onExpandToWindow?()
                            }) {
                                Image(systemName: "arrow.up.left.and.arrow.down.right")
                                    .font(.system(size: 10, weight: .medium))
                                    .foregroundStyle(.secondary)
                                    .padding(5)
                                    .background(.ultraThinMaterial, in: Circle())
                            }
                            .buttonStyle(.plain)
                            .help("Open in full window")
                            .padding(6)
                            .transition(.opacity.animation(.easeInOut(duration: 0.15)))
                        }
                    }
                    .background(
                        GeometryReader { geo in
                            Color.clear
                                .preference(key: GraphTileFrameKey.self,
                                           value: geo.frame(in: .global))
                        }
                    )
                    .onHover { hovering in
                        withAnimation(.easeInOut(duration: 0.15)) {
                            graphHovered = hovering
                        }
                        if hovering {
                            // Cancel any pending dismiss (mouse came back to tile)
                            panelController.cancelDismiss()
                            if !panelController.isShowing {
                                // Show the floating panel after a brief delay
                                DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                                    guard self.graphHovered else { return }
                                    showExpandedPanel()
                                }
                            }
                        } else if !hovering && !panelController.isShowing {
                            // Only dismiss if the panel ISN'T showing.
                            // When the panel IS showing, the panel's own onHover
                            // manages dismissal — we ignore the tile's hover-out
                            // because it fires when the NSPanel occludes the tile.
                        }
                    }
            } else {
                // Empty state
                VStack(spacing: 8) {
                    Image(systemName: "point.3.connected.trianglepath.dotted")
                        .font(.title2)
                        .foregroundStyle(.tertiary)
                    Text("No assets yet")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                    Text("Files, URLs, and facts will appear here as the model works.")
                        .font(.caption2)
                        .foregroundStyle(.quaternary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .aspectRatio(1.0, contentMode: .fit)
            }
        }
    }

    private func showExpandedPanel() {
        // Get the screen rect of the graph tile via the window
        guard let window = NSApp.keyWindow ?? NSApp.windows.first(where: { $0.isVisible && !($0 is NSPanel) }) else { return }

        let windowFrame = window.frame
        // The graph tile is in the inspector panel (right side)
        // Estimate its screen position from the window's right portion
        let inspectorWidth: CGFloat = 300
        let tileSize: CGFloat = min(inspectorWidth - 20, 280)
        let tileScreenRect = CGRect(
            x: windowFrame.maxX - inspectorWidth + 10,
            y: windowFrame.maxY - tileSize - 80,
            width: tileSize,
            height: tileSize
        )

        let selectedBinding = Binding<String?>(
            get: { viewModel.selectedGraphNodeId },
            set: { viewModel.selectedGraphNodeId = $0 }
        )

        panelController.show(
            sourceScreenRect: tileScreenRect,
            graphData: viewModel.graphData,
            selectedNodeId: selectedBinding,
            onExpandToWindow: { onExpandToWindow?() }
        )
    }
}

/// Preference key for tracking the graph tile's frame in screen coordinates
private struct GraphTileFrameKey: PreferenceKey {
    static var defaultValue: CGRect = .zero
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        value = nextValue()
    }
}


// MARK: - Thinking Toggle Tile

struct ThinkingToggleTile: View {
    @EnvironmentObject var viewModel: AppViewModel

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: viewModel.thinkingMode ? "brain.fill" : "brain")
                .font(.system(size: 14))
                .foregroundStyle(viewModel.thinkingMode ? .purple : .secondary)
                .frame(width: 24, height: 24)

            VStack(alignment: .leading, spacing: 1) {
                Text("Thinking")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.primary)
                Text(viewModel.thinkingMode ? "Model reasons before answering" : "Direct responses only")
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer()

            if viewModel.isThinkingToggling {
                ProgressView()
                    .scaleEffect(0.5)
                    .frame(width: 20, height: 20)
            } else {
                Button {
                    Task { await viewModel.toggleThinkingMode() }
                } label: {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(viewModel.thinkingMode ? Color.purple : Color(.separatorColor).opacity(0.4))
                        .frame(width: 36, height: 20)
                        .overlay(alignment: viewModel.thinkingMode ? .trailing : .leading) {
                            Circle()
                                .fill(.white)
                                .frame(width: 16, height: 16)
                                .padding(.horizontal, 2)
                                .shadow(radius: 1)
                        }
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(viewModel.thinkingMode
                      ? Color.purple.opacity(0.06)
                      : Color(.controlBackgroundColor).opacity(0.35))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(viewModel.thinkingMode
                        ? Color.purple.opacity(0.2)
                        : Color(.separatorColor).opacity(0.15),
                        lineWidth: 0.5)
        )
        .animation(.easeInOut(duration: 0.2), value: viewModel.thinkingMode)
    }
}


// MARK: - Skills Activity Tile

struct SkillsActivityTile: View {
    @EnvironmentObject var viewModel: AppViewModel
    @State private var showTools = false

    var body: some View {
        InspectorTile(title: "Active Skill", icon: "sparkles") {
            if let info = viewModel.activeSkillInfo {
                VStack(alignment: .leading, spacing: 8) {
                    // Skill badge: icon + label + auto-route indicator
                    HStack(spacing: 8) {
                        Image(systemName: info.skill.icon)
                            .font(.title3)
                            .foregroundStyle(colorForSkill(info.skill.name))
                            .frame(width: 28, height: 28)
                            .background(
                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    .fill(colorForSkill(info.skill.name).opacity(0.12))
                            )

                        VStack(alignment: .leading, spacing: 1) {
                            Text(info.skill.label)
                                .font(.callout.weight(.semibold))
                            Text(info.skill.description)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }

                    // Status pills
                    HStack(spacing: 6) {
                        if info.autoRoute {
                            pillBadge("Auto", icon: "arrow.triangle.swap", color: .blue)
                        }
                        if info.userLocked {
                            pillBadge("Locked", icon: "lock.fill", color: .orange)
                        }
                        if info.skill.enableThinking {
                            pillBadge("Thinking", icon: "brain", color: .purple)
                        }
                        if info.skill.enforcesResearchBudget {
                            pillBadge("Budget", icon: "gauge.with.dots.needle.33percent", color: .red)
                        }
                    }

                    // Tool list (expandable)
                    if !info.skill.tools.isEmpty {
                        Button {
                            withAnimation(.easeInOut(duration: 0.2)) {
                                showTools.toggle()
                            }
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: showTools ? "chevron.down" : "chevron.right")
                                    .imageScale(.small)
                                    .frame(width: 10)
                                Text("\(info.skill.tools.count) tools")
                                    .font(.caption)
                                Spacer()
                            }
                            .foregroundStyle(.secondary)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)

                        if showTools {
                            toolGrid(info.skill.tools)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                }
            } else {
                // Empty state
                VStack(spacing: 6) {
                    Image(systemName: "sparkles")
                        .font(.title2)
                        .foregroundStyle(.tertiary)
                    Text("No active skill")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                    Text("Skills activate automatically based on your conversation.")
                        .font(.caption2)
                        .foregroundStyle(.quaternary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
            }
        }
    }

    @ViewBuilder
    private func pillBadge(_ label: String, icon: String, color: Color) -> some View {
        HStack(spacing: 3) {
            Image(systemName: icon)
                .imageScale(.small)
            Text(label)
        }
        .font(.system(size: 9, weight: .medium))
        .foregroundStyle(color)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(
            Capsule().fill(color.opacity(0.1))
        )
    }

    @ViewBuilder
    private func toolGrid(_ tools: [String]) -> some View {
        let columns = [GridItem(.adaptive(minimum: 70), spacing: 4)]
        LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
            ForEach(tools, id: \.self) { tool in
                Text(tool)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .fill(Color(.controlBackgroundColor).opacity(0.5))
                    )
            }
        }
    }

    private func colorForSkill(_ name: String) -> Color {
        switch name {
        case "general":        return .blue
        case "quick_research": return .cyan
        case "research":       return .indigo
        case "code":           return .green
        case "memory":         return .purple
        default:               return .secondary
        }
    }
}


// MARK: - Inspector Tile Wrapper

/// Reusable container for inspector panel tiles. Uses semantic system
/// colors that match native macOS 26 conventions.
struct InspectorTile<Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: icon)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)

            content()
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color(.controlBackgroundColor).opacity(0.35))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Color(.separatorColor).opacity(0.15), lineWidth: 0.5)
        )
    }
}


// MARK: - Performance Tile

/// Live generation dashboard — tok/s curve, phase, prefill latency,
/// token counts, thinking/content split. Updates from
/// `AgentManager.liveMetrics` + `metricsHistory`. Sparkline is a
/// Catmull-Rom-smoothed cubic Bézier so the line stays curved rather
/// than sharp-kinked between samples (matches the macOS aesthetic of
/// the Battery / Energy sparklines in System Settings).
struct PerformanceTile: View {
    @EnvironmentObject var agentManager: AgentManager

    private var isActive: Bool { agentManager.liveMetrics != nil }

    var body: some View {
        InspectorTile(title: "Performance", icon: "waveform.path.ecg") {
            VStack(alignment: .leading, spacing: 10) {
                headerRow
                sparkline
                statsGrid
                contextPressureSection
            }
            .animation(.easeInOut(duration: 0.25), value: isActive)
        }
    }

    // MARK: Header — phase chip + current tok/s

    @ViewBuilder
    private var headerRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            phaseChip
            Spacer(minLength: 6)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(formattedTPS)
                    .font(.system(size: 22, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(isActive ? .primary : .secondary)
                Text("tok/s")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var phaseChip: some View {
        let label: String = {
            guard let m = agentManager.liveMetrics else { return "Idle" }
            switch m.phase {
            case "thinking": return "Thinking"
            case "content":  return "Writing"
            case "tool":     return "Tool"
            default:         return "Generating"
            }
        }()
        let color: Color = {
            guard let m = agentManager.liveMetrics else { return .secondary }
            switch m.phase {
            case "thinking": return .purple
            case "content":  return .blue
            case "tool":     return .orange
            default:         return .gray
            }
        }()
        HStack(spacing: 5) {
            if isActive {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
                    .opacity(0.9)
                    .modifier(PulseAnimation())
            } else {
                Circle()
                    .fill(Color.secondary.opacity(0.4))
                    .frame(width: 6, height: 6)
            }
            Text(label)
                .font(.caption.weight(.medium))
                .foregroundStyle(isActive ? color : .secondary)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(
            Capsule().fill(color.opacity(isActive ? 0.1 : 0.0))
        )
        .overlay(
            Capsule().stroke(color.opacity(isActive ? 0.22 : 0.0), lineWidth: 0.5)
        )
    }

    // MARK: Sparkline — Charts-driven, organic point arrival

    @ViewBuilder
    private var sparkline: some View {
        let samples = agentManager.metricsHistory.map { $0.tps }
        let peak = max(agentManager.peakTokensPerSecond, samples.max() ?? 1, 1)
        ZStack(alignment: .topTrailing) {
            if samples.isEmpty {
                Text("No activity yet")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            } else {
                Chart {
                    ForEach(Array(samples.enumerated()), id: \.offset) { idx, value in
                        AreaMark(
                            x: .value("t", idx),
                            y: .value("tok/s", value)
                        )
                        .interpolationMethod(.catmullRom)
                        .foregroundStyle(LinearGradient(
                            colors: [
                                Color.accentColor.opacity(0.35),
                                Color.accentColor.opacity(0.02),
                            ],
                            startPoint: .top,
                            endPoint: .bottom
                        ))
                        LineMark(
                            x: .value("t", idx),
                            y: .value("tok/s", value)
                        )
                        .interpolationMethod(.catmullRom)
                        .foregroundStyle(Color.accentColor)
                        .lineStyle(StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                    }
                    if let last = samples.last {
                        PointMark(
                            x: .value("t", samples.count - 1),
                            y: .value("tok/s", last)
                        )
                        .foregroundStyle(Color.accentColor)
                        .symbolSize(36)
                    }
                }
                .chartXAxis(.hidden)
                .chartYAxis(.hidden)
                .chartXScale(domain: 0...max(samples.count - 1, 1))
                .chartYScale(domain: 0...(peak * 1.15))
                // Charts framework tweens between values when the source
                // array changes; this is what gives the line its organic
                // arrival feel. The animation key on samples.count drives
                // the "graph populating" expansion as samples accumulate.
                .animation(.easeOut(duration: 0.45), value: samples.count)
                .animation(.easeOut(duration: 0.45), value: samples.last ?? 0)
            }

            if peak > 0 && !samples.isEmpty {
                Text("peak \(peak, specifier: "%.1f")")
                    .font(.system(size: 9, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(
                        Capsule().fill(Color(.controlBackgroundColor).opacity(0.6))
                    )
                    .padding(4)
            }
        }
        .frame(height: 54)
        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color(.controlBackgroundColor).opacity(0.3))
        )
    }

    /// Catmull-Rom spline through `points`, converted to cubic Bézier so
    /// CoreGraphics can render it as one smooth Path. Tension τ=0.5
    /// matches the "natural" look Apple uses in the Battery graph.
    static func catmullRomPath(points: [CGPoint], tension: CGFloat = 0.5) -> Path {
        var path = Path()
        guard let first = points.first else { return path }
        path.move(to: first)
        if points.count < 2 { return path }
        for i in 0..<points.count - 1 {
            let p0 = points[max(0, i - 1)]
            let p1 = points[i]
            let p2 = points[i + 1]
            let p3 = points[min(points.count - 1, i + 2)]
            let c1 = CGPoint(
                x: p1.x + (p2.x - p0.x) * tension / 3.0,
                y: p1.y + (p2.y - p0.y) * tension / 3.0
            )
            let c2 = CGPoint(
                x: p2.x - (p3.x - p1.x) * tension / 3.0,
                y: p2.y - (p3.y - p1.y) * tension / 3.0
            )
            path.addCurve(to: p2, control1: c1, control2: c2)
        }
        return path
    }

    // MARK: Stats grid

    @ViewBuilder
    private var statsGrid: some View {
        let m = agentManager.liveMetrics
        let thinkingTok = (m?.thinkingChars ?? 0) / 4
        let contentTok = (m?.contentChars ?? 0) / 4
        // Live turn uses `m.tokens`; between turns we show the last turn
        // the user actually watched so the cell is never blank.
        let isStreaming = m != nil
        let totalTok = isStreaming
            ? (m?.tokens ?? (thinkingTok + contentTok))
            : agentManager.tokensLastTurn
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                statCell(
                    label: "Tokens",
                    value: "\(totalTok)",
                    sublabel: isStreaming ? "this turn" : "last turn"
                )
                statCell(
                    label: "Elapsed",
                    value: formatSeconds(m?.elapsed ?? 0),
                    sublabel: "since first tok"
                )
            }
            HStack(spacing: 8) {
                statCell(
                    label: "Prefill",
                    value: formatSeconds(agentManager.lastPrefillSeconds ?? 0),
                    sublabel: "time-to-first"
                )
                statCell(
                    label: "Peak",
                    value: String(format: "%.1f", agentManager.peakTokensPerSecond),
                    sublabel: "tok/s"
                )
            }
            // Stack-wide system stats (CPU + RAM + Cores) for the Clyde
            // + agent + llama-server processes combined. Sampled by
            // SystemStatsMonitor every 1s via full `ps -A` scan + comm
            // pattern match — picks up the agent and llama-server even
            // when AgentManager doesn't currently have their PIDs.
            HStack(spacing: 8) {
                statCell(
                    label: "CPU",
                    value: String(format: "%.0f%%", agentManager.systemStats.totalCPUPercent),
                    sublabel: "of all cores"
                )
                ramCell(mb: agentManager.systemStats.ramMB)
                coresCell(
                    used: agentManager.systemStats.coresUsed,
                    total: agentManager.systemStats.coreCount
                )
            }
            .animation(.easeOut(duration: 0.5), value: agentManager.systemStats.totalCPUPercent)
            .animation(.easeOut(duration: 0.5), value: agentManager.systemStats.ramMB)
            .animation(.easeOut(duration: 0.5), value: agentManager.systemStats.coresUsed)
            if thinkingTok > 0 || contentTok > 0 {
                thinkingContentSplit(thinking: thinkingTok, content: contentTok)
            }
        }
    }

    /// RAM cell — number stays large, unit renders in a smaller font on
    /// the same baseline so "322 MB" or "12.4 GB" reads cleanly without
    /// the unit dominating the cell.
    @ViewBuilder
    private func ramCell(mb: Double) -> some View {
        let (numText, unitText): (String, String) = {
            if mb <= 0 { return ("—", "") }
            if mb < 1024 { return (String(format: "%.0f", mb), "MB") }
            return (String(format: "%.1f", mb / 1024.0), "GB")
        }()
        VStack(alignment: .leading, spacing: 1) {
            Text("RAM")
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(numText)
                    .font(.system(size: 15, weight: .medium, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                Text(unitText)
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            Text("rss")
                .font(.system(size: 9))
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color(.controlBackgroundColor).opacity(0.3))
        )
    }

    /// Cores cell — replaces the old GPU active/idle tile. `used / total`
    /// e.g. "1.9 / 16" tells you how many logical cores the stack is
    /// actually busying. Replaces the GPU bool which was a poor proxy.
    @ViewBuilder
    private func coresCell(used: Double, total: Int) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("Cores")
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(String(format: "%.1f", used))
                    .font(.system(size: 15, weight: .medium, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                Text("/ \(total)")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            Text("cores busy")
                .font(.system(size: 9))
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color(.controlBackgroundColor).opacity(0.3))
        )
    }

    @ViewBuilder
    private func statCell(label: String, value: String, sublabel: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.system(size: 9, weight: .medium))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
            Text(value)
                .font(.system(size: 15, weight: .medium, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(.primary)
            Text(sublabel)
                .font(.system(size: 9))
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color(.controlBackgroundColor).opacity(0.3))
        )
    }

    @ViewBuilder
    private func thinkingContentSplit(thinking: Int, content: Int) -> some View {
        let total = max(thinking + content, 1)
        let thinkingFrac = Double(thinking) / Double(total)
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Thinking / Writing")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)
                Spacer()
                Text("\(thinking)")
                    .font(.system(size: 10, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.purple)
                Text("/")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
                Text("\(content)")
                    .font(.system(size: 10, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.blue)
            }
            GeometryReader { geo in
                let w = geo.size.width
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(Color.blue.opacity(0.35))
                        .frame(height: 4)
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(Color.purple)
                        .frame(width: w * thinkingFrac, height: 4)
                        // Glide the purple bar between thinking/writing
                        // ratios instead of snapping. Matches the
                        // sparkline's organic feel.
                        .animation(.easeOut(duration: 0.5), value: thinkingFrac)
                }
            }
            .frame(height: 4)
        }
    }

    // MARK: Formatters

    private var formattedTPS: String {
        guard let m = agentManager.liveMetrics else { return "—" }
        return String(format: "%.1f", m.tps)
    }

    private func formatSeconds(_ s: Double) -> String {
        if s <= 0 { return "—" }
        if s < 10 { return String(format: "%.1fs", s) }
        return String(format: "%.0fs", s)
    }

    // MARK: Context pressure section

    /// Smooth 4-stop gradient for the context pressure line + chip.
    /// Green (0–55%) → yellow (70%) → orange (85%) → red (100%).
    /// Interpolated in RGB so the transitions feel continuous.
    private static func pressureColor(_ fraction: Double) -> Color {
        let f = min(1.0, max(0.0, fraction))
        // Stop table: fraction → (r, g, b)
        let stops: [(Double, Double, Double, Double)] = [
            (0.00, 0.30, 0.80, 0.40),   // green
            (0.55, 0.45, 0.82, 0.35),   // still green, slightly warmer
            (0.70, 0.96, 0.78, 0.16),   // yellow
            (0.85, 0.98, 0.56, 0.13),   // orange
            (1.00, 0.93, 0.26, 0.20),   // red
        ]
        for i in 0..<stops.count - 1 {
            let (a, ar, ag, ab) = stops[i]
            let (b, br, bg, bb) = stops[i + 1]
            if f <= b {
                let span = (b - a)
                let t = span > 0 ? (f - a) / span : 0
                return Color(
                    red: ar + (br - ar) * t,
                    green: ag + (bg - ag) * t,
                    blue: ab + (bb - ab) * t
                )
            }
        }
        return Color(red: 0.93, green: 0.26, blue: 0.20)
    }

    @ViewBuilder
    private var contextPressureSection: some View {
        let cp = agentManager.contextPressure
        let history = agentManager.contextHistory
        let threshold = cp?.threshold ?? 0
        let tokens = cp?.tokens ?? 0
        let fraction = cp?.fraction ?? 0
        let color = Self.pressureColor(fraction)

        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Context Pressure")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)
                Spacer()
                Text(pressureLabel(fraction))
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(color)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(color.opacity(0.15)))
            }

            contextGraph(history: history, threshold: threshold, color: color)

            HStack {
                Text(formatTokens(tokens))
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                Text("/")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                Text(threshold > 0 ? formatTokens(threshold) : "—")
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                Spacer()
                if let msgCount = cp?.messages, msgCount > 0 {
                    Text("\(msgCount) msgs")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
    }

    private func pressureLabel(_ f: Double) -> String {
        switch f {
        case ..<0.55: return "Healthy"
        case ..<0.70: return "Warming"
        case ..<0.85: return "Compacting soon"
        case ..<1.0:  return "Near limit"
        default:      return "Compacting now"
        }
    }

    private func formatTokens(_ n: Int) -> String {
        if n >= 1_000_000 {
            return String(format: "%.1fM", Double(n) / 1_000_000)
        }
        if n >= 1_000 {
            return String(format: "%.1fK", Double(n) / 1_000)
        }
        return "\(n)"
    }

    @ViewBuilder
    private func contextGraph(history: [ContextPressure], threshold: Int, color: Color) -> some View {
        let samples = history.map { Double($0.tokens) }
        let maxY = max(Double(threshold), samples.max() ?? 1, 1) * 1.05
        ZStack(alignment: .topTrailing) {
            if samples.isEmpty {
                Text("No turn yet")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            } else {
                Chart {
                    ForEach(Array(samples.enumerated()), id: \.offset) { idx, value in
                        AreaMark(
                            x: .value("t", idx),
                            y: .value("tokens", value)
                        )
                        .interpolationMethod(.catmullRom)
                        .foregroundStyle(LinearGradient(
                            colors: [color.opacity(0.40), color.opacity(0.02)],
                            startPoint: .top,
                            endPoint: .bottom
                        ))
                        LineMark(
                            x: .value("t", idx),
                            y: .value("tokens", value)
                        )
                        .interpolationMethod(.catmullRom)
                        .foregroundStyle(color)
                        .lineStyle(StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                    }
                    if threshold > 0 {
                        RuleMark(y: .value("threshold", Double(threshold)))
                            .foregroundStyle(Color.red.opacity(0.35))
                            .lineStyle(StrokeStyle(lineWidth: 0.7, dash: [3, 3]))
                    }
                    if let last = samples.last {
                        PointMark(
                            x: .value("t", samples.count - 1),
                            y: .value("tokens", last)
                        )
                        .foregroundStyle(color)
                        .symbolSize(30)
                    }
                }
                .chartXAxis(.hidden)
                .chartYAxis(.hidden)
                .chartXScale(domain: 0...max(samples.count - 1, 1))
                .chartYScale(domain: 0...maxY)
                // Charts handles smooth point arrival animation; the
                // animation key on samples.last lets the curve glide
                // when the most recent sample's value changes too.
                .animation(.easeOut(duration: 0.5), value: samples.count)
                .animation(.easeOut(duration: 0.5), value: samples.last ?? 0)
            }
        }
        .frame(height: 44)
        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color(.controlBackgroundColor).opacity(0.3))
        )
    }
}

/// Pulse animation for the active-phase dot.
private struct PulseAnimation: ViewModifier {
    @State private var animate = false
    func body(content: Content) -> some View {
        content
            .scaleEffect(animate ? 1.25 : 1.0)
            .opacity(animate ? 0.55 : 1.0)
            .animation(
                .easeInOut(duration: 0.9).repeatForever(autoreverses: true),
                value: animate
            )
            .onAppear { animate = true }
    }
}


// MARK: - Legend

struct GraphLegend: View {
    var body: some View {
        HStack(spacing: 10) {
            legendItem(color: GraphColors.file, label: "File")
            legendItem(color: GraphColors.url, label: "URL")
            legendItem(color: GraphColors.query, label: "Query")
            legendItem(color: GraphColors.fact, label: "Fact")
        }
        .font(.system(size: 9))
        .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private func legendItem(color: Color, label: String) -> some View {
        HStack(spacing: 3) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(label)
        }
    }
}


#Preview {
    InspectorPanelView()
        .frame(width: 300, height: 600)
        .environmentObject(AppViewModel())
}
