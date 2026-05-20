//
//  AgentManager.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-05.
//  Rewritten 2026-04-06: Clean state machine with PID-based health checks.
//
//  Manages the Clyde agent + llama.cpp server lifecycle.
//  Single entry point: ensureReady() — handles all states.
//  PID-based liveness during inference avoids false zombie detection.

import Foundation
import Combine

@MainActor
class AgentManager: ObservableObject {

    // MARK: - Published State

    @Published private(set) var agentStatus: AgentStatus = .idle
    @Published private(set) var mlxStatus: MLXStatus = .stopped
    @Published var isConnected = false

    /// Transient confirmation message ("Agent loaded!", "Model loaded!").
    /// Auto-clears after 1.5s. Banner shows it in priority over status.
    @Published private(set) var transientMessage: String?
    private var transientClearTask: Task<Void, Never>?

    /// Live generation metrics from the active stream. Cleared when stream
    /// ends. The inspector's PerformanceTile renders this as a dashboard
    /// with a curved tok/s sparkline.
    @Published private(set) var liveMetrics: LiveMetrics?

    /// Ring buffer of the last ~60 samples (≈30s at 0.5s interval) for the
    /// sparkline graph. Kept newest-last.
    @Published private(set) var metricsHistory: [LiveMetrics] = []

    /// Peak tok/s across the current turn — resets when stream ends.
    @Published private(set) var peakTokensPerSecond: Double = 0

    /// Time-to-first-token (prefill) in seconds. Captured from the first
    /// metrics sample of a turn; persists until the NEXT turn starts.
    @Published private(set) var lastPrefillSeconds: Double?

    /// Tokens generated on the turn that just finished — appears in the
    /// "Tokens" stat cell between turns so the user always sees a number.
    @Published private(set) var tokensLastTurn: Int = 0

    /// Latest conversation-context snapshot + rolling history so the
    /// Context Pressure sparkline can show growth and post-compact drops.
    @Published private(set) var contextPressure: ContextPressure?
    @Published private(set) var contextHistory: [ContextPressure] = []

    /// CPU%/RAM/GPU-active monitor for the Clyde stack. Polled by the
    /// monitor's own Timer; surfaced in InspectorPanelView's Performance
    /// tile as three mini tiles.
    let systemStats = SystemStatsMonitor()

    private static let metricsHistoryCap = 60
    private static let contextHistoryCap = 120

    /// Called by AppViewModel each time a metrics SSE event arrives.
    /// Pass nil on stream completion — tile keeps last values but marks idle.
    func updateLiveMetrics(_ m: LiveMetrics?) {
        liveMetrics = m
        guard let m else {
            // End-of-stream tick: snapshot last turn's count + reset peak.
            if let last = metricsHistory.last {
                tokensLastTurn = last.tokens
            }
            peakTokensPerSecond = 0
            return
        }
        // First sample of a fresh turn (empty history OR elapsed shrank)
        // → treat elapsed as prefill latency. History is NOT cleared so
        // the sparkline shows a continuous graph across the conversation.
        let isFreshTurn = metricsHistory.isEmpty
            || (metricsHistory.last?.elapsed ?? 0) > m.elapsed + 0.5
        if isFreshTurn {
            lastPrefillSeconds = m.elapsed
            peakTokensPerSecond = 0
        }
        metricsHistory.append(m)
        if metricsHistory.count > Self.metricsHistoryCap {
            metricsHistory.removeFirst(metricsHistory.count - Self.metricsHistoryCap)
        }
        if m.tps > peakTokensPerSecond {
            peakTokensPerSecond = m.tps
        }
    }

    /// Called by AppViewModel when a context SSE event arrives. Threshold
    /// of 0 means "unchanged" — reuse the last known threshold (the
    /// agent sends 0 on post-compact deltas because the threshold itself
    /// didn't move, only the token count did).
    func updateContextPressure(_ snapshot: ContextPressure) {
        let threshold = snapshot.threshold > 0
            ? snapshot.threshold
            : (contextPressure?.threshold ?? snapshot.threshold)
        let fixed = ContextPressure(
            tokens: snapshot.tokens,
            threshold: threshold,
            messages: snapshot.messages > 0
                ? snapshot.messages
                : (contextPressure?.messages ?? snapshot.messages),
            timestamp: snapshot.timestamp
        )
        contextPressure = fixed
        contextHistory.append(fixed)
        if contextHistory.count > Self.contextHistoryCap {
            contextHistory.removeFirst(contextHistory.count - Self.contextHistoryCap)
        }
    }

    /// Set to true when a macOS tool reports a permission error.
    /// ContentView observes this to present PermissionsView.
    @Published var showPermissionsSheet = false

    /// Description of the permission that was denied (for the banner).
    @Published var lastPermissionError: String?

    // MARK: - State Machine

    enum AgentStatus: Equatable {
        case idle          // Fresh launch — nothing started
        case starting      // Agent process launching
        case loadingModel  // Agent up, MLX loading model into GPU
        case running       // Fully healthy
        case inferring     // MLX busy with generation (health checks use PID, not HTTP)
        case restarting    // Self-healing in progress
        case hibernating   // Shutting down for inactivity
        case hibernated    // Asleep
        case waking        // Coming back from hibernation
        case stopping      // Graceful shutdown
        case stopped       // Terminated
        case error(String) // Fatal — needs user action or auto-recovery

        var isError: Bool {
            if case .error = self { return true }
            return false
        }
    }

    enum MLXStatus: Equatable {
        case stopped
        case starting      // Process launched, waiting for port
        case loading       // Model loading into GPU (~10-15s)
        case running       // Healthy, responding to requests
        case busy          // Currently generating (PID alive, HTTP blocked)
        case recovering(String)
        case error(String)
    }

    // MARK: - Configuration

    /// Application Support directory for runtime data (writable)
    private let dataDir: String
    /// Path to the agent Python script (in dataDir or bundle)
    private let agentScript: String
    /// Legacy alias — points to dataDir for backward compatibility
    private let agentHome: String

    /// Path to ClydeEngine binary (Python interpreter for the agent)
    private let clydeEnginePath: String
    /// Path to llama-server binary (bundled or Homebrew fallback)
    private let llamaServerPath: String
    /// Fallback python3 path (Homebrew)
    private let pythonPath = "/opt/homebrew/bin/python3"
    /// Backend server command — llama.cpp for Qwen 3.6
    private let mlxServerCommand: String  // legacy name, points to llama-server
    private let mlxModel: String          // legacy name, points to Qwen GGUF

    private let mlxPort = 8810  // llama.cpp port (legacy name kept for compat)
    private let llamaPort = 8810
    private let agentPort = 8801
    private let healthCheckInterval: TimeInterval = 5.0
    private let hibernateAfter: TimeInterval = 600  // 10 min

    // MARK: - Process Tracking

    private var agentProcess: Process?
    private var mlxProcess: Process?
    private var watchdogProcess: Process?

    /// PIDs for liveness checks (survives process reference loss)
    private var agentPID: pid_t?
    private var mlxPID: pid_t?

    /// Whether we own these processes (started them ourselves)
    private var weOwnAgent = false
    private var weOwnMLX = false

    // MARK: - Tasks

    private var healthCheckTask: Task<Void, Never>?
    private var hibernateTask: Task<Void, Never>?

    // MARK: - Recovery State

    private var agentRestartCount = 0
    private var mlxRestartCount = 0
    private let maxRestartAttempts = 5
    private var lastAgentRestart: Date = .distantPast
    private var lastMLXRestart: Date = .distantPast

    /// Reset restart counters if enough time has passed (process was stable)
    private let restartCounterResetInterval: TimeInterval = 120  // 2 min

    // MARK: - Activity

    private var lastActivityTime = Date()
    private var isShuttingDown = false

    // MARK: - Pending Message

    var pendingMessage: PendingMessage?

    struct PendingMessage {
        let content: String
        let attachments: [Attachment]
        let conversationId: UUID
    }

