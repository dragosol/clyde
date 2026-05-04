//
//  GraphExpandPanel.swift
//  Clyde
//
//  Floating NSPanel for the hover-to-expand asset graph feature.
//  When the user hovers over the graph tile in the inspector, the
//  graph smoothly expands into a borderless floating panel that can
//  extend beyond Clyde's window boundaries. Moving the mouse outside
//  the panel causes it to smoothly shrink back. A button in the
//  top-right corner opens the graph in a full dedicated window.
//

import SwiftUI
import AppKit
import Combine


// MARK: - Floating Panel (NSPanel subclass)

/// A borderless, non-activating floating panel that hovers above the
/// main window. Used for the graph's hover-to-expand behavior so the
/// expanded graph can extend beyond the inspector tile's bounds.
class GraphFloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: true
        )
        isFloatingPanel = true
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        hidesOnDeactivate = false
        isMovableByWindowBackground = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
    }
}


// MARK: - Panel Controller

/// Manages the lifecycle of the floating graph panel.
/// Handles showing/hiding with smooth animations and mouse tracking.
@MainActor
class GraphPanelController: NSObject, ObservableObject {
    @Published var isShowing = false

    private var panel: GraphFloatingPanel?
    private var hideWorkItem: DispatchWorkItem?
    private var sourceRect: CGRect = .zero

    private let expandedSize = CGSize(width: 500, height: 500)

    /// Show the expanded graph panel, anchored to the given screen rect
    func show(
        sourceScreenRect: CGRect,
        graphData: GraphData?,
        selectedNodeId: Binding<String?>,
        onExpandToWindow: @escaping () -> Void
    ) {
        hideWorkItem?.cancel()
        sourceRect = sourceScreenRect

        if let existing = panel, existing.isVisible {
            return
        }

        let targetRect = calculatePanelRect(from: sourceScreenRect)
        let panel = GraphFloatingPanel(contentRect: targetRect)
        self.panel = panel

        // Wrap content in an onHover-aware view for auto-dismiss
        let panelContent = ExpandedGraphContent(
            graphData: graphData,
            selectedNodeId: selectedNodeId,
            onExpandToWindow: {
                onExpandToWindow()
                self.dismiss()
            },
            onDismiss: { self.dismiss() },
            onHoverChanged: { [weak self] hovering in
                if hovering {
                    self?.cancelDismiss()
                } else {
                    self?.scheduleDismiss()
                }
            }
        )

        let hostingView = NSHostingView(rootView: AnyView(panelContent))
        hostingView.frame = NSRect(origin: .zero, size: targetRect.size)
        panel.contentView = hostingView

        // Start at source rect size, animate to expanded
        let startRect = NSRect(
            x: sourceScreenRect.origin.x,
            y: sourceScreenRect.origin.y,
            width: sourceScreenRect.width,
            height: sourceScreenRect.height
        )
        panel.setFrame(startRect, display: false)
        panel.alphaValue = 0

        if let mainWindow = NSApp.keyWindow ?? NSApp.windows.first(where: { $0.isVisible && !($0 is NSPanel) }) {
            mainWindow.addChildWindow(panel, ordered: .above)
        }

        panel.orderFront(nil)

        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.25
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().setFrame(targetRect, display: true)
            panel.animator().alphaValue = 1.0
        }

        isShowing = true
    }

    /// Dismiss the panel with a shrink animation
    func dismiss() {
        guard let panel = panel, panel.isVisible else { return }

        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.2
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            panel.animator().alphaValue = 0
            let shrinkRect = NSRect(
                x: sourceRect.origin.x,
                y: sourceRect.origin.y,
                width: sourceRect.width,
                height: sourceRect.height
            )
            panel.animator().setFrame(shrinkRect, display: true)
        }, completionHandler: { [weak self] in
            guard let self else { return }
            Task { @MainActor in
                panel.parent?.removeChildWindow(panel)
                panel.orderOut(nil)
                self.panel = nil
                self.isShowing = false
            }
        })
    }

    /// Schedule a dismiss after a short delay (cancelled if mouse re-enters)
    func scheduleDismiss() {
        hideWorkItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            self?.dismiss()
        }
        hideWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: item)
    }

    /// Cancel any pending dismiss (mouse re-entered)
    func cancelDismiss() {
        hideWorkItem?.cancel()
        hideWorkItem = nil
    }

    // MARK: - Private

    private func calculatePanelRect(from sourceRect: CGRect) -> NSRect {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else {
            return NSRect(origin: sourceRect.origin, size: expandedSize)
        }

        let visibleFrame = screen.visibleFrame

        // Center the expanded panel over the source tile
        let centerX = sourceRect.midX - expandedSize.width / 2
        let centerY = sourceRect.midY - expandedSize.height / 2

        // Clamp to screen bounds with 20px margin
        let x = max(visibleFrame.minX + 20, min(centerX, visibleFrame.maxX - expandedSize.width - 20))
        let y = max(visibleFrame.minY + 20, min(centerY, visibleFrame.maxY - expandedSize.height - 20))

        return NSRect(x: x, y: y, width: expandedSize.width, height: expandedSize.height)
    }
}


