//
//  ModelsSettingsPane.swift
//  Clyde
//
//  LM Studio-style model loader + detailed settings panel.
//
//  Redesigned 2026-04-16:
//    Top half:  Searchable model list (routes from /v1/routes) with
//               backend type badges, load status, context window.
//               Click "Load" to lazy-load via ProcessManager.
//    Bottom:    When a model is loaded, a full settings panel appears
//               with context window management, compaction, sampling,
//               phase overrides, and backend settings — all read from
//               and written back to the agent via /v1/settings.
//

import SwiftUI
import Combine

// MARK: - Data Types

/// Shape returned by the agent's /v1/routes endpoint (expanded).
/// Uses custom decoder to handle YAML's bool/string ambiguity
/// (YAML parses `off` as boolean false, not string "off").
struct RouteInfo: Identifiable, Equatable, Codable {
    let name: String
    let backend: String
    let backendType: String?
    let model: String
    let modelShort: String?
    let profile: String?
    let family: String?
    let healthy: Bool
    let loaded: Bool?
    let latencyMs: Double?
    let notes: String?
    let contextWindow: Int?
    let thinkingDirective: String?
    let thinkingModeDefault: String?
    let samplingDefaults: [String: Double]?
    let managed: Bool?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, backend, model, profile, family, healthy, loaded, notes, managed
        case backendType = "backend_type"
        case modelShort = "model_short"
        case latencyMs = "latency_ms"
        case contextWindow = "context_window"
        case thinkingDirective = "thinking_directive"
        case thinkingModeDefault = "thinking_mode_default"
        case samplingDefaults = "sampling_defaults"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        backend = try c.decode(String.self, forKey: .backend)
        backendType = try c.decodeIfPresent(String.self, forKey: .backendType)
        model = try c.decode(String.self, forKey: .model)
        modelShort = try c.decodeIfPresent(String.self, forKey: .modelShort)
        profile = try c.decodeIfPresent(String.self, forKey: .profile)
        family = try c.decodeIfPresent(String.self, forKey: .family)
        healthy = (try? c.decode(Bool.self, forKey: .healthy)) ?? false
        loaded = try? c.decode(Bool.self, forKey: .loaded)
        latencyMs = try c.decodeIfPresent(Double.self, forKey: .latencyMs)
        notes = try c.decodeIfPresent(String.self, forKey: .notes)
        contextWindow = try c.decodeIfPresent(Int.self, forKey: .contextWindow)
        // thinking_directive: might be string or bool (YAML "off" → false)
        thinkingDirective = Self.decodeFlexibleString(from: c, key: .thinkingDirective)
        // thinking_mode_default: YAML "off" → bool false, "on" → bool true
        thinkingModeDefault = Self.decodeFlexibleString(from: c, key: .thinkingModeDefault)
        samplingDefaults = try c.decodeIfPresent([String: Double].self, forKey: .samplingDefaults)
        managed = try? c.decode(Bool.self, forKey: .managed)
    }

    /// Decode a value that may be String, Bool, or null in JSON.
    /// Converts Bool true→"on", false→"off".
    private static func decodeFlexibleString(
        from c: KeyedDecodingContainer<CodingKeys>, key: CodingKeys
    ) -> String? {
        if let s = try? c.decode(String.self, forKey: key) { return s }
        if let b = try? c.decode(Bool.self, forKey: key) { return b ? "on" : "off" }
        return nil
    }

    // Manual memberwise init for creating instances in fallback code
    init(name: String, backend: String, backendType: String?, model: String,
         modelShort: String?, profile: String?, family: String?, healthy: Bool,
         loaded: Bool?, latencyMs: Double?, notes: String?, contextWindow: Int?,
         thinkingDirective: String?, thinkingModeDefault: String?,
         samplingDefaults: [String: Double]?, managed: Bool?) {
        self.name = name; self.backend = backend; self.backendType = backendType
        self.model = model; self.modelShort = modelShort; self.profile = profile
        self.family = family; self.healthy = healthy; self.loaded = loaded
        self.latencyMs = latencyMs; self.notes = notes
        self.contextWindow = contextWindow
        self.thinkingDirective = thinkingDirective
        self.thinkingModeDefault = thinkingModeDefault
        self.samplingDefaults = samplingDefaults; self.managed = managed
    }
}

struct RoutesResponse: Codable {
    let defaultRoute: String?
    let routes: [RouteInfo]

    enum CodingKeys: String, CodingKey {
        case defaultRoute = "default_route"
        case routes
    }
}

/// Shape returned by /v1/settings
struct AgentSettings: Codable {
    var agent: AgentConfig?
    var session: SessionConfig?
    var profile: ProfileConfig?
    var backend: BackendConfig?
    var env: EnvConfig?

    struct AgentConfig: Codable {
        var maxIterations: Int?
        var maxTokens: Int?
        var temperature: Double?
        var toolTimeout: Int?

        enum CodingKeys: String, CodingKey {
            case maxIterations = "max_iterations"
            case maxTokens = "max_tokens"
            case temperature
            case toolTimeout = "tool_timeout"
        }
    }

    struct SessionConfig: Codable {
        var compactAfterTokens: Int?
        var preserveRecentMessages: Int?