    // MARK: - Init

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let fm = FileManager.default

        // ── Data directory (writable, persists user data) ──
        let appSupport = "\(home)/Library/Application Support/Clyde"
        self.dataDir = appSupport
        self.agentHome = appSupport  // backward compat alias

        // ── Agent script location ──
        // Prefer bundled agent code (inside .app), fall back to App Support
        let bundledAgent = Bundle.main.resourcePath.map { "\($0)/agent/agent.py" }
        if let bundled = bundledAgent, fm.fileExists(atPath: bundled) {
            self.agentScript = bundled
        } else {
            self.agentScript = "\(appSupport)/agent/agent.py"
        }

        // ── ClydeEngine (Python interpreter) ──
        // Prefer bundled in app's MacOS/, fall back to App Support, then Homebrew
        let bundledEngine = Bundle.main.bundlePath + "/Contents/MacOS/ClydeEngine"
        let appSupportEngine = "\(appSupport)/ClydeEngine.app/Contents/MacOS/ClydeEngine"
        if fm.fileExists(atPath: bundledEngine) {
            self.clydeEnginePath = bundledEngine
        } else if fm.fileExists(atPath: appSupportEngine) {
            self.clydeEnginePath = appSupportEngine
        } else {
            self.clydeEnginePath = appSupportEngine  // will be created at launch
        }

        // ── llama-server ──
        // Prefer bundled, fall back to Homebrew
        let bundledLlama = Bundle.main.bundlePath + "/Contents/MacOS/llama-server"
        if fm.fileExists(atPath: bundledLlama) {
            self.llamaServerPath = bundledLlama
        } else {
            self.llamaServerPath = "/opt/homebrew/bin/llama-server"
        }

        // ── Backend: llama.cpp with Qwen 3.6 ──
        self.mlxServerCommand = self.llamaServerPath  // legacy var name
        self.mlxModel = "\(home)/.clyde/models/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"

        // ── System stats monitor ──
        // Pattern-match on full process list (`ps -A`) so the agent and
        // llama-server are picked up even when the explicit PIDs above
        // happen to be nil (PID file racing the launch, or external
        // start). The `comm` column contains the executable basename.
        systemStats.processPatterns = [
            "llama-server",     // Pro tier
            "agent.py",         // FastAPI agent worker
            "ClydeEngine",      // bundled Python interpreter
            "mlx_vlm",          // Phase 3 Pro
            "flash_vlm",        // Phase 3 Flash
        ]
        systemStats.includeOwnPID = true
        systemStats.start()
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - PUBLIC API (3 entry points + inference tracking)
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    /// Called once at app launch. Detects existing processes but does NOT start anything.
    func onLaunch() {
        isShuttingDown = false
        setupDataDirectory()
        writePIDFile("\(dataDir)/.clyde.pid", pid: ProcessInfo.processInfo.processIdentifier)
        installSignalHandlers()
        ensureClydeEngineBundle()

        Task {
            let agentUp = await isPortResponding(agentPort)
            let mlxUp = await isPortResponding(mlxPort)

            if agentUp {
                // Validate PID files — only trust if process is alive
                if let pid = readPIDFile("\(agentHome)/.agent-server.pid"),
                   isProcessAlive(pid) {
                    agentPID = pid
                } else {
                    removePIDFile("\(agentHome)/.agent-server.pid")
                }
                transition(to: mlxUp ? .running : .loadingModel)
                isConnected = true
                if mlxUp {
                    if let pid = readPIDFile("\(agentHome)/.mlx-server.pid"),
                       isProcessAlive(pid) {
                        mlxPID = pid
                    } else {
                        removePIDFile("\(agentHome)/.mlx-server.pid")
                    }
                    mlxStatus = .running
                }
                startHealthCheck()
                resetHibernateTimer()
                launchWatchdog()
            } else {
                // No existing backends — stay idle. The pill prompts the
                // user to send a message; that triggers ensureReady →
                // startFullStack, giving us proper lazy-load semantics
                // instead of burning RAM on app launch.
                transition(to: .idle)
            }
        }
    }

    /// **Single entry point for "make everything ready."**
    /// Call before sending a message. Handles:
    ///   idle → start agent + MLX
    ///   hibernated → wake
    ///   error/stopped → restart
    ///   running → no-op
    ///   inferring → no-op (already busy)
    ///
    /// Returns true if the stack is ready for requests.
    @discardableResult
    /// True when the active route is external (LM Studio, custom URL) —
    /// no local backend to launch or monitor.
    private var isExternalRoute: Bool {
        let custom = UserDefaults.standard.string(forKey: "custom_llm_endpoint") ?? ""
        if !custom.trimmingCharacters(in: .whitespaces).isEmpty { return true }
        let kind = activeBackendKind
        return kind == .custom || kind == .ollama
    }

    func ensureReady() async -> Bool {
        // ── External route: only need the agent, not local backends ──
        if isExternalRoute {
            let agentOk = await isPortResponding(agentPort)
            if agentOk {
                if agentStatus != .running && agentStatus != .inferring {
                    print("[AgentManager] ensureReady: external route, agent alive — adopting")
                    transition(to: .running)
                    startHealthCheck()
                    resetHibernateTimer()
                    launchWatchdog()
                }
                return true
            }
            print("[AgentManager] ensureReady: external route, starting agent only")
            return await startAgentOnly()
        }

        switch agentStatus {
        case .running, .inferring:
            let agentOk = await isPortResponding(agentPort)
            if activeBackendKind.isProcessManagerManaged {
                if agentOk { return true }
            } else {
                let bPort = activeBackendPort
                let bOk = await isPortResponding(bPort)
                if agentOk && bOk { return true }
            }
            print("[AgentManager] ensureReady: status=\(stateLabel(agentStatus)) but agent=\(agentOk) — recovering")
            transition(to: .error("Connection dropped — restarting"))
            return await startFullStack()

        case .idle, .stopped, .error:
            let agentOk = await isPortResponding(agentPort)
            let isMLXBackend = activeBackendKind == .mlxVLM || activeBackendKind == .mlxLM
            if agentOk && activeBackendKind.isProcessManagerManaged {
                // ProcessManager-managed backend: agent alive = ready.
                // ProcessManager will lazy-start the backend on first chat request.
                print("[AgentManager] ensureReady: agent alive, \(activeBackendKind.displayName) — ProcessManager manages backend")
                transition(to: .running)
                startHealthCheck()
                resetHibernateTimer()
                launchWatchdog()
                return true
            }
            let backendPort = activeBackendPort
            let backendOk = await isPortResponding(backendPort)
            if agentOk && backendOk {
                print("[AgentManager] ensureReady: agent + backend (\(activeBackendKind.displayName)@\(backendPort)) already alive — adopting")
                transition(to: .running)
                if isMLXBackend { mlxStatus = .running }
                startHealthCheck()
                resetHibernateTimer()
                launchWatchdog()
                return true
            }
            if agentOk {
                if isMLXBackend {
                    // Agent up, MLX not yet — adopt agent, launch MLX only
                    print("[AgentManager] ensureReady: agent alive, MLX not — adopting agent, launching MLX")
                    transition(to: .loadingModel)
                    startHealthCheck()
                    let mlxLaunched = await launchMLX()
                    if mlxLaunched {
                        transition(to: .running)
                        launchWatchdog()
                        resetHibernateTimer()
                        return true
                    }
                    // MLX failed but agent works — proceed, health check monitors
                    transition(to: .error("MLX failed to load"))
                    launchWatchdog()
                    return false
                } else {
                    // Non-MLX backend (llama.cpp, Ollama, custom) — agent manages
                    // backend lifecycle via ProcessManager. Just adopt the agent;
                    // the backend will lazy-load on first chat request.
                    print("[AgentManager] ensureReady: agent alive, non-MLX backend (\(activeBackendKind.displayName)) — agent manages lifecycle")
                    transition(to: .running)
                    startHealthCheck()
                    resetHibernateTimer()
                    launchWatchdog()
                    return true
                }
            }
            // Nothing alive — full startup
            return await startFullStack()

        case .hibernated:
            return await wake()

        case .starting, .loadingModel, .waking, .restarting:
            // If loadingModel but both agent + active backend already alive, skip the wait
            if agentStatus == .loadingModel {
                let agentOk = await isPortResponding(agentPort)
                let bPort = activeBackendPort
                let bOk = await isPortResponding(bPort)
                if agentOk && bOk {
                    print("[AgentManager] ensureReady: agent + backend alive during .loadingModel — adopting")
                    transition(to: .running)
                    if activeBackendKind == .mlxVLM || activeBackendKind == .mlxLM {
                        mlxStatus = .running
                    }
                    return true
                }
            }
            // Already in progress — wait for it
            return await waitForReady(timeout: 90)

        case .hibernating, .stopping:
            // Wait for transition to complete, then start
            _ = await waitForState(oneOf: [.hibernated, .stopped], timeout: 10)
            return await startFullStack()
        }
    }

