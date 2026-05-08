//
//  BackendKind.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-14.
//
//  Backend abstraction for the inference server Clyde talks to.
//  AgentManager's legacy name `MLXStatus` still exists for binary compat
//  but the only production backend is llama.cpp with Qwen 3.6.
//
//  Supported backends:
//    • Clyde       — llama.cpp, Qwen 3.6 35B-A3B GGUF on :8810 (primary, 48GB Macs)
//    • Clyde Flash — SwiftLM with TurboQuant + SSD streaming on :8812 (16GB Macs, ≤12GB RAM)
//    • ollama      — http://localhost:11434/v1
//    • custom      — any OpenAI-compatible endpoint (user-supplied)
//

import Foundation

/// Which inference server Clyde is pointing at.
/// Stored in @AppStorage("backend_kind") as the `rawValue`.
enum BackendKind: String, CaseIterable, Identifiable, Codable, Equatable {
    // Legacy cases kept for decode compat — they resolve to llamaCpp at runtime.
    case mlxVLM    = "mlx-vlm"
    case mlxLM     = "mlx-lm"
    case llamaCpp  = "llama-cpp"
    case swiftLM   = "swiftlm"     // SwiftLM: TurboQuant + SSD streaming for 16GB Macs
    case ollama    = "ollama"
    case custom    = "custom"

    // Phase 3 — forked-mlx-flash-based Clyde tiers. Unique rawValues so old
    // installs that decoded one of the legacy cases don't collide.
    case mlxVLMPro    = "mlx-vlm-pro"      // Pro tier, mlx_vlm_turbo_server (no Flash streaming)
    case mlxFlashVLM  = "mlx-flash-vlm"    // Flash tier, forked mlx-flash + turbo3 + mlx_vlm

    var id: String { rawValue }

    /// Decode compat: "mlx-flash" stored in UserDefaults resolves to .swiftLM.
    init?(compatRawValue raw: String) {
        switch raw {
        case "mlx-flash": self = .swiftLM
        default: self.init(rawValue: raw)
        }
    }

    /// Human-facing label shown in Settings.
    var displayName: String {
        switch self {
        case .mlxVLM, .mlxLM, .llamaCpp: return "Clyde"
        case .swiftLM:                    return "Clyde Flash (SwiftLM, legacy)"
        case .mlxVLMPro:                  return "Clyde Pro (mlx-vlm turbo3)"
        case .mlxFlashVLM:                return "Clyde Flash (mlx-flash turbo3)"
        case .ollama:                     return "Ollama"
        case .custom:                     return "Custom endpoint"
        }
    }

    /// Default port per backend kind.
    /// Legacy MLX cases redirect to llama.cpp port.
    var defaultPort: Int {
        switch self {
        case .mlxVLM, .mlxLM, .llamaCpp: return 8810
        case .swiftLM:                    return 8812
        case .mlxVLMPro:                  return 8814
        case .mlxFlashVLM:                return 8815
        case .ollama:                     return 11434
        case .custom:                     return 8810
        }
    }

    /// Default launch executable.
    var defaultExecutable: String? {
        switch self {
        case .mlxVLM, .mlxLM, .llamaCpp: return "/opt/homebrew/bin/llama-server"
        case .swiftLM:                    return "$HOME/.clyde/swiftlm/bin/SwiftLM"
        // mlx-flash's venv Python runs both Pro (mlx_vlm_turbo_server.py) and
        // Flash (scripts/flash_vlm_server.py) entry points.
        case .mlxVLMPro, .mlxFlashVLM:   return "$HOME/Documents/Clyde App Project/mlx-flash/.venv/bin/python"
        case .ollama, .custom:            return nil  // externally managed
        }
    }

    /// Whether Clyde is expected to own the process lifecycle.
    var isManagedByClyde: Bool {
        defaultExecutable != nil
    }

    /// Whether ProcessManager in the Python agent manages this backend.
    /// When true, AgentManager should NOT launch or health-check the backend
    /// port — only the agent port (8801). ProcessManager handles startup,
    /// health polling, idle reaping, and recovery for these backends.
    var isProcessManagerManaged: Bool {
        switch self {
        case .llamaCpp, .mlxVLMPro, .mlxFlashVLM:
            return true
        default:
            return false
        }
    }

    /// Resolve a BackendKind from the agent's route metadata.
    static func from(backendType: String?, backendId: String) -> BackendKind {
        let id = backendId.lowercased()
        // Clyde-branded backend IDs (Phase 3) — match by ID first so they
        // map to the correct kind even though their `type:` in backends.yaml
        // is the generic "openai".
        if id == "clyde-pro-mlxvlm" { return .mlxVLMPro }
        if id == "clyde-flash-mlxvlm" { return .mlxFlashVLM }

        let key = (backendType ?? backendId).lowercased()
        switch key {
        case "llamacpp", "llama-cpp", "llama.cpp",
             "mlx", "mlx-vlm", "mlx_vlm", "mlx-lm", "mlx_lm":
            return .llamaCpp
        case "swiftlm", "swift-lm", "swift_lm":
            return .swiftLM
        case "ollama":
            return .ollama
        default:
            // Clyde-branded backend IDs from backends.yaml
            if id == "clyde-local" || id == "clyde-remote" { return .llamaCpp }
            if id == "clyde-flash" || id.contains("swiftlm") { return .swiftLM }
            if id.contains("llama") { return .llamaCpp }
            if id.contains("ollama") { return .ollama }
            return .custom
        }
    }