        enum CodingKeys: String, CodingKey {
            case compactAfterTokens = "compact_after_tokens"
            case preserveRecentMessages = "preserve_recent_messages"
        }
    }

    struct ProfileConfig: Codable {
        var id: String?
        var family: String?
        var contextWindow: Int?
        var thinkingDirective: String?
        var thinkingModeDefault: String?
        var toolSchemaStrategy: String?
        var samplingDefaults: [String: Double]?
        var phaseOverrides: [String: PhaseOverride]?
        var notes: String?

        enum CodingKeys: String, CodingKey {
            case id, family, notes
            case contextWindow = "context_window"
            case thinkingDirective = "thinking_directive"
            case thinkingModeDefault = "thinking_mode_default"
            case toolSchemaStrategy = "tool_schema_strategy"
            case samplingDefaults = "sampling_defaults"
            case phaseOverrides = "phase_overrides"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decodeIfPresent(String.self, forKey: .id)
            family = try c.decodeIfPresent(String.self, forKey: .family)
            contextWindow = try c.decodeIfPresent(Int.self, forKey: .contextWindow)
            toolSchemaStrategy = try c.decodeIfPresent(String.self, forKey: .toolSchemaStrategy)
            samplingDefaults = try c.decodeIfPresent([String: Double].self, forKey: .samplingDefaults)
            phaseOverrides = try c.decodeIfPresent([String: PhaseOverride].self, forKey: .phaseOverrides)
            notes = try c.decodeIfPresent(String.self, forKey: .notes)
            // Handle YAML bool/string ambiguity
            thinkingDirective = Self.flexString(c, .thinkingDirective)
            thinkingModeDefault = Self.flexString(c, .thinkingModeDefault)
        }

        private static func flexString(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> String? {
            if let s = try? c.decode(String.self, forKey: key) { return s }
            if let b = try? c.decode(Bool.self, forKey: key) { return b ? "on" : "off" }
            return nil
        }
    }

    struct PhaseOverride: Codable {
        var temperature: Double?
        var topP: Double?
        var topK: Int?
        var repetitionPenalty: Double?
        var maxTokensFloor: Int?

        enum CodingKeys: String, CodingKey {
            case temperature
            case topP = "top_p"
            case topK = "top_k"
            case repetitionPenalty = "repetition_penalty"
            case maxTokensFloor = "max_tokens_floor"
        }
    }

    struct BackendConfig: Codable {
        var managed: Bool?
        var idleTimeout: Int?
        var startupTimeout: Int?
        var running: Bool?
        var modelLoaded: String?

        enum CodingKeys: String, CodingKey {
            case managed, running
            case idleTimeout = "idle_timeout"
            case startupTimeout = "startup_timeout"
            case modelLoaded = "model_loaded"
        }
    }

    struct EnvConfig: Codable {
        var thinkingMode: String?
        var tempStructured: String?
        var tempFree: String?
        var topPOverride: String?
        var repetitionPenalty: String?
        var repetitionContextSize: String?

        enum CodingKeys: String, CodingKey {
            case thinkingMode = "thinking_mode"
            case tempStructured = "temp_structured"
            case tempFree = "temp_free"
            case topPOverride = "top_p_override"
            case repetitionPenalty = "repetition_penalty"
            case repetitionContextSize = "repetition_context_size"
        }
    }
}

// MARK: - View Model

@MainActor
final class ModelsSettingsViewModel: ObservableObject {
    @Published var routes: [RouteInfo] = []
    @Published var defaultRoute: String? = nil
    @Published var isLoading = false
    @Published var lastError: String? = nil
    @Published var lastRefreshed: Date? = nil
    @Published var agentReachable: Bool = false
    @Published var settings: AgentSettings? = nil
    @Published var isSaving = false

    var endpoint: String

    init(endpoint: String) {
        self.endpoint = endpoint
        // Seed the list immediately with known routes so users see models the
        // moment Settings opens, even before the first agent round-trip. The
        // real list replaces this on the first successful refresh().
        self.routes = Self.fallbackRoutes()
    }

    func refresh() async {
        isLoading = true
        lastError = nil
        defer { isLoading = false }
        do {
            let routes = try await Self.fetchRoutes(endpoint: endpoint)
            self.routes = routes.routes
            self.defaultRoute = routes.defaultRoute
            self.lastRefreshed = Date()
            self.agentReachable = true
        } catch RoutesError.endpointMissing {
            do {
                let models = try await Self.fetchModels(endpoint: endpoint)
                self.routes = models.map {
                    RouteInfo(name: $0, backend: "legacy", backendType: nil,
                              model: $0, modelShort: nil, profile: nil, family: nil,
                              healthy: true, loaded: nil, latencyMs: nil,
                              notes: nil, contextWindow: nil,
                              thinkingDirective: nil, thinkingModeDefault: nil,
                              samplingDefaults: nil, managed: nil)
                }
                self.defaultRoute = models.first
                self.lastRefreshed = Date()
                self.agentReachable = true
            } catch {
                self.lastError = "Could not reach agent"
                self.agentReachable = false
                if self.routes.isEmpty { self.routes = Self.fallbackRoutes() }
            }
        } catch {
            self.lastError = error.localizedDescription
            self.agentReachable = false
            if self.routes.isEmpty { self.routes = Self.fallbackRoutes() }
        }
    }

