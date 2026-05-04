//
//  AssetDetailView.swift
//  Clyde
//
//  Detail tile in the inspector panel showing information about
//  a selected asset node and its connections.
//

import SwiftUI

struct AssetDetailTile: View {
    @EnvironmentObject var viewModel: AppViewModel

    var body: some View {
        InspectorTile(title: "Details", icon: "info.circle") {
            if let nodeId = viewModel.selectedGraphNodeId,
               let data = viewModel.graphData,
               let node = data.nodes.first(where: { $0.id == nodeId }) {
                nodeDetail(node: node, data: data)
            } else {
                emptyState
            }
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        VStack(spacing: 4) {
            Text("Click a node to see details")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private func nodeDetail(node: GraphNode, data: GraphData) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            // Type badge + title
            HStack(spacing: 6) {
                Image(systemName: iconForType(node.type))
                    .font(.callout)
                    .foregroundStyle(GraphColors.forType(node.type))
                Text(node.title)
                    .font(.callout.weight(.medium))
                    .lineLimit(2)
            }

            // Body (path / URL / fact text)
            if !node.body.isEmpty {
                let displayBody = shortenPath(node.body)
                Text(displayBody)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(4)
                    .textSelection(.enabled)
            }

            // Status + source tool
            HStack {
                Text(node.status.capitalized)
                    .font(.caption2.weight(.medium))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(GraphColors.forType(node.type).opacity(0.15))
                    .clipShape(Capsule())

                Spacer()

                Text(node.sourceTool.replacingOccurrences(of: "_", with: " "))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            // ── Organize progress section ──
            // If this node IS a task node, or is a file linked to a task node, show progress
            if let progressInfo = organizeProgress(for: node, in: data) {
                Divider().padding(.vertical, 2)
                organizeProgressView(progressInfo)
            }

            // ── Category detail (for organize_category nodes) ──
            if let catMeta = node.metadata,
               catMeta["node_role"] == "organize_category" {
                Divider().padding(.vertical, 2)
                categoryDetailView(node: node, data: data)
            }

            // Connections
            let connections = data.edges.filter { $0.fromId == node.id || $0.toId == node.id }
            if !connections.isEmpty {
                Divider()
                    .padding(.vertical, 2)

                Text("Connections (\(connections.count))")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)

                ForEach(connections) { edge in
                    let otherId = edge.fromId == node.id ? edge.toId : edge.fromId
                    let otherNode = data.nodes.first { $0.id == otherId }
                    let direction = edge.fromId == node.id ? "→" : "←"

                    HStack(spacing: 4) {
                        Text(direction)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)

                        if let other = otherNode {
                            Image(systemName: iconForType(other.type))
                                .font(.caption2)
                                .foregroundStyle(GraphColors.forType(other.type))
                            Text(other.title)
                                .font(.caption2)
                                .lineLimit(1)
                        } else {
                            Text(otherId)
                                .font(.caption2.monospaced())
                                .foregroundStyle(.tertiary)
                        }

                        Spacer()

                        Text(edge.kind.replacingOccurrences(of: "_", with: " "))
                            .font(.system(size: 9))
                            .foregroundStyle(.tertiary)
                    }
                    .contentShape(Rectangle())
                    .onTapGesture {
                        viewModel.selectedGraphNodeId = otherId
                    }
                }
            }
        }
    }

    // MARK: - Organize Progress

    /// Progress info extracted from an organize_task node
    private struct OrganizeProgressInfo {
        let phase: String
        let classified: Int
        let total: Int
        let uncertain: Int
        let categoryCount: Int
        let stateDir: String
        let taskTitle: String
    }

    /// Find organize progress: either the node IS a task node, or it's a file connected to one
    private func organizeProgress(for node: GraphNode, in data: GraphData) -> OrganizeProgressInfo? {
        // Case 1: This node IS the organize task
        if let meta = node.metadata, meta["node_role"] == "organize_task" {
            return extractProgress(from: node, in: data)
        }

        // Case 2: This is a file node linked to a task via source_file edge
        if node.type == "file" {
            // Find any task node connected via source_file
            for edge in data.edges where edge.kind == "source_file" {
                // source_file: task → file, so edge.toId == this file
                if edge.toId == node.id {
                    if let taskNode = data.nodes.first(where: { $0.id == edge.fromId }),
                       taskNode.metadata?["node_role"] == "organize_task" {
                        return extractProgress(from: taskNode, in: data)
                    }
                }
            }
        }

        // Case 3: This is a category node — show the parent task's progress
        if let meta = node.metadata, meta["node_role"] == "organize_category" {
            for edge in data.edges where edge.kind == "has_category" && edge.toId == node.id {
                if let taskNode = data.nodes.first(where: { $0.id == edge.fromId }),
                   taskNode.metadata?["node_role"] == "organize_task" {
                    return extractProgress(from: taskNode, in: data)
                }
            }
        }

        return nil
    }

