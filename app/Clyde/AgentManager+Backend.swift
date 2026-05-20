//
//  AgentManager+Backend.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-14.
//
//  Backend facade over AgentManager. Internal vars still use legacy
//  "mlx" names for compat, but the only production backend is
//  llama.cpp with Qwen 3.6 35B-A3B MoE.
//

import Foundation
import Combine

extension AgentManager {

    /// Lifecycle status of the active inference backend (llama.cpp).
    /// Legacy name `MLXStatus` kept for type compat.
    var backendStatus: MLXStatus { mlxStatus }

    /// Best-guess BackendKind inferred from the currently configured
    /// server command. Until AgentManager stores a BackendConfig
    /// directly, this is the pragmatic read path.
    var activeBackendKind: BackendKind {
        // The internal field is private, so we read from UserDefaults
        // which the Settings UI writes. Default = llama.cpp (the active
        // production backend). MLX is only used when explicitly selected.
        let raw = UserDefaults.standard.string(forKey: "backend_kind") ?? BackendKind.llamaCpp.rawValue
        return BackendKind(compatRawValue: raw) ?? .llamaCpp
    }

    /// Port the active backend is listening on.
    var activeBackendPort: Int {
        let stored = UserDefaults.standard.integer(forKey: "backend_port")
        return stored > 0 ? stored : activeBackendKind.defaultPort
    }

    /// Human-readable one-liner for diagnostics / debug dashboard.
    var backendDescription: String {
        "\(activeBackendKind.displayName) @ :\(activeBackendPort)"
    }
}