    /// Called at app quit. Cleans up processes we started.
    func onQuit() {
        guard !isShuttingDown else { return }
        isShuttingDown = true

        healthCheckTask?.cancel()
        healthCheckTask = nil
        hibernateTask?.cancel()
        hibernateTask = nil
        watchdogProcess?.terminate()
        watchdogProcess = nil

        if weOwnAgent { stopProcess(.agent) }
        if weOwnMLX { stopProcess(.mlx) }
        removePIDFile("\(agentHome)/.clyde.pid")
    }

    /// Tell AgentManager that MLX is now busy with inference.
    /// Health checks switch to PID-based liveness (no HTTP).
    func beginInference() {
        guard agentStatus == .running else { return }
        transition(to: .inferring)
        mlxStatus = .busy
    }

    /// Tell AgentManager inference is complete. Resume HTTP health checks.
    func endInference() {
        if agentStatus == .inferring {
            transition(to: .running)
        }
        if mlxStatus == .busy {
            mlxStatus = .running
        }
        // Reset the hibernation timer so the 10-min countdown
        // starts from inference END, not from message send.
        resetHibernateTimer()
    }

    // MARK: - Activity & Hibernation

    func recordActivity() {
        lastActivityTime = Date()
        resetHibernateTimer()
    }

    func setPendingMessage(content: String, attachments: [Attachment], conversationId: UUID) {
        pendingMessage = PendingMessage(content: content, attachments: attachments, conversationId: conversationId)
    }

    func clearPendingMessage() {
        pendingMessage = nil
    }

    /// Notify AgentManager that the user is swapping models. Drops to
    /// `.loadingModel`, then polls the new backend port until healthy and
    /// returns to `.running`. This makes the pill reflect the swap correctly
    /// (instead of staying on .running while the new backend boots silently).
    ///
    /// Critical: when swapping AWAY from llama.cpp (Clyde-owned) to an MLX
    /// backend (ProcessManager-owned), the llama-server keeps holding GPU
    /// memory and starves MLX. Kill the Clyde-owned llama-server in that
    /// case. The reverse is handled by ProcessManager's exclusive mode.
    func notifyModelSwap(newBackendKind kind: BackendKind, newPort port: Int) async {
        hibernateTask?.cancel()
        let isMLX = (kind == .mlxVLM || kind == .mlxLM)
        let isPMManaged = kind.isProcessManagerManaged

        // Free GPU before MLX boot if Clyde owns a llama-server.
        if isMLX, weOwnMLX, let mp = mlxProcess, mp.isRunning {
            print("[AgentManager] swap to MLX — terminating Clyde-owned llama-server (PID \(mp.processIdentifier))")
            mp.terminate()
            // Hard wait for port to free + GPU to release
            for _ in 0..<10 {
                try? await Task.sleep(for: .milliseconds(300))
                if !mp.isRunning { break }
            }
            if mp.isRunning {
                kill(mp.processIdentifier, SIGKILL)
            }
            mlxProcess = nil
            mlxPID = nil
            weOwnMLX = false
            mlxStatus = .stopped
        }
        transition(to: .loadingModel)
        if isMLX { mlxStatus = .loading }

        if isPMManaged {
            // ProcessManager in the Python agent owns backend lifecycle.
            // We just need to tell the agent to switch routes — it will
            // start the backend on the next chat request. Transition to
            // .running immediately; the agent handles the rest.
            print("[AgentManager] swap to \(kind.displayName) — ProcessManager manages backend, transitioning to .running")
            transition(to: .running)
            resetHibernateTimer()
            return
        }

        // MLX backends: poll until healthy (cold load can take 60-120s).
        let healthy = await waitForPort(port, timeout: 180)
        if healthy {
            if isMLX { mlxStatus = .running }
            transition(to: .running)
            resetHibernateTimer()
        } else {
            transition(to: .error("Model failed to load on \(kind.displayName) within 180s"))
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - STATE MACHINE
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    private func transition(to newState: AgentStatus) {
        let old = agentStatus
        agentStatus = newState
        print("[AgentManager] \(stateLabel(old)) → \(stateLabel(newState))")

        // Transient confirmation pills on key transitions.
        //   starting → loadingModel  ⇒ "Agent loaded!"
        //   loadingModel → running   ⇒ "Model loaded!"
        //   starting → running       ⇒ "Agent loaded!" then "Model loaded!"
        switch (old, newState) {
        case (.starting, .loadingModel):
            showTransient("Agent loaded!")
        case (.loadingModel, .running):
            showTransient("Model loaded!")
        case (.starting, .running):
            // Non-MLX path: agent boot completes the whole stack
            showTransient("Agent loaded!", thenAfter: 1.2, show: "Model loaded!")
        case (.waking, .running):
            showTransient("Resumed!")
        default:
            break
        }

        // Update derived state
        switch newState {
        case .running, .inferring:
            isConnected = true
        case .idle, .stopped, .hibernated:
            isConnected = false
        case .error:
            isConnected = false
            // Ensure health check is running so we can auto-recover.
            if healthCheckTask == nil || healthCheckTask?.isCancelled == true {
                startHealthCheck()
            }
        default:
            break
        }
    }

    /// Show a brief confirmation message in the pill, then auto-clear.
    /// If `show:` is provided, chain a second message after the first clears.
    private func showTransient(_ msg: String,
                                thenAfter chainDelay: Double = 0,
                                show next: String? = nil) {
        transientClearTask?.cancel()
        transientMessage = msg
        transientClearTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(1.5))
            guard !Task.isCancelled else { return }
            if let next, chainDelay > 0 {
                self.transientMessage = next
                try? await Task.sleep(for: .seconds(1.5))
                guard !Task.isCancelled else { return }
            }
            self.transientMessage = nil
        }
    }

