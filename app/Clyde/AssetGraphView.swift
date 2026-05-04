//
//  AssetGraphView.swift
//  Clyde
//
//  Force-directed graph visualization of conversation assets.
//  Premium minimal design: small glowing circles, straight edges,
//  directional connection highlighting on hover.
//

import SwiftUI
import Combine

// MARK: - Graph Simulation

@MainActor
class GraphSimulation: ObservableObject {
    struct NodeState: Identifiable {
        let id: String
        let nodeType: String
        var position: CGPoint
        var velocity: CGPoint = .zero
        var appearProgress: CGFloat = 0   // 0→1 scale-in
        var glowIntensity: CGFloat = 0    // 0→1 entrance bloom
        var targetOpacity: CGFloat = 1
    }

    struct EdgeState: Identifiable {
        var id: String { "\(fromId)-\(toId)" }
        let fromId: String
        let toId: String
        let kind: String
        var drawProgress: CGFloat = 0     // 0→1 progressive draw
        var opacity: CGFloat = 0          // fade-in after draw completes
    }

    @Published var nodeStates: [NodeState] = []
    @Published var edgeStates: [EdgeState] = []
    @Published var isSettled = false

    /// Pulsing phase for directional animations (0→2π, cycles continuously)
    @Published var pulsePhase: CGFloat = 0

    /// When true, step() will position all nodes in a circle on its first
    /// call with valid bounds. This decouples node creation (which may
    /// happen before GeometryReader has measured) from layout.
    var needsLayout = false

    /// Current view bounds — updated by the view whenever GeometryReader
    /// reports a new size. step() reads this directly so the physics loop
    /// always uses fresh bounds even though .task captures a stale closure.
    var currentBounds: CGSize = .zero

    // Base physics constants — scaled dynamically to view size in step()
    private let damping: CGFloat = 0.82
    private let settleThreshold: CGFloat = 0.3

    private var knownNodeIds: Set<String> = []
    private var knownEdgeKeys: Set<String> = []

    /// Update node/edge lists from graph data. Does NOT position nodes —
    /// new nodes are created at (0,0) and `needsLayout` is set so that
    /// step() handles positioning once real bounds are available.
    func updateFromData(_ data: GraphData?, bounds: CGSize) {
        guard let data, !data.nodes.isEmpty else {
            nodeStates = []
            edgeStates = []
            knownNodeIds = []
            knownEdgeKeys = []
            needsLayout = false
            return
        }

        var addedNew = false

        // Add new nodes — position is placeholder; step() will layout
        for node in data.nodes {
            if !knownNodeIds.contains(node.id) {
                knownNodeIds.insert(node.id)
                nodeStates.append(NodeState(
                    id: node.id,
                    nodeType: node.type,
                    position: .zero,  // placeholder — step() will layout
                    appearProgress: 0,
                    glowIntensity: 1.0
                ))
                addedNew = true
            }
        }

        // Remove nodes that no longer exist
        let currentIds = Set(data.nodes.map(\.id))
        nodeStates.removeAll { !currentIds.contains($0.id) }
        knownNodeIds = currentIds

        // Add new edges
        for edge in data.edges {
            let key = "\(edge.fromId)-\(edge.toId)"
            if !knownEdgeKeys.contains(key) {
                knownEdgeKeys.insert(key)
                edgeStates.append(EdgeState(
                    fromId: edge.fromId,
                    toId: edge.toId,
                    kind: edge.kind,
                    drawProgress: 0,
                    opacity: 0
                ))
            }
        }

        // Remove edges whose nodes no longer exist
        let currentEdgeKeys = Set(data.edges.map { "\($0.fromId)-\($0.toId)" })
        edgeStates.removeAll { !currentEdgeKeys.contains($0.id) }
        knownEdgeKeys = currentEdgeKeys

        if addedNew { needsLayout = true }
        isSettled = false
    }

