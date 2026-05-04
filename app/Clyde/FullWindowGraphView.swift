//
//  FullWindowGraphView.swift
//  Clyde
//
//  Full-window asset graph view. Opened from the expand button in
//  the inspector's graph tile or the hover-expand floating panel.
//  Premium visual design with pinch-to-zoom, connection highlighting,
//  and directional pulse animations.
//

import SwiftUI

struct FullWindowGraphView: View {
    @EnvironmentObject var agentManager: AgentManager
    @EnvironmentObject var viewModel: AppViewModel
    @StateObject private var simulation = GraphSimulation()
    @State private var hoveredNodeId: String?
    // Zoom state
    @State private var magnification: CGFloat = 1.0
    @State private var steadyZoom: CGFloat = 1.0
    // Pan state
    @State private var panOffset: CGSize = .zero
    @State private var steadyPan: CGSize = .zero

    var body: some View {
        ZStack {
            Color(.windowBackgroundColor)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                // Toolbar area
                HStack {
                    Label("Asset Graph", systemImage: "point.3.connected.trianglepath.dotted")
                        .font(.headline)
                        .foregroundStyle(.secondary)

                    Spacer()

                    if let data = viewModel.graphData {
                        Text("\(data.nodes.count) nodes · \(data.edges.count) edges")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }

                    // Zoom indicator
                    let currentZoom = steadyZoom * magnification
                    if abs(currentZoom - 1.0) > 0.05 {
                        Text("\(Int(currentZoom * 100))%")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.tertiary)
                            .padding(.leading, 8)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)

                Divider()

                // Graph canvas
                if let data = viewModel.graphData, !data.nodes.isEmpty {
                    let currentZoom = steadyZoom * magnification
                    let currentPanX = steadyPan.width + panOffset.width
                    let currentPanY = steadyPan.height + panOffset.height
                    GeometryReader { geo in
                        let bounds = geo.size
                        TimelineView(.periodic(from: .now, by: (simulation.isSettled && hoveredNodeId == nil) ? 1.0 : 1.0/30.0)) { _ in
                            Canvas { context, size in
                                // Apply pan + zoom transform
                                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                                context.translateBy(x: center.x + currentPanX, y: center.y + currentPanY)
                                context.scaleBy(x: currentZoom, y: currentZoom)
                                context.translateBy(x: -center.x, y: -center.y)

                                let hoverInfo = buildHoverInfo()
                                drawEdges(context: &context, size: size, hoverInfo: hoverInfo, zoom: currentZoom)
                                drawNodes(context: &context, size: size, hoverInfo: hoverInfo, zoom: currentZoom)
                            }
                            .onContinuousHover { phase in
                                switch phase {
                                case .active(let location):
                                    hoveredNodeId = hitTest(at: location, zoom: currentZoom, panX: currentPanX, panY: currentPanY, size: bounds)
                                case .ended:
                                    hoveredNodeId = nil
                                @unknown default:
                                    break
                                }
                            }
                            // Two-finger scroll → pan, pinch → zoom (via NSEvent monitors)
                            .overlay(
                                GraphInputCapture(
                                    onScroll: { dx, dy in
                                        steadyPan.width += dx
                                        steadyPan.height += dy
                                    },
                                    onMagnify: { delta in
                                        magnification = 1.0 + delta
                                    },
                                    onMagnifyEnd: { delta in
                                        steadyZoom = max(0.3, min(5.0, steadyZoom * (1.0 + delta)))
                                        magnification = 1.0
                                    }
                                )
                            )
                            // Click+drag to pan
                            .simultaneousGesture(
                                DragGesture(minimumDistance: 3)
                                    .onChanged { value in
                                        panOffset = value.translation
                                    }
                                    .onEnded { value in
                                        steadyPan.width += value.translation.width
                                        steadyPan.height += value.translation.height
                                        panOffset = .zero
                                    }
                            )
                            // Tap to select node
                            .simultaneousGesture(
                                SpatialTapGesture()
                                    .onEnded { value in
                                        selectNode(at: value.location, zoom: currentZoom, panX: currentPanX, panY: currentPanY, size: bounds)
                                    }
                            )
                        }
                        .onChange(of: viewModel.graphData) { _, newData in
                            simulation.updateFromData(newData, bounds: bounds)
                        }
                        .onChange(of: bounds) { _, newBounds in
                            simulation.currentBounds = newBounds
                        }
                        .onAppear {
                            simulation.currentBounds = bounds
                            simulation.updateFromData(viewModel.graphData, bounds: bounds)
                        }
                        .task {
                            while !Task.isCancelled {
                                if !simulation.isSettled || hoveredNodeId != nil {
                                    simulation.step()
                                }
                                try? await Task.sleep(for: .milliseconds(33))
                            }
                        }
                    }
                } else {
                    VStack(spacing: 12) {
                        Image(systemName: "point.3.connected.trianglepath.dotted")
                            .font(.system(size: 48))
                            .foregroundStyle(.tertiary)
                        Text("No assets in this conversation yet")
                            .font(.title3)
                            .foregroundStyle(.tertiary)
                        Text("Start a task with web search or file operations to see the graph populate.")
                            .font(.caption)
                            .foregroundStyle(.quaternary)
                            .multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }

                // Bottom bar with legend and detail
                VStack(spacing: 8) {
                    Divider()

                    HStack(alignment: .top) {
                        GraphLegend()
                            .padding(.leading, 16)

                        Spacer()

                        // Selected node info
                        if let nodeId = viewModel.selectedGraphNodeId,
                           let data = viewModel.graphData,
                           let node = data.nodes.first(where: { $0.id == nodeId }) {
                            HStack(spacing: 6) {
                                Image(systemName: iconForType(node.type))
                                    .foregroundStyle(GraphColors.forType(node.type))
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(node.title)
                                        .font(.caption.weight(.medium))
                                        .lineLimit(1)
                                    if !node.body.isEmpty {
                                        Text(node.body)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                    }
                                }
                            }
                            .padding(.trailing, 16)
                        }
                    }
                    .padding(.vertical, 8)
                }
            }
        }
        .onAppear {
            // Ensure polling is active for the current conversation
            if let conv = viewModel.selectedConversation {
                viewModel.startGraphPolling(for: conv.id)
            }
        }
    }

    // MARK: - Hover Info

    private func buildHoverInfo() -> GraphHoverInfo {
        guard let hId = hoveredNodeId else {
            return GraphHoverInfo(hoveredId: nil)
        }
        var info = GraphHoverInfo(hoveredId: hId)
        for edge in simulation.edgeStates {
            if edge.fromId == hId {
                info.forwardEdgeKeys.insert(edge.id)
                info.forwardNodeIds.insert(edge.toId)
            }
            if edge.toId == hId {
                info.backwardEdgeKeys.insert(edge.id)
                info.backwardNodeIds.insert(edge.fromId)
            }
        }
        return info
    }

    // MARK: - Drawing (delegates to shared GraphDrawing)

    private func drawEdges(context: inout GraphicsContext, size: CGSize, hoverInfo: GraphHoverInfo, zoom: CGFloat) {
        GraphDrawing.drawEdges(
            context: &context, size: size, hoverInfo: hoverInfo, zoom: zoom,
            edgeStates: simulation.edgeStates, nodeStates: simulation.nodeStates,
            pulsePhase: simulation.pulsePhase
        )
    }

    private func drawNodes(context: inout GraphicsContext, size: CGSize, hoverInfo: GraphHoverInfo, zoom: CGFloat) {
        GraphDrawing.drawNodes(
            context: &context, size: size, hoverInfo: hoverInfo, zoom: zoom,
            nodeStates: simulation.nodeStates, pulsePhase: simulation.pulsePhase,
            selectedNodeId: viewModel.selectedGraphNodeId,
            hoveredNodeId: hoveredNodeId,
            nodeCount: simulation.nodeStates.count,
            titleForId: { id in viewModel.graphData?.nodes.first { $0.id == id }?.title ?? "" },
            baseNodeRadius: 8
        )
    }

    // MARK: - Hit Testing

    private func hitTest(at point: CGPoint, zoom: CGFloat, panX: CGFloat, panY: CGFloat, size: CGSize) -> String? {
        // Reverse the pan+zoom transform to get graph coordinates
        let center = CGPoint(x: size.width / 2, y: size.height / 2)
        let graphPoint = CGPoint(
            x: (point.x - center.x - panX) / zoom + center.x,
            y: (point.y - center.y - panY) / zoom + center.y
        )
        let threshold: CGFloat = 25 / zoom
        var closestId: String?
        var closestDist: CGFloat = .infinity

        for node in simulation.nodeStates {
            let dx = node.position.x - graphPoint.x
            let dy = node.position.y - graphPoint.y
            let dist = sqrt(dx*dx + dy*dy)
            if dist < threshold && dist < closestDist {
                closestDist = dist
                closestId = node.id
            }
        }
        return closestId
    }

    private func selectNode(at point: CGPoint, zoom: CGFloat, panX: CGFloat, panY: CGFloat, size: CGSize) {
        viewModel.selectedGraphNodeId = hitTest(at: point, zoom: zoom, panX: panX, panY: panY, size: size)
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
}
