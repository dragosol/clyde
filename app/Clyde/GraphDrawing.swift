//
//  GraphDrawing.swift
//  Clyde
//
//  Shared Canvas drawing helpers for the asset graph.
//  Used by AssetGraphView, ExpandedAssetGraphView, and FullWindowGraphView
//  to ensure visual consistency across all graph views.
//
//  Node design:
//    Hovered / highlighted → soft pulsing radial glow (no hard circle)
//    Default → tiny crystal/diamond fragment with subtle glow
//  Edge design:
//    Highlighted → animated dashed stroke flowing in direction
//    Default → subtle solid line
//

import SwiftUI
import AppKit

// MARK: - Hover Info (shared struct)

struct GraphHoverInfo {
    let hoveredId: String?
    var forwardEdgeKeys: Set<String> = []
    var forwardNodeIds: Set<String> = []
    var backwardEdgeKeys: Set<String> = []
    var backwardNodeIds: Set<String> = []
}

// MARK: - Scroll Event Capture (for two-finger trackpad panning)

/// An invisible NSView that captures scroll-wheel and magnify events
/// via local event monitors, forwarding them to callbacks. Completely
/// transparent to hit testing so it never blocks hover, clicks, or
/// drag gestures on views beneath it.
struct GraphInputCapture: NSViewRepresentable {
    var onScroll: (CGFloat, CGFloat) -> Void
    var onMagnify: ((CGFloat) -> Void)?
    var onMagnifyEnd: ((CGFloat) -> Void)?

    func makeNSView(context: Context) -> GraphInputNSView {
        let view = GraphInputNSView()
        view.onScroll = onScroll
        view.onMagnify = onMagnify
        view.onMagnifyEnd = onMagnifyEnd
        return view
    }

    func updateNSView(_ nsView: GraphInputNSView, context: Context) {
        nsView.onScroll = onScroll
        nsView.onMagnify = onMagnify
        nsView.onMagnifyEnd = onMagnifyEnd
    }
}

class GraphInputNSView: NSView {
    var onScroll: ((CGFloat, CGFloat) -> Void)?
    var onMagnify: ((CGFloat) -> Void)?
    var onMagnifyEnd: ((CGFloat) -> Void)?
    private var scrollMonitor: Any?
    private var magnifyMonitor: Any?

    // Completely transparent to hit testing — hover, clicks
    // all pass through to the Canvas beneath us.
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        // Tear down monitors when view leaves a window (panel closed, etc.)
        guard window != nil else {
            removeMonitors()
            return
        }

        if scrollMonitor == nil {
            scrollMonitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
                guard let self = self, let w = self.window, w == event.window else { return event }
                let locationInView = self.convert(event.locationInWindow, from: nil)
                guard self.bounds.contains(locationInView) else { return event }

                let dx = event.scrollingDeltaX
                let dy = event.scrollingDeltaY
                if abs(dx) > 0.01 || abs(dy) > 0.01 {
                    DispatchQueue.main.async { self.onScroll?(dx, dy) }
                }
                // IMPORTANT: return event (don't consume) so other scroll
                // views in the app continue to work normally.
                return event
            }
        }

        if magnifyMonitor == nil {
            magnifyMonitor = NSEvent.addLocalMonitorForEvents(matching: .magnify) { [weak self] event in
                guard let self = self, let w = self.window, w == event.window else { return event }
                let locationInView = self.convert(event.locationInWindow, from: nil)
                guard self.bounds.contains(locationInView) else { return event }

                DispatchQueue.main.async {
                    if event.phase == .ended {
                        self.onMagnifyEnd?(event.magnification)
                    } else {
                        self.onMagnify?(event.magnification)
                    }
                }
                return event
            }
        }
    }

    override func removeFromSuperview() {
        removeMonitors()
        super.removeFromSuperview()
    }

    deinit {
        removeMonitors()
    }

    private func removeMonitors() {
        if let m = scrollMonitor { NSEvent.removeMonitor(m); scrollMonitor = nil }
        if let m = magnifyMonitor { NSEvent.removeMonitor(m); magnifyMonitor = nil }
    }
}