    private func extractProgress(from taskNode: GraphNode, in data: GraphData) -> OrganizeProgressInfo? {
        guard let meta = taskNode.metadata else { return nil }
        let classified = Int(meta["classified"] ?? "0") ?? 0
        let total = Int(meta["total_items"] ?? "0") ?? 0
        let uncertain = Int(meta["uncertain"] ?? "0") ?? 0
        let catNodes = data.nodes.filter { $0.metadata?["node_role"] == "organize_category" }
        return OrganizeProgressInfo(
            phase: meta["phase"] ?? "unknown",
            classified: classified,
            total: total,
            uncertain: uncertain,
            categoryCount: catNodes.count,
            stateDir: meta["state_dir"] ?? "",
            taskTitle: taskNode.title
        )
    }

    @ViewBuilder
    private func organizeProgressView(_ info: OrganizeProgressInfo) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            // Header
            HStack(spacing: 6) {
                Image(systemName: "folder.badge.gearshape")
                    .font(.caption)
                    .foregroundStyle(.orange)
                Text("Organization Progress")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.primary)
            }

            // Progress bar
            let progress = info.total > 0 ? Double(info.classified) / Double(info.total) : 0
            VStack(alignment: .leading, spacing: 3) {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(Color(.separatorColor).opacity(0.2))
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(progressColor(for: info.phase))
                            .frame(width: geo.size.width * progress)
                    }
                }
                .frame(height: 6)

                HStack {
                    Text("\(info.classified)/\(info.total)")
                        .font(.caption2.weight(.medium).monospacedDigit())
                    Spacer()
                    Text("\(Int(progress * 100))%")
                        .font(.caption2.weight(.medium).monospacedDigit())
                        .foregroundStyle(progressColor(for: info.phase))
                }
                .foregroundStyle(.secondary)
            }

            // Phase + stats
            HStack(spacing: 8) {
                phasePill(info.phase)

                if info.categoryCount > 0 {
                    HStack(spacing: 3) {
                        Image(systemName: "folder.fill")
                            .imageScale(.small)
                        Text("\(info.categoryCount)")
                    }
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.blue)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.blue.opacity(0.1)))
                }

                if info.uncertain > 0 {
                    HStack(spacing: 3) {
                        Image(systemName: "questionmark.circle")
                            .imageScale(.small)
                        Text("\(info.uncertain)")
                    }
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.orange.opacity(0.1)))
                }
            }
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color.orange.opacity(0.04))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .stroke(Color.orange.opacity(0.15), lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private func categoryDetailView(node: GraphNode, data: GraphData) -> some View {
        let meta = node.metadata ?? [:]
        let itemCount = Int(meta["item_count"] ?? "0") ?? 0

        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "folder.fill")
                    .font(.caption)
                    .foregroundStyle(.blue)
                Text("Category Details")
                    .font(.caption.weight(.semibold))
            }

            HStack {
                Text("\(itemCount) items")
                    .font(.caption2.weight(.medium))
                Spacer()
                Text(meta["confidence"] ?? "")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(Color.blue.opacity(0.04))
        )
    }

    @ViewBuilder
    private func phasePill(_ phase: String) -> some View {
        let (icon, color) = phaseDisplay(phase)
        HStack(spacing: 3) {
            Image(systemName: icon)
                .imageScale(.small)
            Text(phase.capitalized)
        }
        .font(.system(size: 9, weight: .medium))
        .foregroundStyle(color)
        .padding(.horizontal, 5)
        .padding(.vertical, 2)
        .background(Capsule().fill(color.opacity(0.1)))
    }

    private func phaseDisplay(_ phase: String) -> (String, Color) {
        switch phase {
        case "ingest":   return ("arrow.down.doc", .gray)
        case "taxonomy": return ("list.bullet.indent", .purple)
        case "classify": return ("tag", .blue)
        case "review":   return ("eye", .orange)
        case "execute":  return ("checkmark.circle", .green)
        case "done":     return ("checkmark.seal.fill", .green)
        default:         return ("questionmark", .secondary)
        }
    }

    private func progressColor(for phase: String) -> Color {
        switch phase {
        case "done", "execute": return .green
        case "review": return .orange
        default: return .blue
        }
    }

    private func iconForType(_ type: String) -> String {
        switch type {
        case "file": return "doc.fill"
        case "url": return "globe"
        case "query": return "magnifyingglass"
        case "fact": return "lightbulb.fill"
        default: return "circle"
        }
    }

    private func shortenPath(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path.hasPrefix(home) {
            return "~" + String(path.dropFirst(home.count))
        }
        return path
    }
}


#Preview {
    AssetDetailTile()
        .frame(width: 300)
        .environmentObject(AppViewModel())
}