    /// Phase 3 UX: when the agent is offline, populate the model list with a
    /// known-good fallback so users still see what's available and can click
    /// Load to bring a route up. Mirrors the routes defined in the shipped
    /// backends.yaml; refreshed from the real agent once it connects.
    static func fallbackRoutes() -> [RouteInfo] {
        func mk(_ name: String, backend: String, backendType: String,
                model: String, family: String?, ctx: Int) -> RouteInfo {
            RouteInfo(name: name, backend: backend, backendType: backendType,
                      model: model, modelShort: nil, profile: family,
                      family: family, healthy: false, loaded: false,
                      latencyMs: nil, notes: nil, contextWindow: ctx,
                      thinkingDirective: nil, thinkingModeDefault: nil,
                      samplingDefaults: nil, managed: true)
        }
        return [
            mk("clyde", backend: "clyde-local", backendType: "llamacpp",
               model: "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
               family: "qwen3_5", ctx: 131072),
            mk("clyde-pro", backend: "clyde-pro-mlxvlm",
               backendType: "llamacpp",
               model: "mlx-community/Qwen3.6-35B-A3B-4bit",
               family: "qwen3_5", ctx: 262144),
            mk("clyde-flash", backend: "clyde-flash-mlxvlm",
               backendType: "llamacpp",
               model: "mlx-community/Qwen3.6-35B-A3B-4bit",
               family: "qwen3_5", ctx: 262144),
        ]
    }

    func fetchSettings(for route: String? = nil) async {
        let clean = endpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        var urlStr = "\(clean)/v1/settings"
        if let r = route { urlStr += "?route=\(r)" }
        guard let url = URL(string: urlStr) else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 4
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            self.settings = try JSONDecoder().decode(AgentSettings.self, from: data)
        } catch {
            // Settings endpoint may not exist on older agents
        }
    }

    func saveSettings(_ payload: [String: Any]) async -> Bool {
        isSaving = true
        defer { isSaving = false }
        let clean = endpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(clean)/v1/settings") else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 5
        do {
            req.httpBody = try JSONSerialization.data(withJSONObject: payload)
            let (_, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                // Refresh settings after save
                await fetchSettings()
                return true
            }
            return false
        } catch {
            return false
        }
    }

    enum RoutesError: Error { case endpointMissing }

    static func fetchRoutes(endpoint: String) async throws -> RoutesResponse {
        let clean = endpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(clean)/v1/routes") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 4
        let (data, response) = try await URLSession.shared.data(for: req)
        if let http = response as? HTTPURLResponse, http.statusCode == 404 {
            throw RoutesError.endpointMissing
        }
        return try JSONDecoder().decode(RoutesResponse.self, from: data)
    }

    static func fetchModels(endpoint: String) async throws -> [String] {
        let clean = endpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(clean)/v1/models") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        let (data, _) = try await URLSession.shared.data(for: req)
        struct ModelsResponse: Decodable {
            struct Model: Decodable { let id: String }
            let data: [Model]
        }
        let mr = try JSONDecoder().decode(ModelsResponse.self, from: data)
        return mr.data.map(\.id)
    }
}

// MARK: - Main Pane

struct ModelsSettingsPane: View {
    @EnvironmentObject var viewModel: AppViewModel
    @AppStorage("api_endpoint") private var apiEndpoint = "http://127.0.0.1:8801"
    @AppStorage("model_name") private var modelName = "clyde-qwen"
    @AppStorage("temperature") private var temperature = 0.7
    @AppStorage("max_tokens") private var maxTokens = 4096.0
    @AppStorage("use_custom_endpoint") private var useCustomEndpoint = false
    @AppStorage("custom_llm_endpoint") private var customLLMEndpoint = ""
    @AppStorage("backend_kind") private var backendKindRaw = BackendKind.llamaCpp.rawValue
    @AppStorage("backend_port") private var backendPort = BackendKind.llamaCpp.defaultPort

    @StateObject private var model = ModelsSettingsViewModel(endpoint: "http://127.0.0.1:8801")
    @State private var startingAgent = false
    @State private var showEndpointConfig = false
    @State private var searchText = ""
    @State private var showAdvanced = false
    @State private var loadingRoute: String? = nil

    // Accordion: only one settings section open at a time
    enum SettingsSection: String { case generation, context, backend, phases, none }
    @State private var expandedSection: SettingsSection = .none

    // Local copies of settings for sliders (synced from /v1/settings)
    @State private var localCompactAfterTokens: Double = 120000
    @State private var localPreserveRecent: Double = 4
    @State private var localMaxIterations: Double = 80
    @State private var localToolTimeout: Double = 30
    @State private var localIdleTimeout: Double = 600
    @State private var localTopP: Double = 0.95
    @State private var localTopK: Double = 40
    @State private var localRepPenalty: Double = 1.05
    @State private var thinkingMode: String = "off"

    private var backendKindSelection: Binding<BackendKind> {
        Binding(
            get: { BackendKind(rawValue: backendKindRaw) ?? .llamaCpp },
            set: { newKind in
                let oldKind = BackendKind(rawValue: backendKindRaw) ?? .llamaCpp
                backendKindRaw = newKind.rawValue
                if backendPort == oldKind.defaultPort {
                    backendPort = newKind.defaultPort
                }
            }
        )
    }