    func step() {
        let bounds = currentBounds
        guard nodeStates.count > 0 else { return }
        guard bounds.width > 1 && bounds.height > 1 else { return }
        let n = nodeStates.count

        // Deferred layout: position nodes in a circle once we have valid bounds.
        // Nodes are created at (0,0) by updateFromData and needsLayout is set.
        if needsLayout && bounds.width > 10 && bounds.height > 10 {
            let cx = bounds.width / 2
            let cy = bounds.height / 2
            let radius = min(bounds.width, bounds.height) * 0.3
            for i in 0..<n {
                if n == 1 {
                    nodeStates[i].position = CGPoint(x: cx, y: cy)
                } else {
                    let angle = (2 * .pi * CGFloat(i)) / CGFloat(n)
                    nodeStates[i].position = CGPoint(
                        x: cx + cos(angle) * radius,
                        y: cy + sin(angle) * radius
                    )
                }
                nodeStates[i].velocity = .zero
            }
            needsLayout = false
        }

        // Scale physics to view size so behavior is consistent from
        // the tiny inspector tile (~150px) up to full-window (~1400px).
        let refSize: CGFloat = 500  // reference size constants were tuned for
        let viewScale = max(0.3, min(bounds.width, bounds.height) / refSize)
        let repulsion: CGFloat = max(800, 4000 * viewScale * viewScale)
        let springK: CGFloat = 0.008
        let springRest: CGFloat = max(20, 90 * viewScale)
        // Stronger gravity for smaller views to keep nodes centered
        let centerGravity: CGFloat = viewScale < 0.6 ? 0.025 : 0.01
        let margin: CGFloat = max(8, 20 * viewScale)

        var forces = Array(repeating: CGPoint.zero, count: n)

        // Repulsion (all pairs)
        for i in 0..<n {
            for j in (i+1)..<n {
                let dx = nodeStates[i].position.x - nodeStates[j].position.x
                let dy = nodeStates[i].position.y - nodeStates[j].position.y
                let distSq = max(dx*dx + dy*dy, 100)
                let dist = sqrt(distSq)
                let f = repulsion / distSq
                let fx = f * dx / dist
                let fy = f * dy / dist
                forces[i].x += fx; forces[i].y += fy
                forces[j].x -= fx; forces[j].y -= fy
            }
        }

        // Spring attraction (edges)
        let idx = Dictionary(uniqueKeysWithValues: nodeStates.enumerated().map { ($1.id, $0) })
        for edge in edgeStates {
            guard let i = idx[edge.fromId], let j = idx[edge.toId] else { continue }
            let dx = nodeStates[j].position.x - nodeStates[i].position.x
            let dy = nodeStates[j].position.y - nodeStates[i].position.y
            let dist = max(sqrt(dx*dx + dy*dy), 1)
            let displacement = dist - springRest
            let f = springK * displacement
            let fx = f * dx / dist
            let fy = f * dy / dist
            forces[i].x += fx; forces[i].y += fy
            forces[j].x -= fx; forces[j].y -= fy
        }

        // Center gravity — pulls nodes toward the center of the view
        let cx = bounds.width / 2
        let cy = bounds.height / 2
        for i in 0..<n {
            forces[i].x += (cx - nodeStates[i].position.x) * centerGravity
            forces[i].y += (cy - nodeStates[i].position.y) * centerGravity
        }

        // Soft boundary repulsion — exponential push away from edges
        // instead of hard clamping, which caused corner pooling
        for i in 0..<n {
            let x = nodeStates[i].position.x
            let y = nodeStates[i].position.y
            let pushStrength: CGFloat = 2.0

            if x < margin {
                forces[i].x += pushStrength * (margin - x)
            } else if x > bounds.width - margin {
                forces[i].x -= pushStrength * (x - (bounds.width - margin))
            }
            if y < margin {
                forces[i].y += pushStrength * (margin - y)
            } else if y > bounds.height - margin {
                forces[i].y -= pushStrength * (y - (bounds.height - margin))
            }
        }

        // Apply forces + damping with velocity cap to prevent flinging
        let maxSpeed: CGFloat = max(15, min(bounds.width, bounds.height) * 0.08)
        var maxVel: CGFloat = 0
        for i in 0..<n {
            nodeStates[i].velocity.x = (nodeStates[i].velocity.x + forces[i].x) * damping
            nodeStates[i].velocity.y = (nodeStates[i].velocity.y + forces[i].y) * damping
            // Clamp velocity magnitude to prevent explosive flings
            let speed = sqrt(nodeStates[i].velocity.x * nodeStates[i].velocity.x
                           + nodeStates[i].velocity.y * nodeStates[i].velocity.y)
            if speed > maxSpeed {
                let scale = maxSpeed / speed
                nodeStates[i].velocity.x *= scale
                nodeStates[i].velocity.y *= scale
            }
            nodeStates[i].position.x += nodeStates[i].velocity.x
            nodeStates[i].position.y += nodeStates[i].velocity.y
            // Safety clamp — but the soft repulsion should keep nodes well inside
            nodeStates[i].position.x = max(5, min(bounds.width - 5, nodeStates[i].position.x))
            nodeStates[i].position.y = max(5, min(bounds.height - 5, nodeStates[i].position.y))
            maxVel = max(maxVel, abs(nodeStates[i].velocity.x) + abs(nodeStates[i].velocity.y))

            // Animate appearance: spring-like ease with overshoot
            if nodeStates[i].appearProgress < 1 {
                nodeStates[i].appearProgress = min(1, nodeStates[i].appearProgress + 0.05)
            }
            // Fade entrance glow
            if nodeStates[i].glowIntensity > 0 {
                nodeStates[i].glowIntensity = max(0, nodeStates[i].glowIntensity - 0.015)
            }
        }

        // Animate edges: progressive draw then fade-in
        for i in edgeStates.indices {
            if edgeStates[i].drawProgress < 1 {
                edgeStates[i].drawProgress = min(1, edgeStates[i].drawProgress + 0.035)
            }
            // Fade in opacity after draw starts
            let targetOpacity: CGFloat = min(edgeStates[i].drawProgress * 1.5, 1.0)
            edgeStates[i].opacity += (targetOpacity - edgeStates[i].opacity) * 0.08
        }

        // Advance pulse phase (continuous cycle for directional animations)
        pulsePhase += 0.06
        if pulsePhase > .pi * 2 { pulsePhase -= .pi * 2 }

        isSettled = maxVel < settleThreshold
            && nodeStates.allSatisfy { $0.appearProgress >= 1 && $0.glowIntensity <= 0 }
            && edgeStates.allSatisfy { $0.drawProgress >= 1 }
    }
}