// MARK: - Drawing Helpers

enum GraphDrawing {

    // MARK: - Edges

    static func drawEdges(
        context: inout GraphicsContext,
        size: CGSize,
        hoverInfo: GraphHoverInfo,
        zoom: CGFloat,
        edgeStates: [GraphSimulation.EdgeState],
        nodeStates: [GraphSimulation.NodeState],
        pulsePhase: CGFloat
    ) {
        let idx = Dictionary(uniqueKeysWithValues: nodeStates.map { ($0.id, $0) })

        for edge in edgeStates {
            guard let from = idx[edge.fromId], let to = idx[edge.toId] else { continue }
            guard edge.drawProgress > 0 else { continue }

            let fromPt = from.position
            let toPt = to.position

            // Animated endpoint for entrance draw
            let endPt = CGPoint(
                x: fromPt.x + (toPt.x - fromPt.x) * edge.drawProgress,
                y: fromPt.y + (toPt.y - fromPt.y) * edge.drawProgress
            )

            let isForward = hoverInfo.forwardEdgeKeys.contains(edge.id)
            let isBackward = hoverInfo.backwardEdgeKeys.contains(edge.id)
            let isHighlighted = isForward || isBackward

            if isHighlighted && hoverInfo.hoveredId != nil {
                let highlightColor = isForward
                    ? GraphColors.forType(to.nodeType)
                    : GraphColors.forType(from.nodeType)

                // Outer glow line (soft)
                var glowPath = Path()
                glowPath.move(to: fromPt)
                glowPath.addLine(to: endPt)
                context.stroke(
                    glowPath,
                    with: .color(highlightColor.opacity(0.15)),
                    lineWidth: 5 / zoom
                )

                // Animated dashed line — dashes flow in the direction of the connection.
                // Forward: from→to, Backward: to→from (reversed dash offset).
                let dashLen: CGFloat = 8 / zoom
                let gapLen: CGFloat = 5 / zoom
                // pulsePhase cycles 0→2π; map to dash offset range
                let cycleLen = dashLen + gapLen
                let offset = (pulsePhase / (.pi * 2)) * cycleLen * 4
                let dashPhase = isForward ? offset : -offset

                var dashedPath = Path()
                dashedPath.move(to: fromPt)
                dashedPath.addLine(to: endPt)

                context.stroke(
                    dashedPath,
                    with: .color(highlightColor.opacity(0.75 * Double(edge.opacity))),
                    style: StrokeStyle(
                        lineWidth: 1.5 / zoom,
                        lineCap: .round,
                        dash: [dashLen, gapLen],
                        dashPhase: dashPhase
                    )
                )
            } else {
                // Default edge: subtle solid line
                var path = Path()
                path.move(to: fromPt)
                path.addLine(to: endPt)
                let dimFactor: CGFloat = (hoverInfo.hoveredId != nil) ? 0.06 : 0.18
                context.stroke(
                    path,
                    with: .color(Color.white.opacity(dimFactor * Double(edge.opacity))),
                    lineWidth: 0.7 / zoom
                )
            }
        }
    }

    // MARK: - Nodes

