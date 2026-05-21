//
//  NeuralNetBackgroundView.swift
//  Clyde
//
//  Animated neural-network background that plays while the AI is generating.
//  Glowing green nodes expand outward from the cursor origin in continuous
//  waves. Fades away immediately when streaming stops.
//
//  PERFORMANCE: Canvas + TimelineView at ~15fps when active, 0fps when idle.
//

import SwiftUI

// MARK: - Data Model

struct NeuralNode {
    var x: Float
    var y: Float
    let distFromOrigin: Float   // precomputed distance from origin
    let size: Float             // core radius
    let brightness: Float       // 0–1
    var conn0: Int16            // connection indices (-1 = none)
    var conn1: Int16
    var conn2: Int16
}

// MARK: - View

struct NeuralNetBackgroundView: View {
    let isActive: Bool
    /// Cursor tracker is observed by THIS view only — when the streaming
    /// status text moves, only the background re-renders, not the chat tree.
    /// This isolation is what prevents the layout-loop crash that previously
    /// happened when the cursor origin lived on AppViewModel directly.
    @ObservedObject var cursorTracker: CursorOriginTracker
    /// Fallback used when the streaming cursor hasn't reported a position yet
    /// (e.g. before the first stream starts, or right after one ends).
    let fallbackOrigin: CGPoint

    /// Convenience accessor used throughout the view body and helpers below.
    private var origin: CGPoint { cursorTracker.origin ?? fallbackOrigin }

    @State private var nodes: [NeuralNode] = []
    @State private var waveStart: Date? = nil
    @State private var fadeOutStart: Date? = nil
    @State private var isVisible = false
    @State private var lastSize: CGSize = .zero
    @State private var localOrigin: CGPoint = CGPoint(x: 400, y: 400) // origin in local coords
    @State private var needsInitialOrigin = false  // waiting for StreamingCursor to report position

    private let nodeCount = 55
    private let maxConnDist: Float = 200
    private let fadeOutDuration: Float = 0.6    // quick fade when streaming stops

    // Heartbeat pulse parameters
    private let beatInterval: Float = 1.4       // seconds between heartbeats
    private let beatSpeed: Float = 350          // how fast the pulse ring expands (px/s)
    private let beatWidth: Float = 300          // width of the expanding pulse ring
    private let beatDecay: Float = 0.7          // how quickly the beat fades with distance