    private func stateLabel(_ state: AgentStatus) -> String {
        switch state {
        case .idle: return "idle"
        case .starting: return "starting"
        case .loadingModel: return "loadingModel"
        case .running: return "running"
        case .inferring: return "inferring"
        case .restarting: return "restarting"
        case .hibernating: return "hibernating"
        case .hibernated: return "hibernated"
        case .waking: return "waking"
        case .stopping: return "stopping"
        case .stopped: return "stopped"
        case .error(let msg): return "error(\(msg))"
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - STARTUP
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    /// Start just the Python agent — no local backend.
    /// Used for external routes (LM Studio, custom endpoint).
    private func startAgentOnly() async -> Bool {
        transition(to: .starting)
        let ok = await launchAgent()
        if ok {
            transition(to: .running)
            startHealthCheck()
            resetHibernateTimer()
            launchWatchdog()
        } else {
            transition(to: .error("Agent failed to start"))
        }
        return ok
    }

    /// Start MLX + agent from scratch. Returns true if fully ready.
    /// MLX loads first (slow: 15-30s GPU load), then agent (fast: ~5s).
    private func startFullStack() async -> Bool {
        // Reset restart counters if coming from idle/stopped (fresh start)
        if agentStatus == .idle || agentStatus == .stopped {
            agentRestartCount = 0
            mlxRestartCount = 0
        }

        // Kill any orphan processes left from a previous Clyde session that
        // wasn't shut down cleanly (force-quit, debugger detach, crash).
        // This is the THOROUGH cleanup — SIGKILL on MLX, wait for port to
        // be free, wait for GPU memory to release. Without this, the new
        // MLX racing against the old one fails to load the model.
        await cleanupLeftoverAgent()
        await cleanupLeftoverMLX()

        // ── Phase 1: Load MLX (the slow/visible part) ──
        // ONLY launch MLX if the active backend is actually MLX.
        // When the user last used llama.cpp / Ollama / custom, the agent's
        // ProcessManager handles backend lifecycle — Clyde just needs the agent.
        let isMLXBackend = activeBackendKind == .mlxVLM || activeBackendKind == .mlxLM
        let isPMBackend = activeBackendKind.isProcessManagerManaged
        var mlxOk = !isMLXBackend  // Non-MLX backends skip this phase entirely

        if isMLXBackend && !isPMBackend {
            // Retry up to 3 times with cleanup between attempts. MLX can
            // fail to load on the first try when GPU memory is fragmented
            // from a previous crash, or when a leftover process didn't
            // fully release the Metal device. A second attempt after a
            // full cleanup usually succeeds.
            transition(to: .loadingModel)

            let maxMLXAttempts = 3
            for attempt in 1...maxMLXAttempts {
                mlxOk = await launchMLX()
                if mlxOk { break }
                if attempt < maxMLXAttempts {
                    print("[AgentManager] MLX launch attempt \(attempt)/\(maxMLXAttempts) failed — cleaning up and retrying")
                    forceKillMLX()
                    try? await Task.sleep(for: .seconds(3))
                    await cleanupLeftoverMLX()
                    try? await Task.sleep(for: .seconds(2))
                }
            }
            if !mlxOk {
                // MLX failed all attempts, but start the agent and health
                // check anyway. MLX may load successfully after a delay
                // (e.g., GPU memory clearing up). The health check will
                // detect when both are healthy and clear the error banner.
                // Without this, the app gets stuck in .error permanently
                // with no agent running and no health check to recover.
                print("[AgentManager] MLX failed all \(maxMLXAttempts) attempts — starting agent + health check anyway for recovery")
                transition(to: .error("MLX failed to load model after \(maxMLXAttempts) attempts"))

                // Start agent despite MLX failure — agent can handle MLX being down
                _ = await launchAgent()

                // Start health check so it can detect recovery
                startHealthCheck()
                launchWatchdog()  // Monitor for Clyde crash even in error state
                return false
            }
        } else {
            print("[AgentManager] Non-MLX backend (\(activeBackendKind.displayName)) — skipping MLX launch, agent manages backend lifecycle")
            transition(to: .starting)
        }

        // ── Phase 2: Start Agent (fast, needs MLX) ──
        transition(to: .starting)

        let agentOk = await launchAgent()
        guard agentOk else {
            transition(to: .error("Agent failed to start — check \(dataDir)/agent/agent.py"))
            startHealthCheck()
            launchWatchdog()  // Monitor for Clyde crash — MLX is running
            return false
        }

        // ── Phase 3: Ready ──
        transition(to: .running)
        startHealthCheck()
        resetHibernateTimer()
        launchWatchdog()
        return true
    }

    /// Launch the Python agent process. Returns true when port is responsive.
    private func launchAgent() async -> Bool {
        // NOTE: cleanupLeftoverAgent() should have been called by startFullStack
        // before we get here. Same reasoning as launchMLX — we don't trust
        // a port-responding agent that we didn't launch ourselves, because
        // it's almost certainly a leftover from a previous force-quit.
        if await isPortResponding(agentPort) {
            print("[AgentManager] Agent port \(agentPort) still responding — re-cleaning")
            killOrphanedProcess(name: "agent.py", signal: SIGKILL)
            try? await Task.sleep(for: .milliseconds(300))
        }

        let process = Process()

        // Use ClydeEngine (bundled Python binary) — shows "Clyde Engine" in
        // System Settings for permissions. Falls back to Homebrew python3.
        let fm = FileManager.default
        let enginePath = fm.fileExists(atPath: clydeEnginePath) ? clydeEnginePath : pythonPath
        process.executableURL = URL(fileURLWithPath: enginePath)
        process.arguments = [agentScript]
        process.currentDirectoryURL = URL(fileURLWithPath: "\(dataDir)/agent")

        // Pass data directory and Python home to the agent
        var env = ProcessInfo.processInfo.environment
        env["CLYDE_DATA_DIR"] = dataDir
        env["PYTHONHOME"] = env["PYTHONHOME"] ?? "/opt/homebrew/Cellar/python@3.13/3.13.7/Frameworks/Python.framework/Versions/3.13"
        process.environment = env

        let logHandle = openLog("/tmp/clyde-agent.log")
        process.standardOutput = logHandle ?? FileHandle.nullDevice
        process.standardError = logHandle ?? FileHandle.nullDevice

        do {
            try process.run()
            agentProcess = process
            agentPID = process.processIdentifier
            weOwnAgent = true
            writePIDFile("\(agentHome)/.agent-server.pid", pid: process.processIdentifier)
            print("[AgentManager] Agent launched (PID \(process.processIdentifier)), waiting for port \(agentPort)...")

            let ready = await waitForPort(agentPort, timeout: 20)
            if ready {
                print("[AgentManager] Agent responsive on port \(agentPort)")
                return true
            } else {
                print("[AgentManager] Agent failed to respond within 20s")
                // Check if process died
                if !process.isRunning {
                    print("[AgentManager] Agent process exited with code \(process.terminationStatus)")
                }
                return false
            }
        } catch {
            print("[AgentManager] Agent launch error: \(error)")
            return false
        }
    }

    /// Launch llama.cpp backend server. Returns true when port is responsive.
    /// (Function name kept as launchMLX for binary compat with callers.)
    private func launchMLX() async -> Bool {
        // When backend is ProcessManager-managed (llama.cpp, mlx-vlm Pro,
        // mlx-flash), the Python agent owns lifecycle. Clyde must NOT
        // spawn a competing server on the same port.
        if activeBackendKind.isProcessManagerManaged {
            print("[AgentManager] launchMLX skipped — ProcessManager manages \(activeBackendKind.displayName) lifecycle")
            mlxStatus = .running   // optimistic; agent will lazy-start it
            return true
        }

        if await isPortResponding(mlxPort) {
            print("[AgentManager] Port \(mlxPort) still responding — re-cleaning")
            killOrphanedProcess(name: "llama-server", signal: SIGKILL)
            try? await Task.sleep(for: .milliseconds(500))
        }

        mlxStatus = .starting

        let process = Process()
        process.executableURL = URL(fileURLWithPath: mlxServerCommand)
        process.arguments = [
            "--model", mlxModel,
            "--port", String(mlxPort),
            "--host", "127.0.0.1",
            "--ctx-size", "131072",
            "--n-gpu-layers", "999",
            "--cache-type-k", "q4_0",
            "--cache-type-v", "q4_0",
            "--flash-attn", "on",
            "--mmap",
            "--jinja",
            "--parallel", "1",
            "--cont-batching"
        ]
        process.environment = ProcessInfo.processInfo.environment

        let logHandle = openLog("/tmp/clyde-llamacpp.log")
        process.standardOutput = logHandle ?? FileHandle.nullDevice
        process.standardError = logHandle ?? FileHandle.nullDevice

        do {
            try process.run()
            mlxProcess = process
            mlxPID = process.processIdentifier
            weOwnMLX = true
            writePIDFile("\(agentHome)/.mlx-server.pid", pid: process.processIdentifier)
            mlxStatus = .loading
            print("[AgentManager] llama-server launched (PID \(process.processIdentifier)), loading model...")

            let ready = await waitForPort(mlxPort, timeout: 90)
            if ready {
                mlxStatus = .running
                print("[AgentManager] llama-server responsive on port \(mlxPort)")
                return true
            } else {
                if !process.isRunning {
                    mlxStatus = .error("llama-server crashed during load (exit \(process.terminationStatus))")
                } else {
                    mlxStatus = .error("llama-server timed out loading model (90s)")
                }
                return false
            }
        } catch {
            mlxStatus = .error("llama-server launch error: \(error.localizedDescription)")
            return false
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - HIBERNATION
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    private func resetHibernateTimer() {
        hibernateTask?.cancel()
        guard !isShuttingDown else { return }

        hibernateTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(self?.hibernateAfter ?? 300))
            guard let self, !Task.isCancelled, !self.isShuttingDown else { return }

            // Don't hibernate mid-inference or with pending work
            guard self.agentStatus == .running else { return }
            guard self.pendingMessage == nil else { return }

            let elapsed = Date().timeIntervalSince(self.lastActivityTime)
            guard elapsed >= self.hibernateAfter - 5 else { return }

            await self.hibernate()
        }
    }

    private func hibernate() async {
        guard agentStatus == .running else { return }
        print("[AgentManager] Hibernating — no activity for \(Int(hibernateAfter))s")
        transition(to: .hibernating)

        healthCheckTask?.cancel()
        healthCheckTask = nil

        stopProcess(.agent)
        if weOwnMLX { stopProcess(.mlx) }

        transition(to: .hibernated)
        print("[AgentManager] Hibernated. Send a message to wake.")
    }

    private func wake() async -> Bool {
        guard agentStatus == .hibernated else { return agentStatus == .running }
        transition(to: .waking)
        print("[AgentManager] Waking from hibernation...")
        return await startFullStack()
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - HEALTH CHECK (PID-aware)
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    private var agentHealthFailures = 0
    private var mlxHealthFailures = 0

    private func startHealthCheck() {
        healthCheckTask?.cancel()
        agentHealthFailures = 0
        mlxHealthFailures = 0

        healthCheckTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(self?.healthCheckInterval ?? 5))
                guard let self, !self.isShuttingDown else { break }

                // Skip checks for non-active states
                // Skip health checks during startup/shutdown transitions
                // to prevent recovery from interfering with launches
                // that are still in progress.
                let skip: Set<String> = [
                    "idle", "starting",                    // startup
                    "hibernated", "hibernating", "waking", // sleep
                    "stopping", "stopped",                 // shutdown
                ]
                if skip.contains(self.stateLabel(self.agentStatus)) { continue }

                await self.runHealthCheck()
            }
        }
    }