    private var activeRoute: RouteInfo? {
        model.routes.first(where: { $0.name == modelName })
            ?? model.routes.first(where: { $0.name == model.defaultRoute })
    }

    private var filteredRoutes: [RouteInfo] {
        if searchText.isEmpty { return model.routes }
        let q = searchText.lowercased()
        return model.routes.filter {
            $0.name.lowercased().contains(q)
            || $0.backend.lowercased().contains(q)
            || ($0.modelShort ?? $0.model).lowercased().contains(q)
            || ($0.family ?? "").lowercased().contains(q)
            || ($0.profile ?? "").lowercased().contains(q)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Section 1: Model Loader
            modelLoaderSection

            if model.agentReachable, activeRoute != nil {
                Divider().padding(.vertical, 8)

                // Section 2: Detailed Settings (only when a model is loaded)
                settingsSection
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 16)
        .frame(minWidth: 520, idealWidth: 580)
        .task {
            model.endpoint = agentURL
            // Re-apply custom endpoint if one was saved (agent loses
            // in-memory routes on restart)
            if isCustomEndpointActive {
                apiEndpoint = agentURL
                await applyEndpoint()
            }
            await model.refresh()
            await model.fetchSettings(for: modelName)
            syncLocalFromSettings()
        }
        .onChange(of: apiEndpoint) { _, newValue in
            // Always talk to the agent, never directly to an LLM server.
            // applyEndpoint() handles routing custom URLs through the agent.
            let effective = (newValue.contains("127.0.0.1:8801") || newValue.contains("localhost:8801"))
                ? newValue : agentURL
            model.endpoint = effective
            viewModel.updateAPISettings()
        }
        .onChange(of: temperature) { _, _ in
            viewModel.updateAPISettings()
        }
        .onChange(of: maxTokens) { _, _ in
            viewModel.updateAPISettings()
        }
    }

    // MARK: - Model Loader Section

    private var modelLoaderSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header row
            HStack {
                Text("Models")
                    .font(.title3.weight(.semibold))
                Spacer()
                if model.isLoading {
                    ProgressView().controlSize(.small)
                }
                Button {
                    Task { await model.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .imageScale(.small)
                }
                .buttonStyle(.borderless)
                .help("Refresh model list")

                Button {
                    showEndpointConfig.toggle()
                } label: {
                    Image(systemName: "gearshape")
                        .imageScale(.small)
                }
                .buttonStyle(.borderless)
                .help("Configure endpoint")
            }

            // Search bar
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Filter models…", text: $searchText)
                    .textFieldStyle(.plain)
                if !searchText.isEmpty {
                    Button {
                        searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                }
            }
            .padding(8)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(.quaternary.opacity(0.5))
            )

            // Endpoint config (collapsible)
            if showEndpointConfig || useCustomEndpoint {
                endpointConfigSection
            }

            // Phase 3 UX: always show the model list. When the agent is
            // offline, we overlay a slim status chip + "Start Agent" button
            // inline instead of blocking the whole pane with a big banner.
            if !model.agentReachable && !model.isLoading {
                inlineAgentStatus
            }

            modelList
        }
    }

    /// One-line inline status chip for when the agent is offline — replaces
    /// the old blocking banner so users can still see routes + click Load,
    /// which will start the agent on demand.
    private var inlineAgentStatus: some View {
        HStack(spacing: 8) {
            Circle().fill(.red).frame(width: 7, height: 7)
            Text("Agent offline")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
            Text("— \(apiEndpoint)")
                .font(.caption2)
                .foregroundStyle(.secondary.opacity(0.7))
                .lineLimit(1)
            Spacer()
            Button {
                Task { await startAgentAndRefresh() }
            } label: {
                HStack(spacing: 4) {
                    if startingAgent {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "play.fill")
                    }
                    Text(startingAgent ? "Starting…" : "Start Agent")
                }
                .font(.caption)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(.green)
            .disabled(startingAgent)

            Button("Custom…") {
                useCustomEndpoint = true
                showEndpointConfig = true
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .font(.caption)
            .foregroundStyle(.blue)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(.red.opacity(0.05))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .strokeBorder(.red.opacity(0.18), lineWidth: 0.5)
        )
    }

    // MARK: - Model List

    private var modelList: some View {
        VStack(spacing: 2) {
            ForEach(filteredRoutes) { route in
                modelRow(route)
            }

            if filteredRoutes.isEmpty && !searchText.isEmpty {
                Text("No models matching \"\(searchText)\"")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
        }
    }

    private func modelRow(_ route: RouteInfo) -> some View {
        let isActive = route.name == modelName
        let isLoaded = route.loaded ?? false
        let isLoading = loadingRoute == route.name
        let isDefault = (route.name == model.defaultRoute)

        return HStack(spacing: 10) {
            // Status indicator
            Circle()
                .fill(isLoaded ? .green : (route.healthy ? .orange.opacity(0.6) : .red.opacity(0.4)))
                .frame(width: 8, height: 8)

            // Model info
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(route.name)
                        .font(.body.weight(isActive ? .semibold : .regular))
                    if isDefault {
                        Image(systemName: "star.fill")
                            .font(.caption2)
                            .foregroundStyle(.yellow)
                            .help("Default route — picked when Clyde starts a new chat")
                    }
                    backendBadge(route)
                }
                HStack(spacing: 8) {
                    // Show backend type (llama.cpp / mlx-vlm / mlx-flash) instead
                    // of the model profile name (qwen3_5) which is confusing to users.
                    Text(backendDisplayName(route))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    if let ctx = route.contextWindow {
                        Text("\(ctx / 1024)K ctx")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if let lat = route.latencyMs {
                        Text("\(Int(lat))ms")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Spacer()

            // Load / Active button
            if isActive && isLoaded {
                HStack(spacing: 4) {
                    Circle().fill(.green).frame(width: 6, height: 6)
                    Text("Loaded")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.green)
                }
            } else if isLoading {
                HStack(spacing: 4) {
                    ProgressView().controlSize(.mini)
                    Text("Loading…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                Button(isActive ? "Reload" : "Load") {
                    Task { await loadModel(route) }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .font(.caption)
            }

            // Set-as-default button: persists this route as the default in
            // backends.yaml via /v1/default_route. Hidden when this route
            // already IS the default (the star next to the name covers it).
            if !isDefault {
                Button {
                    Task { await setDefaultRoute(route) }
                } label: {
                    Image(systemName: "star")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .foregroundStyle(.secondary)
                .help("Set as default — Clyde will load this route on startup")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isActive ? Color.accentColor.opacity(0.08) : Color.clear)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(isActive ? Color.accentColor.opacity(0.25) : Color.clear, lineWidth: 1)
        )
    }

    private func backendDisplayName(_ route: RouteInfo) -> String {
        let bid = route.backend.lowercased()
        if bid.contains("flash") { return "mlx-flash" }
        if bid.contains("mlxvlm") || bid.contains("mlx-vlm") || bid.contains("pro") { return "mlx-vlm" }
        if bid.contains("local") || bid.contains("llama") { return "llama.cpp" }
        if bid.contains("remote") { return "remote" }
        if bid.contains("ollama") { return "ollama" }
        if let bt = route.backendType {
            if bt == "llamacpp" { return "llama.cpp" }
            if bt == "openai" { return "openai" }
            return bt
        }
        return route.family ?? route.backend
    }

    private func backendBadge(_ route: RouteInfo) -> some View {
        let type = route.backendType ?? route.backend
        let backendId = route.backend.lowercased()
        let (label, color): (String, Color) = {
            // Phase 3: Clyde-branded openai-typed backends get dedicated badges.
            if backendId == "clyde-pro-mlxvlm" { return ("Pro turbo3", .green) }
            if backendId == "clyde-flash-mlxvlm" { return ("Flash turbo3", .cyan) }
            switch type.lowercased() {
            case "llamacpp", "mlx": return ("Clyde", .green)
            case "swiftlm", "swift-lm", "swift_lm": return ("Flash (SwiftLM)", .cyan)
            case "ollama":          return ("Ollama", .purple)
            case "openai":          return ("API", .orange)
            default:                return (type.uppercased(), .gray)
            }
        }()
        return Text(label)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .fill(color.opacity(0.12))
            )
    }

    // MARK: - Set as Default

    private func setDefaultRoute(_ route: RouteInfo) async {
        let cleanBase = apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(cleanBase)/v1/default_route") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 8
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["route": route.name])
        _ = try? await URLSession.shared.data(for: req)
        // Refresh to pick up the new default_route the agent just persisted.
        await model.refresh()
    }

    // MARK: - Load Model

    private func loadModel(_ route: RouteInfo) async {
        loadingRoute = route.name
        defer { loadingRoute = nil }

        // ── Exclusive mode: kill ALL running backends before switching ──
        // Belt-and-suspenders on top of ProcessManager's own exclusive mode.
        let cleanBase = apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if let stopURL = URL(string: "\(cleanBase)/v1/stop_backends") {
            var stopReq = URLRequest(url: stopURL)
            stopReq.httpMethod = "POST"
            stopReq.timeoutInterval = 10
            _ = try? await URLSession.shared.data(for: stopReq)
        }

        // Switch the active model name
        modelName = route.name
        let resolvedKind = BackendKind.from(backendType: route.backendType, backendId: route.backend)
        backendKindRaw = resolvedKind.rawValue
        backendPort = resolvedKind.defaultPort
        viewModel.updateAPISettings()

        // Tell AgentManager about the swap so the pill shows "Loading
        // model..." until the new backend's port comes up. Without this,
        // status stays .running while the new backend boots silently and
        // the user thinks it's frozen. Kick off in parallel with the ping.
        let swappingMgr = viewModel.agentManager
        let swapTask = Task { @MainActor in
            await swappingMgr?.notifyModelSwap(newBackendKind: resolvedKind, newPort: resolvedKind.defaultPort)
        }

        // Trigger lazy-load by sending a lightweight request to the agent
        // (the agent's chat_completions handler calls ProcessManager.ensure_running)
        let clean = apiEndpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if let url = URL(string: "\(clean)/v1/chat/completions") {
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.timeoutInterval = 180  // model load can take up to 2-3 minutes
            let body: [String: Any] = [
                "model": route.name,
                "messages": [["role": "user", "content": "ping"]],
                "max_tokens": 1,
                "stream": false
            ]
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            _ = try? await URLSession.shared.data(for: req)
        }

        // Wait for AgentManager swap to settle (port healthy or error).
        await swapTask.value

        await model.refresh()
        await model.fetchSettings(for: modelName)
        syncLocalFromSettings()
    }

    // MARK: - Offline Banner

    private var offlineBanner: some View {
        cardContainer {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Circle().fill(.red).frame(width: 8, height: 8)
                    Text("Agent Offline")
                        .font(.headline)
                    Spacer()
                }
                Text("Could not reach \(apiEndpoint)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let err = model.lastError {
                    Text(err)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .lineLimit(2)
                }
                HStack(spacing: 12) {
                    Button {
                        Task { await startAgentAndRefresh() }
                    } label: {
                        HStack(spacing: 5) {
                            if startingAgent {
                                ProgressView().controlSize(.mini)
                            } else {
                                Image(systemName: "play.fill")
                            }
                            Text(startingAgent ? "Starting…" : "Start Agent")
                        }
                        .font(.caption.weight(.medium))
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .tint(.green)
                    .disabled(startingAgent)

                    Button("Use Custom Endpoint") {
                        useCustomEndpoint = true
                        showEndpointConfig = true
                    }
                    .buttonStyle(.borderless)
                    .controlSize(.small)
                    .font(.caption)
                    .foregroundStyle(.blue)
                }
            }
        }
    }

    // MARK: - Endpoint Config

    private var endpointConfigSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            LabeledContent {
                HStack(spacing: 8) {
                    TextField("http://127.0.0.1:8801", text: displayEndpoint)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit {
                            Task { await applyEndpoint() }
                        }
                    Button("Test") {
                        Task { await applyEndpoint() }
                    }
                    .disabled(model.isLoading)
                }
            } label: {
                Text("Endpoint:")
                    .font(.caption)
            }

            if isCustomEndpointActive {
                HStack(spacing: 4) {
                    Image(systemName: "bolt.fill")
                        .foregroundStyle(.green)
                        .font(.caption2)
                    Text("Routing through Clyde agent to external LLM")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(.quaternary.opacity(0.3))
        )
    }

    private let agentURL = "http://127.0.0.1:8801"

    /// Shows customLLMEndpoint when active, otherwise apiEndpoint.
    /// Writes go to apiEndpoint (which applyEndpoint() then routes).
    private var displayEndpoint: Binding<String> {
        Binding(
            get: { isCustomEndpointActive ? customLLMEndpoint : apiEndpoint },
            set: { apiEndpoint = $0 }
        )
    }

    private var isCustomEndpointActive: Bool {
        !customLLMEndpoint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func applyEndpoint() async {
        let entered = apiEndpoint.trimmingCharacters(in: .whitespacesAndNewlines)

        if entered.contains("127.0.0.1:8801") || entered.contains("localhost:8801") || entered.isEmpty {
            // Normal agent mode — clear any custom endpoint
            model.endpoint = entered.isEmpty ? agentURL : entered
            viewModel.updateAPISettings()
            // Clear custom route on agent
            if let url = URL(string: "\(agentURL)/v1/custom_endpoint") {
                var req = URLRequest(url: url)
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try? JSONSerialization.data(withJSONObject: ["endpoint": ""])
                try? await URLSession.shared.data(for: req)
            }
            await model.refresh()
            return
        }

        // Custom LLM URL — keep agent on localhost, route to this endpoint
        // 1. Save the custom URL separately
        customLLMEndpoint = entered
        // 2. Point Clyde back at agent so all chat/skills/tools work
        apiEndpoint = agentURL
        model.endpoint = agentURL
        viewModel.updateAPISettings()
        // 3. POST the custom URL to the agent's routing layer
        if let url = URL(string: "\(agentURL)/v1/custom_endpoint") {
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["endpoint": entered])
            do {
                let (_, response) = try await URLSession.shared.data(for: req)
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    await model.refresh()
                }
            } catch {
                print("[Settings] custom endpoint failed: \(error)")
            }
        }
    }

    // MARK: - Settings Section (shown when model loaded)

    private var settingsSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            // ── Active Model Header ──
            if let route = activeRoute {
                activeModelHeader(route)
            }

            // ── Generation Parameters ──
            collapsibleGroup("Generation", icon: "dial.low", section: .generation) {
                sliderRow("Temperature", value: $temperature,
                          range: 0...2, step: 0.1, format: "%.1f",
                          width: 36)

                sliderRow("Max Tokens", value: $maxTokens,
                          range: 256...131072, step: 1024,
                          format: { "\(Int($0).formatted())" },
                          width: 62)

                sliderRow("Top P", value: $localTopP,
                          range: 0...1, step: 0.01, format: "%.2f",
                          width: 42)

                sliderRow("Top K", value: $localTopK,
                          range: 0...200, step: 1, format: "%.0f",
                          width: 42)

                sliderRow("Repetition Penalty", value: $localRepPenalty,
                          range: 1.0...2.0, step: 0.01, format: "%.2f",
                          width: 42)
            }

            // ── Context Window Management ──
            collapsibleGroup("Context Window", icon: "rectangle.stack", section: .context) {
                if let ctx = activeRoute?.contextWindow {
                    infoRow("Context Window", value: "\(ctx.formatted()) tokens")
                }
                if let profile = model.settings?.profile?.id {
                    infoRow("Profile", value: profile)
                }

                sliderRow("Compact After", value: $localCompactAfterTokens,
                          range: 10000...250000, step: 5000,
                          format: { "\(Int($0 / 1000))K" },
                          width: 42)

                sliderRow("Preserve Messages", value: $localPreserveRecent,
                          range: 1...10, step: 1, format: "%.0f",
                          width: 30)

                sliderRow("Max Iterations", value: $localMaxIterations,
                          range: 1...200, step: 1, format: "%.0f",
                          width: 42)

                sliderRow("Tool Timeout", value: $localToolTimeout,
                          range: 5...120, step: 5, format: "%.0f s",
                          width: 42)
            }

            // ── Thinking & Backend ──
            collapsibleGroup("Backend", icon: "server.rack", section: .backend) {
                HStack {
                    Text("Thinking Mode")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(width: 130, alignment: .leading)
                    Spacer()
                    Picker("", selection: $thinkingMode) {
                        Text("Off").tag("off")
                        Text("On").tag("on")
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 120)
                }

                sliderRow("Idle Timeout", value: $localIdleTimeout,
                          range: 60...3600, step: 60,
                          format: { "\(Int($0 / 60))m" },
                          width: 36)

                if let managed = model.settings?.backend?.managed, managed {
                    infoRow("Managed", value: "ProcessManager controls lifecycle")
                }

                if let running = model.settings?.backend?.running {
                    infoRow("Status", value: running ? "Running" : "Stopped")
                }
            }

            // ── Phase Overrides (collapsible) ──
            phaseOverridesSection

            // ── Save / Apply ──
            HStack {
                Spacer()
                if model.isSaving {
                    ProgressView().controlSize(.small)
                }
                Button("Apply Settings") {
                    Task { await applySettings() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(model.isSaving)
            }
            .padding(.top, 4)
        }
    }

    // MARK: - Active Model Header

    private func activeModelHeader(_ route: RouteInfo) -> some View {
        HStack(spacing: 10) {
            Circle()
                .fill((route.loaded ?? false) ? .green : .orange)
                .frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(route.name)
                        .font(.headline)
                    backendBadge(route)
                }
                Text(route.modelShort ?? route.model)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            if let lat = route.latencyMs {
                detailChip("\(Int(lat))ms", icon: "bolt")
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.accentColor.opacity(0.06))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(Color.accentColor.opacity(0.2), lineWidth: 0.5)
        )
    }

    // MARK: - Phase Overrides

    private var phaseOverridesSection: some View {
        let isOpen = expandedSection == .phases
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    expandedSection = isOpen ? .none : .phases
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: isOpen ? "chevron.down" : "chevron.right")
                        .imageScale(.small)
                        .frame(width: 12)
                        .foregroundStyle(.secondary)
                    Image(systemName: "waveform.path")
                        .imageScale(.small)
                        .foregroundStyle(.secondary)
                    Text("Phase-Aware Sampling")
                        .font(.body.weight(.medium))
                    Spacer()
                    Text("Per-phase overrides")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.vertical, 4)

            if isOpen, let phases = model.settings?.profile?.phaseOverrides {
                VStack(spacing: 1) {
                    // Header
                    HStack(spacing: 0) {
                        Text("Phase")
                            .frame(width: 80, alignment: .leading)
                        Text("Temp")
                            .frame(width: 50, alignment: .trailing)
                        Text("Top P")
                            .frame(width: 50, alignment: .trailing)
                        Text("Top K")
                            .frame(width: 50, alignment: .trailing)
                        Text("Token Floor")
                            .frame(width: 80, alignment: .trailing)
                    }
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)

                    ForEach(["scoping", "research", "synthesis", "writing", "review"], id: \.self) { phase in
                        if let po = phases[phase] {
                            phaseRow(phase, override: po)
                        }
                    }
                }
                .padding(8)
                .padding(.top, 4)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(.quaternary.opacity(0.3))
                )

                Text("The agent selects the tighter of global and phase settings automatically.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .padding(.leading, 4)
                    .padding(.top, 4)
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(.quaternary.opacity(0.15))
        )
    }

    private func phaseRow(_ name: String, override po: AgentSettings.PhaseOverride) -> some View {
        HStack(spacing: 0) {
            Text(name.capitalized)
                .frame(width: 80, alignment: .leading)
                .font(.caption.weight(.medium))
            Text(po.temperature.map { String(format: "%.2f", $0) } ?? "—")
                .frame(width: 50, alignment: .trailing)
                .font(.caption.monospacedDigit())
            Text(po.topP.map { String(format: "%.2f", $0) } ?? "—")
                .frame(width: 50, alignment: .trailing)
                .font(.caption.monospacedDigit())
            Text(po.topK.map { "\($0)" } ?? "—")
                .frame(width: 50, alignment: .trailing)
                .font(.caption.monospacedDigit())
            Text(po.maxTokensFloor.map { "\($0.formatted())" } ?? "—")
                .frame(width: 80, alignment: .trailing)
                .font(.caption.monospacedDigit())
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 3)
        .background(
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(.quaternary.opacity(0.15))
        )
    }

    // MARK: - Settings Helpers

    private func settingsGroup<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.body.weight(.medium))
                .foregroundStyle(.primary)
            content()
        }
    }

    private func collapsibleGroup<Content: View>(
        _ title: String, icon: String, section: SettingsSection,
        @ViewBuilder content: () -> Content
    ) -> some View {
        let isOpen = expandedSection == section
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    expandedSection = isOpen ? .none : section
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: isOpen ? "chevron.down" : "chevron.right")
                        .imageScale(.small)
                        .frame(width: 12)
                        .foregroundStyle(.secondary)
                    Image(systemName: icon)
                        .imageScale(.small)
                        .foregroundStyle(.secondary)
                    Text(title)
                        .font(.body.weight(.medium))
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.vertical, 4)

            if isOpen {
                VStack(alignment: .leading, spacing: 10) {
                    content()
                }
                .padding(.top, 8)
                .padding(.leading, 4)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(.quaternary.opacity(0.15))
        )
    }

    private func sliderRow(_ label: String, value: Binding<Double>,
                           range: ClosedRange<Double>, step: Double,
                           format: String, width: CGFloat,
                           help: String? = nil) -> some View {
        sliderRow(label, value: value, range: range, step: step,
                  format: { String(format: format, $0) }, width: width, help: help)
    }

    private func sliderRow(_ label: String, value: Binding<Double>,
                           range: ClosedRange<Double>, step: Double,
                           format: @escaping (Double) -> String,
                           width: CGFloat, help: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text(label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 130, alignment: .leading)
                Slider(value: value, in: range, step: step)
                Text(format(value.wrappedValue))
                    .monospacedDigit()
                    .font(.caption)
                    .frame(width: width, alignment: .trailing)
            }
            if let help = help {
                Text(help)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .padding(.leading, 130)
            }
        }
    }

    private func infoRow(_ label: String, value: String) -> some View {
        HStack {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 130, alignment: .leading)
            Text(value)
                .font(.caption.monospacedDigit())
            Spacer()
        }
    }

    // MARK: - Apply Settings

    private func applySettings() async {
        let payload: [String: Any] = [
            "agent": [
                "max_tokens": Int(maxTokens),
                "temperature": temperature,
                "max_iterations": Int(localMaxIterations),
                "tool_timeout": Int(localToolTimeout),
            ],
            "session": [
                "compact_after_tokens": Int(localCompactAfterTokens),
                "preserve_recent_messages": Int(localPreserveRecent),
            ],
            "env": [
                "thinking_mode": thinkingMode,
                "top_p_override": String(format: "%.2f", localTopP),
                "repetition_penalty": String(format: "%.2f", localRepPenalty),
            ],
            "backend": [
                "idle_timeout": Int(localIdleTimeout),
            ]
        ]
        let ok = await model.saveSettings(payload)
        if ok {
            syncLocalFromSettings()
        }
    }

    private func syncLocalFromSettings() {
        guard let s = model.settings else { return }
        if let v = s.session?.compactAfterTokens { localCompactAfterTokens = Double(v) }
        if let v = s.session?.preserveRecentMessages { localPreserveRecent = Double(v) }
        if let v = s.agent?.maxIterations { localMaxIterations = Double(v) }
        if let v = s.agent?.toolTimeout { localToolTimeout = Double(v) }
        if let v = s.backend?.idleTimeout { localIdleTimeout = Double(v) }
        if let v = s.env?.thinkingMode { thinkingMode = v }
        // Sampling from profile
        if let sd = s.profile?.samplingDefaults {
            if let v = sd["top_p"] { localTopP = v }
            if let v = sd["top_k"] { localTopK = v }
            if let v = sd["repetition_penalty"] { localRepPenalty = v }
        }
        // Env overrides take precedence
        if let v = s.env?.topPOverride, let d = Double(v) { localTopP = d }
        if let v = s.env?.repetitionPenalty, let d = Double(v) { localRepPenalty = d }
    }

    // MARK: - Card Container

    private func cardContainer<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(.background)
                    .shadow(color: .black.opacity(0.08), radius: 2, y: 1)
            }
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(.quaternary, lineWidth: 0.5)
            }
    }

    private func detailChip(_ text: String, icon: String) -> some View {
        HStack(spacing: 3) {
            Image(systemName: icon)
                .imageScale(.small)
            Text(text)
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(.secondary.opacity(0.1))
        )
    }

    // MARK: - Start Agent

    private func startAgentAndRefresh() async {
        guard let manager = viewModel.agentManager else {
            model.lastError = "AgentManager is not wired up in this build."
            return
        }
        startingAgent = true
        defer { startingAgent = false }
        _ = await manager.ensureReady()
        try? await Task.sleep(nanoseconds: 750_000_000)
        await model.refresh()
    }
}

// MARK: - Live switching helper

extension AppViewModel {
    @MainActor
    func liveSwitchModel(to modelName: String) {
        updateAPISettings()
    }
}