// MARK: - Expanded Graph Content (SwiftUI view inside the panel)

struct ExpandedGraphContent: View {
    let graphData: GraphData?
    @Binding var selectedNodeId: String?
    let onExpandToWindow: () -> Void
    let onDismiss: () -> Void
    let onHoverChanged: (Bool) -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            // Background: frosted glass with shadow
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(.ultraThinMaterial)
                .shadow(color: .black.opacity(0.2), radius: 20, y: 8)

            // Graph content
            VStack(spacing: 0) {
                // Header bar
                HStack {
                    Label("Asset Graph", systemImage: "point.3.connected.trianglepath.dotted")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)

                    Spacer()

                    // Expand to full window button
                    Button(action: onExpandToWindow) {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(4)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .help("Open in full window")

                    // Close button
                    Button(action: onDismiss) {
                        Image(systemName: "xmark")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(4)
                            .background(.ultraThinMaterial, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .help("Close")
                }
                .padding(.horizontal, 12)
                .padding(.top, 10)
                .padding(.bottom, 6)

                // The actual graph — larger and more detailed
                if let data = graphData, !data.nodes.isEmpty {
                    ExpandedAssetGraphView(graphData: data, selectedNodeId: $selectedNodeId)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .padding(.horizontal, 8)
                        .padding(.bottom, 4)
                } else {
                    VStack(spacing: 8) {
                        Image(systemName: "point.3.connected.trianglepath.dotted")
                            .font(.title)
                            .foregroundStyle(.tertiary)
                        Text("No assets yet")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(.bottom, 8)
                }

                // Legend at the bottom
                GraphLegend()
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .onHover { hovering in
            onHoverChanged(hovering)
        }
    }
}


// MARK: - Expanded Graph View (larger version with premium visuals)

/// A larger version of AssetGraphView used in the floating panel.
/// Shows labels, hover glow, connection highlighting, directional pulses.
struct ExpandedAssetGraphView: View {
    let graphData: GraphData
    @Binding var selectedNodeId: String?
    @StateObject private var simulation = GraphSimulation()
    @State private var hoveredNodeId: String?
    // Zoom state
    @State private var magnification: CGFloat = 1.0
    @State private var steadyZoom: CGFloat = 1.0
    // Pan state
    @State private var panOffset: CGSize = .zero
    @State private var steadyPan: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            let bounds = geo.size
            let currentZoom = steadyZoom * magnification
            let currentPanX = steadyPan.width + panOffset.width
            let currentPanY = steadyPan.height + panOffset.height
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
            .onChange(of: graphData) { _, newData in
                simulation.updateFromData(newData, bounds: bounds)
            }
            .onChange(of: bounds) { _, newBounds in
                simulation.currentBounds = newBounds
            }
            .onAppear {
                simulation.currentBounds = bounds
                simulation.updateFromData(graphData, bounds: bounds)
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
            selectedNodeId: selectedNodeId,
            hoveredNodeId: hoveredNodeId,
            nodeCount: simulation.nodeStates.count,
            titleForId: { id in graphData.nodes.first { $0.id == id }?.title ?? "" },
            baseNodeRadius: 7
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
        let threshold: CGFloat = 22 / zoom
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
        selectedNodeId = hitTest(at: point, zoom: zoom, panX: panX, panY: panY, size: size)
    }
}