    var body: some View {
        GeometryReader { geo in
            let size = geo.size
            if isVisible {
                TimelineView(.periodic(from: .now, by: 1.0 / 15.0)) { timeline in
                    Canvas { context, canvasSize in
                        draw(context: &context, size: canvasSize, now: timeline.date)
                    }
                }
                .drawingGroup()
            }
            Color.clear
                .onChange(of: size) { _, newSize in lastSize = newSize }
                .onAppear {
                    lastSize = size
                    updateLocalOrigin(geo: geo)
                    if isActive {
                        // Origin may already be valid from a previous stream
                        startAnimation()
                    }
                }
                .onChange(of: isActive) { _, active in
                    updateLocalOrigin(geo: geo)
                    if active {
                        // Don't animate immediately — StreamingCursor hasn't
                        // rendered yet so origin is stale. Set visible so the
                        // Canvas exists, but wait for the first origin update
                        // to actually generate nodes and start the pulse.
                        animationGeneration &+= 1
                        fadeOutStart = nil
                        isVisible = true
                        needsInitialOrigin = true
                        // Fallback: if origin never arrives (e.g. cursor off-screen),
                        // start anyway after a short delay.
                        let gen = animationGeneration
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                            guard animationGeneration == gen, needsInitialOrigin else { return }
                            needsInitialOrigin = false
                            startAnimation()
                        }
                    } else {
                        needsInitialOrigin = false
                        startFadeOut()
                    }
                }
                // Track origin changes continuously — the cursor preference
                // may arrive after streaming starts, and the cursor moves as
                // new content streams in.
                .onChange(of: origin) { _, _ in
                    let prev = localOrigin
                    updateLocalOrigin(geo: geo)

                    if isActive && needsInitialOrigin {
                        // First origin report after streaming started —
                        // NOW start the animation from the correct position.
                        needsInitialOrigin = false
                        startAnimation()
                    } else if isVisible {
                        // Ongoing origin updates: regenerate if cursor moved significantly
                        let dx = localOrigin.x - prev.x
                        let dy = localOrigin.y - prev.y
                        if dx * dx + dy * dy > 400 { // moved > 20px
                            let w = max(Float(lastSize.width), 800)
                            let h = max(Float(lastSize.height), 600)
                            generateNodes(width: w, height: h,
                                          ox: Float(localOrigin.x),
                                          oy: Float(localOrigin.y))
                            waveStart = Date()
                        }
                    }
                }
        }
        .ignoresSafeArea()
    }

    private func updateLocalOrigin(geo: GeometryProxy) {
        let geoGlobal = geo.frame(in: .global)
        localOrigin = CGPoint(
            x: origin.x - geoGlobal.minX,
            y: origin.y - geoGlobal.minY
        )
    }

    // MARK: - Lifecycle

    /// Monotonically increasing generation counter — prevents stale fade-out
    /// closures from killing a newer animation.
    @State private var animationGeneration: UInt64 = 0

    private func startAnimation() {
        animationGeneration &+= 1  // bump so any pending fade-out becomes stale
        let w = max(Float(lastSize.width), 800)
        let h = max(Float(lastSize.height), 600)
        generateNodes(width: w, height: h, ox: Float(localOrigin.x), oy: Float(localOrigin.y))
        fadeOutStart = nil
        waveStart = Date()
        isVisible = true
    }

    private func startFadeOut() {
        let gen = animationGeneration  // capture current generation
        fadeOutStart = Date()
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(fadeOutDuration) + 0.1) {
            // Only kill visibility if no new animation started since we began fading
            guard animationGeneration == gen, !isActive else { return }
            isVisible = false
            waveStart = nil
        }
    }

    // MARK: - Drawing

    private func draw(context: inout GraphicsContext, size: CGSize, now: Date) {
        guard let start = waveStart else { return }

        let elapsed = Float(now.timeIntervalSince(start))

        // Master fade-out when streaming stops
        var masterAlpha: Float = 1.0
        if let fadeStart = fadeOutStart {
            let fadeElapsed = Float(now.timeIntervalSince(fadeStart))
            masterAlpha = max(0, 1.0 - fadeElapsed / fadeOutDuration)
            if masterAlpha <= 0 { return }
        }

        let ox = CGFloat(localOrigin.x)
        let oy = CGFloat(localOrigin.y)

        // --- Draw origin beacon — large, faint glow around the spinning logo ---
        let beatPhase = (elapsed.truncatingRemainder(dividingBy: beatInterval)) / beatInterval
        let beaconIntensity = max(0.08, powf(1.0 - beatPhase, 3.0)) * masterAlpha
        let beaconGlowR: CGFloat = CGFloat(50.0 + 60.0 * (1.0 - beatPhase))
        let beaconCenter = CGPoint(x: ox, y: oy)
        let beaconGlowRect = CGRect(
            x: ox - beaconGlowR, y: oy - beaconGlowR,
            width: beaconGlowR * 2, height: beaconGlowR * 2
        )
        context.fill(
            Path(ellipseIn: beaconGlowRect),
            with: .radialGradient(
                Gradient(colors: [
                    Color(red: 0.33, green: 0.88, blue: 0.38)
                        .opacity(Double(beaconIntensity) * 0.10),
                    Color(red: 0.28, green: 0.72, blue: 0.32)
                        .opacity(Double(beaconIntensity) * 0.03),
                    .clear
                ]),
                center: beaconCenter,
                startRadius: 0,
                endRadius: beaconGlowR
            )
        )

        // --- Draw connections ---
        for i in 0..<nodes.count {
            let node = nodes[i]
            let na = heartbeat(dist: node.distFromOrigin, elapsed: elapsed) * masterAlpha
            guard na > 0.02 else { continue }

            let conns = [node.conn0, node.conn1, node.conn2]
            for ci in conns {
                guard ci >= 0, Int(ci) < nodes.count else { continue }
                let other = nodes[Int(ci)]
                let oa = heartbeat(dist: other.distFromOrigin, elapsed: elapsed) * masterAlpha
                guard oa > 0.02 else { continue }

                let lineIntensity = min(na, oa)
                let lineAlpha = Double(lineIntensity * 0.15)
                var path = Path()
                path.move(to: CGPoint(x: CGFloat(node.x), y: CGFloat(node.y)))
                path.addLine(to: CGPoint(x: CGFloat(other.x), y: CGFloat(other.y)))
                context.stroke(
                    path,
                    with: .color(Color(red: 0.28, green: 0.72, blue: 0.32).opacity(lineAlpha)),
                    lineWidth: lineIntensity > 0.5 ? 1.2 : 0.7
                )
            }
        }

        // --- Draw lines from origin to nearest nodes (anchor the center) ---
        let originLines = nodes.sorted { $0.distFromOrigin < $1.distFromOrigin }.prefix(6)
        for node in originLines {
            let beat = heartbeat(dist: node.distFromOrigin, elapsed: elapsed) * masterAlpha
            guard beat > 0.05 else { continue }
            let lineAlpha = Double(beat * 0.16)
            var path = Path()
            path.move(to: beaconCenter)
            path.addLine(to: CGPoint(x: CGFloat(node.x), y: CGFloat(node.y)))
            context.stroke(
                path,
                with: .color(Color(red: 0.30, green: 0.80, blue: 0.35).opacity(lineAlpha)),
                lineWidth: beat > 0.5 ? 1.4 : 0.8
            )
        }

        // --- Draw nodes ---
        for node in nodes {
            let beat = heartbeat(dist: node.distFromOrigin, elapsed: elapsed) * masterAlpha
            guard beat > 0.02 else { continue }

            let swell: Float = 1.0 + beat * 0.6
            let coreR = CGFloat(node.size * swell)
            let glowR = coreR * (2.5 + CGFloat(beat) * 1.5)
            let center = CGPoint(x: CGFloat(node.x), y: CGFloat(node.y))

            // Glow
            let glowRect = CGRect(
                x: center.x - glowR, y: center.y - glowR,
                width: glowR * 2, height: glowR * 2
            )
            context.fill(
                Path(ellipseIn: glowRect),
                with: .radialGradient(
                    Gradient(colors: [
                        Color(red: 0.28, green: 0.78, blue: 0.33)
                            .opacity(Double(beat * node.brightness) * 0.10),
                        .clear
                    ]),
                    center: center,
                    startRadius: 0,
                    endRadius: glowR
                )
            )

            // Core
            let coreRect = CGRect(
                x: center.x - coreR, y: center.y - coreR,
                width: coreR * 2, height: coreR * 2
            )
            context.fill(
                Path(ellipseIn: coreRect),
                with: .color(
                    Color(red: 0.33, green: 0.85, blue: 0.38)
                        .opacity(Double(beat * node.brightness) * 0.33)
                )
            )
        }
    }

    // MARK: - Heartbeat Math

    /// Heartbeat pulse — sharp rise, slow decay, repeating from origin.
    /// Returns 0–1 intensity for a node at `dist` pixels from origin.
    @inline(__always)
    private func heartbeat(dist: Float, elapsed: Float) -> Float {
        var maxIntensity: Float = 0

        // How many beats have been spawned so far?
        // New beat every beatInterval, continuously — never stops pulsing.
        let currentBeat = Int(elapsed / beatInterval)

        // Check the most recent 4 beats (older ones still fading at distance)
        let firstBeat = max(0, currentBeat - 3)
        for i in firstBeat...currentBeat {
            let beatAge = elapsed - Float(i) * beatInterval
            guard beatAge > 0 else { continue }

            // Where is this beat's pulse ring right now?
            let ringRadius = beatSpeed * beatAge
            let distFromRing = dist - ringRadius

            // Sharp leading edge (fast rise), trailing fade (slow decay)
            if distFromRing > beatWidth * 0.3 {
                continue  // ring hasn't reached this node yet
            }
            if distFromRing < -beatWidth {
                continue  // ring passed too long ago
            }

            var intensity: Float
            if distFromRing >= 0 {
                // Just ahead of ring — sharp rise
                let t = 1.0 - distFromRing / (beatWidth * 0.3)
                intensity = t * t * t  // cubic ease-in for sharp attack
            } else {
                // Behind ring — slow exponential decay (heartbeat tail)
                let behind = -distFromRing / beatWidth
                intensity = expf(-behind * 3.0)  // exponential falloff
            }

            // Older of the 4 visible beats are slightly weaker
            let ageIndex = currentBeat - i  // 0 = newest, 3 = oldest
            let ageFade = powf(beatDecay, Float(ageIndex))
            intensity *= ageFade

            // Distance fade — far nodes get less of the pulse
            let distFade = max(0.0, 1.0 - dist / 2000.0)
            intensity *= distFade

            maxIntensity = max(maxIntensity, intensity)
        }

        // Faint ambient baseline so nodes don't fully disappear between beats
        let ambient: Float = 0.06
        return min(1.0, maxIntensity + ambient)
    }

    // MARK: - Node Generation

    private func generateNodes(width: Float, height: Float, ox: Float, oy: Float) {
        var newNodes: [NeuralNode] = []
        newNodes.reserveCapacity(nodeCount)
        let margin: Float = 60

        for _ in 0..<nodeCount {
            let x = Float.random(in: -margin...(width + margin))
            let y = Float.random(in: -margin...(height + margin))
            let dx = x - ox
            let dy = y - oy

            newNodes.append(NeuralNode(
                x: x, y: y,
                distFromOrigin: sqrtf(dx * dx + dy * dy),
                size: Float.random(in: 1.5...3.5),
                brightness: Float.random(in: 0.4...1.0),
                conn0: -1, conn1: -1, conn2: -1
            ))
        }

        // Build connections
        for i in 0..<newNodes.count {
            var best: [(Int16, Float)] = []
            for j in 0..<newNodes.count where j != i {
                let dx = newNodes[i].x - newNodes[j].x
                let dy = newNodes[i].y - newNodes[j].y
                let d = sqrtf(dx * dx + dy * dy)
                if d < maxConnDist {
                    best.append((Int16(j), d))
                }
            }
            best.sort { $0.1 < $1.1 }
            let count = min(best.count, Int.random(in: 1...3))
            if count > 0 { newNodes[i].conn0 = best[0].0 }
            if count > 1 { newNodes[i].conn1 = best[1].0 }
            if count > 2 { newNodes[i].conn2 = best[2].0 }
        }

        nodes = newNodes
    }
}

// MARK: - Origin Tracking

struct StreamingCursorOriginKey: PreferenceKey {
    static var defaultValue: CGPoint? = nil
    static func reduce(value: inout CGPoint?, nextValue: () -> CGPoint?) {
        value = value ?? nextValue()
    }
}