// MARK: - Graph Colors

enum GraphColors {
    // Refined palette — softer, more premium
    static let file = Color(red: 0.30, green: 0.85, blue: 0.45)   // green
    static let url = Color(red: 0.40, green: 0.65, blue: 0.95)    // blue
    static let query = Color(red: 0.60, green: 0.45, blue: 0.90)  // purple
    static let fact = Color(red: 0.95, green: 0.75, blue: 0.30)   // amber
    static let edge = Color.white.opacity(0.18)                     // subtle white

    static func forType(_ type: String) -> Color {
        switch type {
        case "file": return file
        case "url": return url
        case "query": return query
        case "fact": return fact
        default: return Color.white.opacity(0.5)
        }
    }
}


// MARK: - Graph Canvas View

struct AssetGraphView: View {
    @EnvironmentObject var viewModel: AppViewModel
    @StateObject private var simulation = GraphSimulation()

    /// The node the user's mouse is currently over
    @State private var hoveredNodeId: String?
    /// Whether we've fed the simulation its first data set
    @State private var didInit = false

    var body: some View {
        GeometryReader { geo in
            let bounds = geo.size
            TimelineView(.periodic(from: .now, by: (simulation.isSettled && hoveredNodeId == nil) ? 1.0 : 1.0/30.0)) { _ in
                Canvas { context, size in
                    let hoverInfo = buildHoverInfo()
                    drawEdges(context: &context, size: size, hoverInfo: hoverInfo, zoom: 1.0)
                    drawNodes(context: &context, size: size, hoverInfo: hoverInfo, zoom: 1.0)
                }
                .onContinuousHover { phase in
                    switch phase {
                    case .active(let location):
                        hoveredNodeId = hitTest(at: location, zoom: 1.0, size: bounds)
                    case .ended:
                        hoveredNodeId = nil
                    @unknown default:
                        break
                    }
                }
                .gesture(
                    SpatialTapGesture()
                        .onEnded { value in
                            selectNode(at: value.location, zoom: 1.0, size: bounds)
                        }
                )
            }
            .onChange(of: viewModel.graphData) { _, newData in
                simulation.updateFromData(newData, bounds: bounds)
            }
            .onChange(of: bounds) { _, newBounds in
                simulation.currentBounds = newBounds
            }
            .task {
                // Feed initial data once — step() handles positioning
                // via needsLayout once currentBounds becomes valid.
                simulation.currentBounds = bounds
                if !didInit {
                    if let data = viewModel.graphData, !data.nodes.isEmpty {
                        simulation.updateFromData(data, bounds: bounds)
                    }
                    didInit = true
                }
                while !Task.isCancelled {
                    if !simulation.isSettled || hoveredNodeId != nil {
                        simulation.step()
                    }
                    try? await Task.sleep(for: .milliseconds(33))
                }
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

    // MARK: - Drawing

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
            baseNodeRadius: 5
        )
    }

    // MARK: - Hit Testing

    private func hitTest(at point: CGPoint, zoom: CGFloat, size: CGSize) -> String? {
        // Convert screen point to graph coordinates (reverse the zoom transform)
        let center = CGPoint(x: size.width / 2, y: size.height / 2)
        let graphPoint = CGPoint(
            x: (point.x - center.x) / zoom + center.x,
            y: (point.y - center.y) / zoom + center.y
        )
        let threshold: CGFloat = 18 / zoom
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

    private func selectNode(at point: CGPoint, zoom: CGFloat, size: CGSize) {
        viewModel.selectedGraphNodeId = hitTest(at: point, zoom: zoom, size: size)
    }
}


// MARK: - Preview

#Preview {
    AssetGraphView()
        .frame(width: 300, height: 300)
        .background(.black)
        .environmentObject(AppViewModel())
}