    /// OpenAI-compatible base URL given a port.
    func baseURL(port: Int) -> URL {
        URL(string: "http://localhost:\(port)/v1")!
    }
}

/// Full config used to launch / connect to a backend.
struct BackendConfig: Equatable, Codable {
    var kind: BackendKind
    var port: Int
    var modelPath: String       // path or repo-id or model tag
    var executable: String?     // nil when Clyde doesn't manage the process
    var extraArgs: [String]     // additional CLI args for managed backends
    var environment: [String: String] = [:]  // env vars for managed process

    /// Memberwise initializer (required because the custom Decodable init suppresses the auto-generated one).
    init(kind: BackendKind, port: Int, modelPath: String, executable: String?, extraArgs: [String], environment: [String: String] = [:]) {
        self.kind = kind
        self.port = port
        self.modelPath = modelPath
        self.executable = executable
        self.extraArgs = extraArgs
        self.environment = environment
    }

    // Custom decoder: environment key may be absent in old serialized data.
    enum CodingKeys: String, CodingKey {
        case kind, port, modelPath, executable, extraArgs, environment
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(BackendKind.self, forKey: .kind)
        port = try c.decode(Int.self, forKey: .port)
        modelPath = try c.decode(String.self, forKey: .modelPath)
        executable = try c.decodeIfPresent(String.self, forKey: .executable)
        extraArgs = try c.decode([String].self, forKey: .extraArgs)
        environment = try c.decodeIfPresent([String: String].self, forKey: .environment) ?? [:]
    }

    /// Default llama.cpp config for Qwen 3.6 35B-A3B MoE.
    static func defaultLlamaCpp(modelPath: String) -> BackendConfig {
        BackendConfig(
            kind: .llamaCpp,
            port: BackendKind.llamaCpp.defaultPort,
            modelPath: modelPath,
            executable: BackendKind.llamaCpp.defaultExecutable,
            extraArgs: ["-m", modelPath, "--port", "8810", "--host", "127.0.0.1"]
        )
    }

    /// SwiftLM config for 16GB Macs with TurboQuant + SSD expert streaming.
    /// 262K context via --ctx-size, 3-bit KV via --turbo-kv, ≤12GB target.
    static func defaultSwiftLM(model: String) -> BackendConfig {
        BackendConfig(
            kind: .swiftLM,
            port: BackendKind.swiftLM.defaultPort,
            modelPath: model,
            executable: "$HOME/.clyde/swiftlm/bin/SwiftLM",
            extraArgs: ["--model", model,
                        "--ctx-size", "262144",
                        "--stream-experts",
                        "--ssd-prefetch",
                        "--turbo-kv",
                        "--max-tokens", "16384",
                        "--thinking",
                        "--port", "8812", "--host", "127.0.0.1"]
        )
    }

    /// Legacy alias — resolves to llama.cpp now.
    static func defaultMLX(modelPath: String) -> BackendConfig {
        defaultLlamaCpp(modelPath: modelPath)
    }

    /// Legacy alias.
    static func llamaCppQwen(modelPath: String) -> BackendConfig {
        defaultLlamaCpp(modelPath: modelPath)
    }

    /// Clyde Pro (Phase 3) — mlx-vlm + native TurboQuantKVCache(bits=3). For
    /// ≥32GB Macs. Same vision-capable model as the Flash tier.
    /// Launch: `<venv>/bin/python <clyde-benchmarks>/mlx_vlm_turbo_server.py`.
    static func defaultMlxVLMPro(model: String) -> BackendConfig {
        BackendConfig(
            kind: .mlxVLMPro,
            port: BackendKind.mlxVLMPro.defaultPort,
            modelPath: model,
            executable: BackendKind.mlxVLMPro.defaultExecutable,
            extraArgs: [
                "$HOME/Documents/Clyde App Project/clyde-benchmarks/mlx_vlm_turbo_server.py",
                "--model", model,
                "--host", "127.0.0.1",
                "--port", "8814",
                "--kv-bits", "3",
            ]
        )
    }

    /// Clyde Flash (Phase 3) — forked mlx-flash + TurboQuantKVCache(bits=3)
    /// + Flash weight streaming + prefix cache persistence. For 16-24GB Macs.
    /// Launch: `<venv>/bin/python <mlx-flash>/scripts/flash_vlm_server.py`.
    static func defaultMlxFlashVLM(model: String) -> BackendConfig {
        BackendConfig(
            kind: .mlxFlashVLM,
            port: BackendKind.mlxFlashVLM.defaultPort,
            modelPath: model,
            executable: BackendKind.mlxFlashVLM.defaultExecutable,
            extraArgs: [
                "$HOME/Documents/Clyde App Project/mlx-flash/scripts/flash_vlm_server.py",
                "--model", model,
                "--host", "127.0.0.1",
                "--port", "8815",
                "--kv-quant-mode", "turbo3",
                "--ram", "4",
                "--ctx-size", "262144",
                "--prefix-cache-dir",
                "$HOME/Library/Application Support/Clyde/prefix-cache",
            ]
        )
    }
}

/// Backend-agnostic status surface. The underlying enum in
/// AgentManager is still called `MLXStatus` for historical reasons;
/// this typealias lets callers say `BackendStatus`.
typealias BackendStatus = AgentManager.MLXStatus