    static func drawNodes(
        context: inout GraphicsContext,
        size: CGSize,
        hoverInfo: GraphHoverInfo,
        zoom: CGFloat,
        nodeStates: [GraphSimulation.NodeState],
        pulsePhase: CGFloat,
        selectedNodeId: String?,
        hoveredNodeId: String?,
        nodeCount: Int,
        titleForId: (String) -> String,
        baseNodeRadius: CGFloat
    ) {
        for node in nodeStates {
            guard node.appearProgress > 0 else { continue }

            let pos = node.position
            let rawScale = node.appearProgress
            let overshoot: CGFloat = rawScale < 1.0
                ? (rawScale < 0.7 ? rawScale * 1.6 : 1.0 + (1.0 - rawScale) * 0.4)
                : 1.0
            let scale = min(overshoot, 1.15) * rawScale.squareRoot()
            let opacity = Double(min(node.appearProgress * 1.5, 1.0))
            let color = GraphColors.forType(node.nodeType)
            let isSelected = selectedNodeId == node.id
            let isHovered = hoveredNodeId == node.id
            let isForwardTarget = hoverInfo.forwardNodeIds.contains(node.id)
            let isBackwardSource = hoverInfo.backwardNodeIds.contains(node.id)
            let isHighlighted = isHovered || isForwardTarget || isBackwardSource
            let isDimmed = hoverInfo.hoveredId != nil && !isHighlighted
            let nodeOpacity = isDimmed ? opacity * 0.25 : opacity

            let r = baseNodeRadius * scale

            // Entrance glow bloom (fades after appearing) — soft radial gradient
            if node.glowIntensity > 0.01 {
                let bloomSize = r * (3 + node.glowIntensity * 4)
                let bloomRect = CGRect(x: pos.x - bloomSize, y: pos.y - bloomSize, width: bloomSize * 2, height: bloomSize * 2)
                let bloomGradient = Gradient(stops: [
                    .init(color: color.opacity(0.25 * Double(node.glowIntensity) * opacity), location: 0),
                    .init(color: color.opacity(0.08 * Double(node.glowIntensity) * opacity), location: 0.4),
                    .init(color: color.opacity(0), location: 1.0)
                ])
                context.fill(
                    Circle().path(in: bloomRect),
                    with: .radialGradient(bloomGradient, center: pos, startRadius: 0, endRadius: bloomSize)
                )
            }

            if isHovered || (isHighlighted && hoverInfo.hoveredId != nil) {
                // ─── Hovered / highlighted: soft pulsing radial glow ───
                // Uses a true radial gradient for smooth, organic falloff
                // instead of concentric hard-edged circles.
                let pulse = 0.8 + 0.2 * sin(pulsePhase * 2)  // gentle 0.8–1.0 range
                let basePulseOpacity = isHovered ? 0.5 : 0.3

                // Main glow — radial gradient from bright center to transparent edge
                let glowR = r * 5.5 * CGFloat(pulse)
                let glowRect = CGRect(x: pos.x - glowR, y: pos.y - glowR, width: glowR * 2, height: glowR * 2)
                let glowGradient = Gradient(stops: [
                    .init(color: Color.white.opacity(0.7 * basePulseOpacity * nodeOpacity), location: 0),
                    .init(color: color.opacity(0.6 * basePulseOpacity * nodeOpacity), location: 0.12),
                    .init(color: color.opacity(0.35 * basePulseOpacity * nodeOpacity), location: 0.3),
                    .init(color: color.opacity(0.12 * basePulseOpacity * nodeOpacity), location: 0.55),
                    .init(color: color.opacity(0.03 * basePulseOpacity * nodeOpacity), location: 0.8),
                    .init(color: color.opacity(0), location: 1.0)
                ])
                context.fill(
                    Circle().path(in: glowRect),
                    with: .radialGradient(glowGradient, center: pos, startRadius: 0, endRadius: glowR)
                )

                // Hot white center point — also gradient for softness
                let hotR = r * 1.2
                let hotRect = CGRect(x: pos.x - hotR, y: pos.y - hotR, width: hotR * 2, height: hotR * 2)
                let hotGradient = Gradient(stops: [
                    .init(color: Color.white.opacity(0.8 * nodeOpacity), location: 0),
                    .init(color: Color.white.opacity(0.3 * nodeOpacity), location: 0.4),
                    .init(color: Color.white.opacity(0), location: 1.0)
                ])
                context.fill(
                    Circle().path(in: hotRect),
                    with: .radialGradient(hotGradient, center: pos, startRadius: 0, endRadius: hotR)
                )

            } else {
                // ─── Default: crystal/diamond fragment with subtle glow ───
                // Tiny diamond shape (rotated square) with a faint aura.

                // Subtle aura behind the crystal — radial gradient for softness
                let auraR = r * 2.5
                let auraRect = CGRect(x: pos.x - auraR, y: pos.y - auraR, width: auraR * 2, height: auraR * 2)
                let auraGradient = Gradient(stops: [
                    .init(color: color.opacity(0.15 * nodeOpacity), location: 0),
                    .init(color: color.opacity(0.04 * nodeOpacity), location: 0.5),
                    .init(color: color.opacity(0), location: 1.0)
                ])
                context.fill(
                    Circle().path(in: auraRect),
                    with: .radialGradient(auraGradient, center: pos, startRadius: 0, endRadius: auraR)
                )

                // Diamond shape — rotated square
                let d = r * 0.9  // half-diagonal
                var diamond = Path()
                diamond.move(to: CGPoint(x: pos.x, y: pos.y - d))     // top
                diamond.addLine(to: CGPoint(x: pos.x + d * 0.7, y: pos.y))  // right
                diamond.addLine(to: CGPoint(x: pos.x, y: pos.y + d))  // bottom
                diamond.addLine(to: CGPoint(x: pos.x - d * 0.7, y: pos.y))  // left
                diamond.closeSubpath()

                context.fill(diamond, with: .color(color.opacity(0.75 * nodeOpacity)))

                // Crystal highlight — a small bright facet on the upper-left
                var facet = Path()
                facet.move(to: CGPoint(x: pos.x, y: pos.y - d))       // top
                facet.addLine(to: CGPoint(x: pos.x - d * 0.7, y: pos.y)) // left
                facet.addLine(to: CGPoint(x: pos.x - d * 0.15, y: pos.y - d * 0.3))
                facet.closeSubpath()
                context.fill(facet, with: .color(Color.white.opacity(0.25 * nodeOpacity)))

                // Tiny bright center point
                let dotR = r * 0.2
                let dotRect = CGRect(x: pos.x - dotR, y: pos.y - dotR, width: dotR * 2, height: dotR * 2)
                context.fill(Circle().path(in: dotRect), with: .color(Color.white.opacity(0.3 * nodeOpacity)))
            }

            // Selected ring
            if isSelected {
                let selR = r * 2.5
                var ring = Path()
                ring.addEllipse(in: CGRect(x: pos.x - selR, y: pos.y - selR, width: selR * 2, height: selR * 2))
                context.stroke(ring, with: .color(color.opacity(0.4 * opacity)), lineWidth: 1 / zoom)
            }

            // Label
            let showLabel = nodeCount < 20 || isSelected || isHovered || zoom > 1.3
            if showLabel {
                let fullTitle = titleForId(node.id)
                if !fullTitle.isEmpty {
                    let maxChars: Int
                    if isHovered { maxChars = 50 }
                    else if zoom > 2.0 { maxChars = 40 }
                    else if zoom > 1.3 { maxChars = 25 }
                    else { maxChars = 16 }
                    let displayTitle = fullTitle.count > maxChars
                        ? String(fullTitle.prefix(maxChars)) + "\u{2026}"
                        : fullTitle
                    let labelOpacity = (isHovered || isSelected) ? 0.9 : 0.5
                    let fontSize: CGFloat = max(6, min(11, 8.5 / zoom))
                    let text = context.resolve(Text(displayTitle)
                        .font(.system(size: fontSize, weight: .medium))
                        .foregroundColor(Color.white.opacity(labelOpacity * nodeOpacity)))
                    context.draw(text, at: CGPoint(x: pos.x, y: pos.y + r + 9 / zoom))
                }
            }
        }
    }
}