    /// Check if the agent has signaled that MLX needs a restart.
    /// The agent writes ~/.clyde/.mlx-needs-restart when it detects MLX is down.
    /// This provides instant detection instead of waiting for health check cycles.
    private func checkMLXRestartSignal() -> Bool {
        let flagPath = "\(agentHome)/.mlx-needs-restart"
        if FileManager.default.fileExists(atPath: flagPath) {
            try? FileManager.default.removeItem(atPath: flagPath)
            print("[AgentManager] Agent signaled MLX needs restart (flag file detected)")
            return true
        }
        return false
    }

    private func runHealthCheck() async {
        let isInferring = agentStatus == .inferring

        // ── Fast path: check if agent signaled MLX is down ──
        if checkMLXRestartSignal() {
            print("[AgentManager] Fast MLX recovery triggered by agent signal")
            await recoverMLX()
            return
        }

        // ── Agent check ──
        // Agent always responds to HTTP (it's Python, not blocked by MLX)
        // During inference, be more tolerant — agent may be busy with tool calls
        let agentAlive = await isPortResponding(agentPort)

        if agentAlive {
            agentHealthFailures = 0
            // If we were in an error or restarting state but the agent is now
            // responding, clear it — it recovered.
            if agentStatus.isError || agentStatus == .restarting {
                print("[AgentManager] Agent responding while in \(agentStatus) state — recovering to .running")
                transition(to: .running)
                // Trigger replay if there's a pending message waiting
                if pendingMessage != nil {
                    print("[AgentManager] healthCheck: posting replay for pending message after recovery")
                    DispatchQueue.main.async {
                        NotificationCenter.default.post(name: .agentReadyForReplay, object: nil)
                    }
                }
            }
            // If stuck in .loadingModel but agent is alive, check MLX too
            // and transition to .running. Prevents the deadlock where
            // waitForReady blocks sendMessage because the state never advances.
            if agentStatus == .loadingModel {
                let mlxAlive = await isPortResponding(mlxPort)
                if mlxAlive {
                    print("[AgentManager] Both backends alive while in .loadingModel — transitioning to .running")
                    transition(to: .running)
                    mlxStatus = .running
                }
            }
        } else {
            agentHealthFailures += 1

            // During inference, be much more tolerant:
            // The agent stays alive but may be slow responding while waiting
            // for MLX recovery or running tools. Check PID instead.
            if isInferring, let pid = agentPID, isProcessAlive(pid) {
                // Agent process alive, just busy — reset to 1 (not 0, so we still notice if it truly dies)
                if agentHealthFailures > 1 { agentHealthFailures = 1 }
            }

            // Require more failures during inference before declaring dead
            let threshold = isInferring ? 6 : 3
            if agentHealthFailures >= threshold {
                print("[AgentManager] Agent unresponsive (\(agentHealthFailures) checks, threshold=\(threshold)) — recovering")
                await recoverAgent()
            }
        }

        // ── MLX check ──
        // Only monitor backend health when Clyde owns the process.
        // ProcessManager-managed backends (llama.cpp, mlx-vlm Pro,
        // mlx-flash) are probed by the Python agent's ProcessManager.
        // Clyde probing independently causes bogus recovery cycles.
        let kind = activeBackendKind
        guard !kind.isProcessManagerManaged else { return }

        // Also check when in .error state — MLX or agent may have
        // recovered after the startup failure. Without this, the
        // "MLX failed to load after 3 attempts" banner sticks forever
        // even when MLX actually loaded successfully moments later.
        guard agentStatus == .running || agentStatus == .inferring || agentStatus.isError else { return }

        if isInferring {
            // During inference: PID check only — MLX can't respond to HTTP
            if let pid = mlxPID, isProcessAlive(pid) {
                mlxHealthFailures = 0  // Alive, just busy
            } else {
                // MLX process died mid-inference — this is bad
                print("[AgentManager] MLX process died during inference!")
                mlxHealthFailures = 10  // Immediate recovery
                await recoverMLX()
            }
        } else {
            // Not inferring: prefer PID as truth, HTTP as a soft signal.
            //
            // Why: llama-server blocks /health during prefill/generation.
            // Treating those stalls as "dead" used to make Clyde SIGKILL
            // a perfectly-busy backend process between tool-call turns,
            // leaving the active SSE stream truncated with no recovery.
            // So: if the kernel says the PID is alive, backend is alive
            // — full stop. HTTP stalls only matter when PID is gone.
            let pidAlive: Bool = {
                if let pid = mlxPID, isProcessAlive(pid) { return true }
                return false
            }()

            if pidAlive {
                // Process is running. HTTP may or may not answer right now;
                // that's fine — it's probably mid-prefill. Don't count it.
                mlxHealthFailures = 0
                // Opportunistic HTTP probe only to update the UI status label.
                let mlxAlive = await isPortResponding(mlxPort)
                if mlxAlive, mlxStatus != .running && mlxStatus != .busy {
                    mlxStatus = .running
                }
                return
            }

            // PID is gone — now HTTP matters as confirmation.
            let mlxAlive = await isPortResponding(mlxPort)
            if mlxAlive {
                // PID-less but HTTP answering? Likely a stale PID file from a
                // clean restart. Clear failures; leave recovery alone.
                mlxHealthFailures = 0
                if mlxStatus != .running && mlxStatus != .busy {
                    mlxStatus = .running
                }
            } else {
                mlxHealthFailures += 1
                if mlxHealthFailures >= 3 {
                    print("[AgentManager] MLX process dead (PID gone, HTTP dead) — recovering")
                    await recoverMLX()
                }
            }
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - SELF-HEALING
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    /// Public entry point for external callers (e.g. AppViewModel) to
    /// signal that the streaming connection dropped and the backend
    /// needs to be restarted. Called when Clyde's chat SSE stream ends
    /// without a clean finish_reason, which is unambiguous evidence
    /// the agent or MLX just died.
    ///
    /// Without this entry point, AppViewModel's only option is to show
    /// the user "Could not connect to the server" and wait for the next
    /// 5-second health check cycle to notice. That's bad UX because the
    /// banner stays empty during the wait, and the user has no idea
    /// recovery is in progress. This triggers it immediately.
    func requestRecovery(reason: String) async {
        print("[AgentManager] External recovery requested: \(reason)")
        // Only kick off recovery if we're in a state that warrants it.
        // If we're already restarting, there's nothing to do.
        switch agentStatus {
        case .restarting, .starting, .loadingModel, .waking, .stopping:
            print("[AgentManager] Recovery already in progress — skipping")
            return
        default:
            break
        }
        // Before restarting, probe if the backend is actually alive.
        // "Never kill what's working."
        let agentOk = await isPortResponding(agentPort)
        let backendPort = activeBackendPort
        let backendOk = await isPortResponding(backendPort)
        if agentOk && backendOk {
            print("[AgentManager] requestRecovery: backends actually alive (agent@\(agentPort), backend@\(backendPort)) — clearing state")
            transition(to: .running)
            if activeBackendKind == .mlxVLM || activeBackendKind == .mlxLM {
                mlxStatus = .running
            }
            startHealthCheck()
            // Backends are alive — trigger replay so the pending message
            // gets re-sent instead of leaving the user stranded.
            if pendingMessage != nil {
                print("[AgentManager] requestRecovery: posting replay notification for pending message")
                DispatchQueue.main.async {
                    NotificationCenter.default.post(name: .agentReadyForReplay, object: nil)
                }
            }
            return
        }

        // Genuinely broken — restart
        transition(to: .restarting)
        _ = await startFullStack()
    }

    /// Public signal that an SSE stream from the agent has produced output
    /// (a token, a tool start, anything). This is unambiguous proof that
    /// the entire backend stack — agent + MLX — is alive and serving
    /// requests successfully, regardless of what the recovery state machine
    /// thinks. Called from AppViewModel on every SSE delta.
    ///
    /// Why this exists: the recovery state machine can get into stale
    /// states where `mlxStatus = .recovering(...)` (or similar) is left
    /// hanging while the actual backend is fine — for example, when a
    /// previous health-check false-positive triggered `recoverMLX()`,
    /// `forceKillMLX()` failed to actually kill anything (stale PID), and
    /// `launchMLX()` then sat in `waitForPort` long enough that the next
    /// health cycle observed PID-alive again. The banner gets stuck on
    /// "Restarting MLX (attempt 1/5)..." even though the user is mid-stream.
    ///
    /// This method is the source-of-truth override: if we're streaming
    /// successfully, the backend is healthy by definition. Reset every
    /// counter and clear every "something is wrong" UI state.
    func notifyBackendHealthy() {
        // Reset failure counters — whatever previously made us think the
        // backend was unhealthy is no longer true.
        mlxHealthFailures = 0
        agentHealthFailures = 0

        // NOTE: Do NOT reset mlxRestartCount / agentRestartCount here.
        // This function is called on every SSE streaming event (hundreds
        // of times per stream). Resetting restart counters here means
        // if MLX crashes mid-stream, the counter resets and the app
        // retries forever instead of giving up after 5 attempts.
        // Restart counters should only reset on the 120s stability
        // timeout inside recoverAgent() / recoverMLX().

        // Clear any stale recovery / error UI state. We only override
        // states that are clearly contradicted by "the stream is working":
        // .recovering, .error, .starting, .loading, .stopped. We do NOT
        // touch .running, .busy (those are correct), or .waking (which
        // can legitimately be in flight even while a stream is delivering
        // a cached response — though in practice waking should also be
        // overridden by stream success).
        switch mlxStatus {
        case .recovering(_), .error(_), .starting, .loading, .stopped:
            print("[AgentManager] notifyBackendHealthy: clearing stale mlxStatus=\(mlxStatus) → .running")
            mlxStatus = .running
        default:
            break
        }

        // Same logic for agentStatus.
        switch agentStatus {
        case .restarting, .starting, .loadingModel, .waking, .error(_):
            print("[AgentManager] notifyBackendHealthy: clearing stale agentStatus=\(agentStatus) → .running")
            transition(to: .running)
        default:
            break
        }
    }

    private func recoverAgent() async {
        // Reset counter if stable for a while
        if Date().timeIntervalSince(lastAgentRestart) > restartCounterResetInterval {
            agentRestartCount = 0
        }

        agentRestartCount += 1
        guard agentRestartCount <= maxRestartAttempts else {
            transition(to: .error("Agent crashed \(agentRestartCount) times — giving up"))
            return
        }

        transition(to: .restarting)
        lastAgentRestart = Date()
        agentHealthFailures = 0

        print("[AgentManager] Agent recovery attempt \(agentRestartCount)/\(maxRestartAttempts)")

        // Kill and restart
        stopProcess(.agent)
        try? await Task.sleep(for: .seconds(1))

        let ok = await launchAgent()
        if ok {
            // Determine correct state from current MLX status, not a
            // stale prevState capture that might have changed during
            // the async recovery window.
            let restored: AgentStatus = (mlxStatus == .busy) ? .inferring : .running
            transition(to: restored)

            // Replay pending message if any
            if pendingMessage != nil {
                NotificationCenter.default.post(name: .agentReadyForReplay, object: nil)
            }
        } else {
            // Exponential backoff before next attempt
            let backoff = min(30.0, pow(2.0, Double(agentRestartCount)))
            print("[AgentManager] Agent restart failed, waiting \(Int(backoff))s before next attempt")
            transition(to: .error("Agent restart failed — retrying in \(Int(backoff))s"))
            try? await Task.sleep(for: .seconds(backoff))
            // Next health check cycle will trigger another recovery attempt
        }
    }

    private var mlxRecoveryStartTime: Date?

    private func recoverMLX() async {
        // Prevent concurrent recovery attempts — but allow a new attempt
        // if the previous one has been running for >120s (hung recovery).
        if let startTime = mlxRecoveryStartTime,
           Date().timeIntervalSince(startTime) < 120 {
            print("[AgentManager] MLX recovery already in progress — skipping")
            return
        }
        mlxRecoveryStartTime = Date()
        defer { mlxRecoveryStartTime = nil }

        // Clean up signal file
        try? FileManager.default.removeItem(atPath: "\(agentHome)/.mlx-needs-restart")

        if Date().timeIntervalSince(lastMLXRestart) > restartCounterResetInterval {
            mlxRestartCount = 0
        }

        mlxRestartCount += 1
        guard mlxRestartCount <= maxRestartAttempts else {
            mlxStatus = .error("MLX crashed \(mlxRestartCount) times — giving up")
            return
        }

        mlxStatus = .recovering("Restarting MLX (attempt \(mlxRestartCount)/\(maxRestartAttempts))...")
        lastMLXRestart = Date()
        mlxHealthFailures = 0

        print("[AgentManager] MLX recovery attempt \(mlxRestartCount)/\(maxRestartAttempts)")

        // Force kill — MLX doesn't have graceful shutdown
        forceKillMLX()
        try? await Task.sleep(for: .seconds(2))

        let ok = await launchMLX()
        if ok {
            print("[AgentManager] MLX self-healed successfully")
            // If we were inferring, transition back to running.
            // The agent is still alive (we don't restart agent for MLX crashes)
            // and will detect MLX is back via its polling loop.
            if agentStatus == .inferring {
                transition(to: .running)
            }

            if pendingMessage != nil {
                NotificationCenter.default.post(name: .agentReadyForReplay, object: nil)
            }
        } else {
            let backoff = min(30.0, pow(2.0, Double(mlxRestartCount)))
            mlxStatus = .error("MLX restart failed — retrying in \(Int(backoff))s")
            try? await Task.sleep(for: .seconds(backoff))
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - PROCESS MANAGEMENT
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    private enum ManagedProcess { case agent, mlx }

    private func stopProcess(_ which: ManagedProcess) {
        switch which {
        case .agent:
            // Graceful HTTP shutdown first
            requestGracefulShutdown(port: agentPort)
            agentProcess?.terminate()
            agentProcess = nil
            killOrphanedProcess(name: "agent.py")
            agentPID = nil
            removePIDFile("\(agentHome)/.agent-server.pid")

        case .mlx:
            mlxProcess?.terminate()
            mlxProcess = nil
            killOrphanedProcess(name: "llama-server")
            mlxPID = nil
            mlxStatus = .stopped
            removePIDFile("\(agentHome)/.mlx-server.pid")
        }
    }

    /// Force-kill MLX with SIGKILL. Used when MLX is stuck (zombie).
    private func forceKillMLX() {
        if let pid = mlxPID, isProcessAlive(pid) {
            print("[AgentManager] SIGKILL MLX PID \(pid)")
            kill(pid, SIGKILL)
        }
        mlxProcess = nil
        mlxPID = nil
        // Also pkill in case PID tracking was stale
        killOrphanedProcess(name: "llama-server", signal: SIGKILL)
        removePIDFile("\(agentHome)/.mlx-server.pid")
    }

    private func killOrphanedProcesses() {
        killOrphanedProcess(name: "agent.py")
        killOrphanedProcess(name: "llama-server")
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - WATCHDOG
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    /// Launches a detached watchdog that kills agent+MLX if Clyde dies (crash, SIGKILL, Xcode stop).
    private func launchWatchdog() {
        // Don't duplicate
        watchdogProcess?.terminate()
        watchdogProcess = nil

        let clydePID = ProcessInfo.processInfo.processIdentifier
        let home = agentHome

        let script = """
        #!/bin/bash
        CLYDE_PID=\(clydePID)
        AGENT_HOME="\(home)"
        AGENT_PORT=\(agentPort)
        while true; do
            sleep 2
            if ! kill -0 "$CLYDE_PID" 2>/dev/null; then
                # Clyde is gone — cleanup
                curl -s -X POST "http://127.0.0.1:$AGENT_PORT/shutdown" --max-time 3 2>/dev/null || true
                sleep 1
                [ -f "$AGENT_HOME/.agent-server.pid" ] && kill $(cat "$AGENT_HOME/.agent-server.pid") 2>/dev/null
                [ -f "$AGENT_HOME/.mlx-server.pid" ] && kill $(cat "$AGENT_HOME/.mlx-server.pid") 2>/dev/null
                rm -f "$AGENT_HOME/.agent-server.pid" "$AGENT_HOME/.mlx-server.pid" "$AGENT_HOME/.clyde.pid"
                sleep 2
                pkill -9 -f "agent.py" 2>/dev/null
                pkill -9 -f "llama-server" 2>/dev/null
                exit 0
            fi
        done
        """

        let scriptPath = "/tmp/clyde-watchdog.sh"
        try? script.write(toFile: scriptPath, atomically: true, encoding: .utf8)

        // Detach via perl setsid so watchdog survives Clyde's death
        let launcher = """
        #!/bin/bash
        exec perl -e 'use POSIX "setsid"; setsid(); exec @ARGV' /bin/bash "\(scriptPath)"
        """
        let launcherPath = "/tmp/clyde-watchdog-launcher.sh"
        try? launcher.write(toFile: launcherPath, atomically: true, encoding: .utf8)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [launcherPath]
        process.qualityOfService = .background
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice

        do {
            try process.run()
            watchdogProcess = process
            print("[AgentManager] Watchdog launched (monitors PID \(clydePID))")
        } catch {
            print("[AgentManager] Watchdog failed: \(error)")
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - SIGNAL HANDLERS
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - DATA DIRECTORY SETUP
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    /// Creates ~/Library/Application Support/Clyde/ and subdirectories.
    /// Installs bundled agent code if available and newer than what's deployed.
    /// Generates config.yaml with correct runtime paths.
    private func setupDataDirectory() {
        let fm = FileManager.default
        let subdirs = ["agent", "memory", "sessions", "transcripts", "graph", "models"]

        // Create directory structure
        for sub in subdirs {
            let path = "\(dataDir)/\(sub)"
            if !fm.fileExists(atPath: path) {
                try? fm.createDirectory(atPath: path, withIntermediateDirectories: true)
            }
        }

        // Install bundled agent code → App Support
        // Copy from Bundle.main/Resources/agent/ → dataDir/agent/
        if let bundledAgentDir = Bundle.main.resourcePath.map({ "\($0)/agent" }),
           fm.fileExists(atPath: bundledAgentDir) {
            installBundledAgent(from: bundledAgentDir)
        }

        // Generate config.yaml with runtime paths
        generateConfig()

        // Install backends.yaml for the router
        installBackendsConfig()

        print("[AgentManager] Data directory ready at \(dataDir)")
    }

    /// Copies bundled agent Python files to App Support if they're newer.
    private func installBundledAgent(from source: String) {
        let fm = FileManager.default
        let destDir = "\(dataDir)/agent"

        guard let contents = try? fm.contentsOfDirectory(atPath: source) else { return }

        for file in contents {
            let src = "\(source)/\(file)"
            let dst = "\(destDir)/\(file)"

            // Skip if destination is newer (user hasn't updated app)
            if fm.fileExists(atPath: dst) {
                let srcDate = (try? fm.attributesOfItem(atPath: src))?[.modificationDate] as? Date ?? .distantPast
                let dstDate = (try? fm.attributesOfItem(atPath: dst))?[.modificationDate] as? Date ?? .distantPast
                if dstDate >= srcDate { continue }
            }

            try? fm.removeItem(atPath: dst)
            try? fm.copyItem(atPath: src, toPath: dst)
        }
    }

    /// Generates config.yaml in the data directory with correct paths.
    private func generateConfig() {
        let configPath = "\(dataDir)/agent/config.yaml"

        let config = """
        server:
          host: 127.0.0.1
          port: \(agentPort)
        backend:
          url: http://localhost:\(llamaPort)
          model: clyde-qwen
        paths:
          agent_home: \(dataDir)
          memory_dir: \(dataDir)/memory
          transcripts_dir: \(dataDir)/transcripts
          sessions_dir: \(dataDir)/sessions
        memory:
          index_file: MEMORY.md
          max_index_lines: 200
          max_topic_chars: 4000
          max_total_instruction_chars: 12000
        session:
          compact_after_tokens: 120000
          preserve_recent_messages: 4
        agent:
          max_iterations: 80
          max_tokens: 32768
          temperature: 0.7
          tool_timeout: 30
        autodream:
          schedule: 03:00
          max_memories: 200
        """

        try? config.write(toFile: configPath, atomically: true, encoding: .utf8)
    }

    /// Installs backends.yaml routing config.
    private func installBackendsConfig() {
        let fm = FileManager.default
        let destPath = "\(dataDir)/backends.yaml"

        // If bundled version exists and is newer, install it
        if let bundled = Bundle.main.path(forResource: "backends_lazy", ofType: "yaml") {
            let srcDate = (try? fm.attributesOfItem(atPath: bundled))?[.modificationDate] as? Date ?? .distantPast
            let dstDate = (try? fm.attributesOfItem(atPath: destPath))?[.modificationDate] as? Date ?? .distantPast
            if srcDate > dstDate || !fm.fileExists(atPath: destPath) {
                try? fm.removeItem(atPath: destPath)
                try? fm.copyItem(atPath: bundled, toPath: destPath)
                return
            }
        }

        // Generate default if none exists
        guard !fm.fileExists(atPath: destPath) else { return }

        // Find the model GGUF — prefer bundled, fall back to known locations
        let home = fm.homeDirectoryForCurrentUser.path
        let bundledModel = Bundle.main.resourcePath.map { "\($0)/models/Qwen3.5-35B-A3B-Q4_K_M.gguf" } ?? ""
        let appSupportModel = "\(dataDir)/models/Qwen3.5-35B-A3B-Q4_K_M.gguf"
        let legacyModel = "\(home)/.clyde/models/Qwen3.5-35B-A3B-Q4_K_M.gguf"

        var modelPath = bundledModel
        if !fm.fileExists(atPath: modelPath) { modelPath = appSupportModel }
        if !fm.fileExists(atPath: modelPath) { modelPath = legacyModel }

        let backends = """
        default: clyde-qwen

        backends:
          llamacpp-local:
            type: llamacpp
            endpoint: http://127.0.0.1:\(llamaPort)
            launch:
              cmd: \(llamaServerPath) --model {model} --host 127.0.0.1 --port \(llamaPort) --ctx-size 131072 --n-gpu-layers 999 --cache-type-k q4_0 --cache-type-v q4_0 --flash-attn on --mmap --jinja --parallel 1 --cont-batching
              idle_timeout: 600
              log_file: /tmp/clyde-llamacpp.log
              health_path: /health
              startup_timeout: 90
              port: \(llamaPort)

        routes:
          clyde-qwen:
            backend: llamacpp-local
            model: \(modelPath)
            profile: qwen3_5-moe
        """

        try? backends.write(toFile: destPath, atomically: true, encoding: .utf8)
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    private var signalSources: [DispatchSourceSignal] = []

    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                print("[AgentManager] Caught signal \(sig), cleaning up...")
                MainActor.assumeIsolated { self?.onQuit() }
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.5) { exit(0) }
            }
            source.resume()
            signalSources.append(source)
        }
    }

    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    // MARK: - HELPERS
    // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    /// HTTP health check with short timeout (5s).
    private func isPortResponding(_ port: Int) async -> Bool {
        // Use /health for both agent and llama.cpp backend
        let path = "/health"
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return false }
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5
        config.timeoutIntervalForResource = 5
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        do {
            let (data, response) = try await session.data(from: url)
            let httpOk = (response as? HTTPURLResponse).map { (200...299).contains($0.statusCode) } ?? false

            // If this is the agent's /health endpoint, also check MLX status
            if httpOk && port == agentPort {
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let mlxStatus = json["mlx"] as? String,
                   mlxStatus != "ok" {
                    // Agent is up but reports MLX is down — trigger fast recovery
                    if self.mlxStatus != .stopped && mlxRecoveryStartTime == nil {
                        print("[AgentManager] Agent reports MLX is down: \(mlxStatus)")
                        mlxHealthFailures += 2  // Accelerate detection
                    }
                }
            }

            return httpOk
        } catch {
            return false
        }
    }

    /// Check if a process is alive by PID (no HTTP needed).
    private func isProcessAlive(_ pid: pid_t) -> Bool {
        return kill(pid, 0) == 0
    }

    /// Poll until port responds or timeout.
    private func waitForPort(_ port: Int, timeout: Int) async -> Bool {
        for _ in 0..<timeout {
            if await isPortResponding(port) { return true }
            try? await Task.sleep(for: .seconds(1))
        }
        return false
    }

    /// Wait until agentStatus is .running or .inferring, or timeout.
    private func waitForReady(timeout: Int) async -> Bool {
        for _ in 0..<timeout {
            if agentStatus == .running || agentStatus == .inferring { return true }
            if case .error = agentStatus { return false }
            try? await Task.sleep(for: .seconds(1))
        }
        return agentStatus == .running || agentStatus == .inferring
    }

    /// Wait until agentStatus matches one of the given states, or timeout.
    private func waitForState(oneOf labels: [AgentStatus], timeout: Int) async -> Bool {
        for _ in 0..<timeout {
            if labels.contains(agentStatus) { return true }
            try? await Task.sleep(for: .seconds(1))
        }
        return false
    }

    /// Fire-and-forget POST to /shutdown for graceful agent cleanup.
    private func requestGracefulShutdown(port: Int) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/shutdown") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 2)
    }

    private func killOrphanedProcess(name: String, signal: Int32 = SIGTERM) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        task.arguments = signal == SIGKILL ? ["-9", "-f", name] : ["-f", name]
        try? task.run()
        task.waitUntilExit()
    }

    /// Aggressively clean up any leftover llama-server process from a previous
    /// Clyde session that didn't shut down cleanly (e.g. force-quit).
    private func cleanupLeftoverMLX() async {
        killOrphanedProcess(name: "llama-server", signal: SIGKILL)

        // Wait for port to be free. Poll every 200ms for up to 5s.
        for _ in 0..<25 {
            try? await Task.sleep(for: .milliseconds(200))
            if !(await isPortResponding(mlxPort)) {
                break
            }
        }

        // Extra grace for GPU memory release. The Metal allocator needs a
        // moment to reclaim the buffers from the dead process before a new
        // one can request them. Without this, the new MLX sometimes hits
        // an "out of memory" failure even though the old one is dead.
        try? await Task.sleep(for: .milliseconds(500))

        // Drop any stale PID file so the new launch starts clean
        removePIDFile("\(agentHome)/.mlx-server.pid")
    }

    /// Clean up any leftover agent.py process from a previous Clyde
    /// session. Uses SIGKILL directly — not SIGTERM — because:
    ///
    ///   1. SIGTERM on an agent that's already in graceful-shutdown
    ///      state (triggered by its own watchdog when the parent PID
    ///      changed) is a no-op: it just sets `_shutting_down = True`
    ///      which is already true.
    ///   2. The agent's graceful shutdown won't actually exit while
    ///      it has in-flight requests. If we send SIGTERM and then
    ///      politely wait for the port to free, we can wait forever.
    ///   3. The agent saves sessions on every write, not only on
    ///      shutdown. There's no data loss from SIGKILL.
    ///
    /// So SIGKILL immediately, wait for the port to actually free
    /// (polling up to 5 seconds), then continue.
    private func cleanupLeftoverAgent() async {
        // Kill hard — no graceful shutdown required
        killOrphanedProcess(name: "agent.py", signal: SIGKILL)

        // Poll for the port to actually be free. On macOS the TCP
        // socket can stay in TIME_WAIT briefly, but the port should
        // stop responding to HTTP within a few hundred ms of SIGKILL.
        for _ in 0..<25 {
            try? await Task.sleep(for: .milliseconds(200))
            if !(await isPortResponding(agentPort)) {
                break
            }
        }

        // Re-kill as belt-and-braces in case SIGKILL didn't catch a
        // process that was launched by a different pgrep-visible name
        // (e.g. if python3 was spawning subprocesses).
        killOrphanedProcess(name: "agent.py", signal: SIGKILL)
        try? await Task.sleep(for: .milliseconds(200))

        removePIDFile("\(agentHome)/.agent-server.pid")
    }

    private func writePIDFile(_ path: String, pid: Int32) {
        try? String(pid).write(toFile: path, atomically: true, encoding: .utf8)
    }

    private func readPIDFile(_ path: String) -> pid_t? {
        guard let str = try? String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              let pid = Int32(str) else { return nil }
        return pid
    }

    private func removePIDFile(_ path: String) {
        try? FileManager.default.removeItem(atPath: path)
    }

    /// Open a log file for writing, creating it if needed.
    private func openLog(_ path: String) -> FileHandle? {
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        let handle = FileHandle(forWritingAtPath: path)
        handle?.seekToEndOfFile()
        return handle
    }
}

// MARK: - Notification Names

extension Notification.Name {
    static let agentReadyForReplay = Notification.Name("agentReadyForReplay")
}
