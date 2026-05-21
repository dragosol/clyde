//
//  PersistenceManager.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//

import Foundation
import Combine

@MainActor
class PersistenceManager: ObservableObject {
    static let shared = PersistenceManager()
    
    private let fileManager = FileManager.default
    private lazy var appSupportURL: URL? = {
        guard let url = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let clydeURL = url.appendingPathComponent("Clyde")
        
        // Create directory if needed
        if !fileManager.fileExists(atPath: clydeURL.path) {
            try? fileManager.createDirectory(at: clydeURL, withIntermediateDirectories: true)
        }
        
        return clydeURL
    }()
    
    private lazy var conversationsURL: URL? = {
        guard let base = appSupportURL else { return nil }
        let url = base.appendingPathComponent("conversations")

        if !fileManager.fileExists(atPath: url.path) {
            try? fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        }

        return url
    }()

    /// Root directory for output files: .../Application Support/Clyde/files/
    private lazy var filesURL: URL? = {
        guard let base = appSupportURL else { return nil }
        let url = base.appendingPathComponent("files")
        if !fileManager.fileExists(atPath: url.path) {
            try? fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        }
        return url
    }()

    /// Root directory for graph data: .../Application Support/Clyde/graph/
    private lazy var graphURL: URL? = {
        guard let base = appSupportURL else { return nil }
        let url = base.appendingPathComponent("graph")
        if !fileManager.fileExists(atPath: url.path) {
            try? fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        }
        return url
    }()

    private init() {}
    
    // MARK: - Conversations
    
    func loadConversations() -> [Conversation] {
        guard let conversationsURL = conversationsURL else {
            print("Failed to get conversations directory")
            return []
        }
        
        do {
            let files = try fileManager.contentsOfDirectory(at: conversationsURL, includingPropertiesForKeys: nil)
            let conversations = files.compactMap { url -> Conversation? in
                guard url.pathExtension == "json" else { return nil }
                return loadConversation(from: url)
            }
            return conversations.sorted { $0.updatedAt > $1.updatedAt }
        } catch {
            print("Failed to load conversations: \(error)")
            return []
        }
    }
    
    func loadConversation(from url: URL) -> Conversation? {
        do {
            let data = try Data(contentsOf: url)
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let conversation = try decoder.decode(Conversation.self, from: data)
            return conversation
        } catch {
            print("Failed to load conversation from \(url): \(error)")
            return nil
        }
    }
    
    func saveConversation(_ conversation: Conversation) {
        guard let conversationsURL = conversationsURL else {
            print("Failed to get conversations directory")
            return
        }

        let fileURL = conversationsURL.appendingPathComponent("\(conversation.id.uuidString).json")

        do {
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(conversation)
            // Atomic write: write to temp file then rename to avoid partial writes
            let tempURL = fileURL.appendingPathExtension("tmp")
            try data.write(to: tempURL)
            _ = try fileManager.replaceItemAt(fileURL, withItemAt: tempURL)
        } catch {
            print("Failed to save conversation: \(error)")
        }
    }
    
    func deleteConversation(_ conversation: Conversation) {
        guard let conversationsURL = conversationsURL else { return }

        let fileURL = conversationsURL.appendingPathComponent("\(conversation.id.uuidString).json")
        try? fileManager.removeItem(at: fileURL)

        // Also delete associated output files
        deleteConversationFiles(for: conversation.id)
    }

    // MARK: - Output File Management

    /// Per-conversation file storage directory
    private func conversationFilesDir(for conversationId: UUID) -> URL? {
        guard let filesURL = filesURL else { return nil }
        let dir = filesURL.appendingPathComponent(conversationId.uuidString)
        if !fileManager.fileExists(atPath: dir.path) {
            try? fileManager.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }

    /// Copy a file from its original location into app sandbox storage.
    /// Returns a FileReference if successful, nil otherwise.
    func copyFileToStorage(from sourceURL: URL, conversationId: UUID) -> FileReference? {
        guard let destDir = conversationFilesDir(for: conversationId) else { return nil }

        let fileName = sourceURL.lastPathComponent
        // Avoid name collisions by appending a short UUID suffix
        let ext = sourceURL.pathExtension
        let baseName = (fileName as NSString).deletingPathExtension
        let uniqueName = "\(baseName)_\(UUID().uuidString.prefix(6)).\(ext)"

        let destURL = destDir.appendingPathComponent(uniqueName)

        do {
            try fileManager.copyItem(at: sourceURL, to: destURL)

            let attrs = try fileManager.attributesOfItem(atPath: destURL.path)
            let fileSize = (attrs[.size] as? Int64) ?? 0
            let mimeType = MIMEType.from(extension: ext)

            return FileReference(
                fileName: fileName,
                mimeType: mimeType,
                relativePath: "\(conversationId.uuidString)/\(uniqueName)",
                originalPath: sourceURL.path,
                fileSize: fileSize
            )
        } catch {
            print("[PersistenceManager] Failed to copy file to storage: \(error)")
            return nil
        }
    }

    /// Resolve a FileReference to its absolute URL in the sandbox.
    /// `nonisolated` so SwiftUI views can call this synchronously.
    nonisolated func resolveFileURL(for fileRef: FileReference) -> URL? {
        guard let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
        let url = appSupport.appendingPathComponent("Clyde/files/\(fileRef.relativePath)")
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    // MARK: - Graph Data Persistence

    /// Save graph data for a conversation. Uses the same atomic-write pattern
    /// as conversation persistence to avoid partial writes on crash.
    func saveGraphData(_ graphData: GraphData, for conversationId: UUID) {
        guard let graphURL = graphURL else { return }
        let fileURL = graphURL.appendingPathComponent("\(conversationId.uuidString).json")

        do {
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(graphData)
            let tempURL = fileURL.appendingPathExtension("tmp")
            try data.write(to: tempURL)
            _ = try fileManager.replaceItemAt(fileURL, withItemAt: tempURL)
        } catch {
            print("[PersistenceManager] Failed to save graph data: \(error)")
        }
    }

    /// Load persisted graph data for a conversation, if any.
    func loadGraphData(for conversationId: UUID) -> GraphData? {
        guard let graphURL = graphURL else { return nil }
        let fileURL = graphURL.appendingPathComponent("\(conversationId.uuidString).json")
        guard fileManager.fileExists(atPath: fileURL.path) else { return nil }

        do {
            let data = try Data(contentsOf: fileURL)
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            return try decoder.decode(GraphData.self, from: data)
        } catch {
            print("[PersistenceManager] Failed to load graph data: \(error)")
            return nil
        }
    }

    /// Delete persisted graph data for a conversation.
    func deleteGraphData(for conversationId: UUID) {
        guard let graphURL = graphURL else { return }
        let fileURL = graphURL.appendingPathComponent("\(conversationId.uuidString).json")
        try? fileManager.removeItem(at: fileURL)
    }

    /// Delete all output files for a conversation.
    func deleteConversationFiles(for conversationId: UUID) {
        guard let filesURL = filesURL else { return }
        let dir = filesURL.appendingPathComponent(conversationId.uuidString)
        if fileManager.fileExists(atPath: dir.path) {
            try? fileManager.removeItem(at: dir)
        }
    }
    
    // MARK: - Auto-Title Generation
    
    func generateTitle(from message: String) -> String {
        // Smart title generation: extract the core intent from the user's message
        let cleaned = message.trimmingCharacters(in: .whitespacesAndNewlines)

        if cleaned.isEmpty {
            return "New Conversation"
        }

        // Remove common greeting prefixes
        var intent = cleaned
        let greetingPrefixes = ["hey ", "hi ", "hello ", "hey, ", "hi, ", "hello, ",
                                 "can you ", "could you ", "would you ", "please ",
                                 "i need you to ", "i want you to ", "i'd like you to "]
        for prefix in greetingPrefixes {
            if intent.lowercased().hasPrefix(prefix) {
                intent = String(intent.dropFirst(prefix.count))
                // Capitalize first letter
                if let first = intent.first {
                    intent = first.uppercased() + intent.dropFirst()
                }
                break
            }
        }

        // Find first sentence
        if let endIndex = intent.firstIndex(where: { $0 == "." || $0 == "?" || $0 == "!" }) {
            let sentence = String(intent[..<endIndex])
            if sentence.count <= 50 && sentence.count >= 3 {
                return sentence
            }
        }

        // Truncate to 50 chars at word boundary
        if intent.count <= 50 {
            return intent
        }

        // Find last space before 47 chars for clean word break
        let prefix = String(intent.prefix(47))
        if let lastSpace = prefix.lastIndex(of: " ") {
            return String(prefix[..<lastSpace]) + "..."
        }

        return prefix + "..."
    }

    /// Generate a refined title using the first few messages of the conversation.
    /// Called after 3 exchanges to produce a more descriptive title.
    func generateRefinedTitle(from messages: [ChatMessage]) -> String {
        // Use the first user message + first assistant response to derive a better title
        let userMessages = messages.filter { $0.role == .user }.prefix(2)
        let assistantMessages = messages.filter { $0.role == .assistant && !$0.content.isEmpty }.prefix(1)

        var context = ""
        for msg in userMessages {
            context += msg.content.prefix(200) + " "
        }
        if let first = assistantMessages.first {
            // Take the first line of assistant response as topic hint
            let firstLine = first.content.components(separatedBy: .newlines).first ?? ""
            context += firstLine.prefix(100)
        }

        // Extract key noun phrases heuristically
        return generateTitle(from: String(context.prefix(200)))
    }
}

// MARK: - Settings

extension PersistenceManager {
    private enum SettingsKey: String {
        case apiEndpoint = "api_endpoint"
        case modelName = "model_name"
        case temperature = "temperature"
        case maxTokens = "max_tokens"
        case showThinking = "show_thinking"
        case showToolCalls = "show_tool_calls"
        case autoTitle = "auto_title"
        case soundEffects = "sound_effects"
        case theme = "theme"
    }
    
    var apiEndpoint: String {
        get {
            let raw = UserDefaults.standard.string(forKey: SettingsKey.apiEndpoint.rawValue) ?? "http://127.0.0.1:8801"
            // Normalize: macOS resolves "localhost" to IPv6 ::1 first, but
            // the Python agent binds to 127.0.0.1 only → connection refused.
            return raw.replacingOccurrences(of: "://localhost:", with: "://127.0.0.1:")
        }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.apiEndpoint.rawValue) }
    }
    
    var modelName: String {
        get { UserDefaults.standard.string(forKey: SettingsKey.modelName.rawValue) ?? "clyde-qwen" }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.modelName.rawValue) }
    }
    
    var temperature: Double {
        get {
            let value = UserDefaults.standard.double(forKey: SettingsKey.temperature.rawValue)
            return value == 0 ? 0.7 : value
        }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.temperature.rawValue) }
    }
    
    var maxTokens: Int {
        get {
            let value = UserDefaults.standard.integer(forKey: SettingsKey.maxTokens.rawValue)
            return value == 0 ? 4096 : value
        }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.maxTokens.rawValue) }
    }
    
    var showThinking: Bool {
        get { UserDefaults.standard.bool(forKey: SettingsKey.showThinking.rawValue) }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.showThinking.rawValue) }
    }
    
    var showToolCalls: Bool {
        get { 
            // Default to true if not set
            UserDefaults.standard.object(forKey: SettingsKey.showToolCalls.rawValue) as? Bool ?? true
        }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.showToolCalls.rawValue) }
    }
    
    var autoTitle: Bool {
        get {
            UserDefaults.standard.object(forKey: SettingsKey.autoTitle.rawValue) as? Bool ?? true
        }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.autoTitle.rawValue) }
    }
    
    var soundEffects: Bool {
        get { UserDefaults.standard.bool(forKey: SettingsKey.soundEffects.rawValue) }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.soundEffects.rawValue) }
    }
    
    var theme: String {
        get { UserDefaults.standard.string(forKey: SettingsKey.theme.rawValue) ?? "auto" }
        set { UserDefaults.standard.set(newValue, forKey: SettingsKey.theme.rawValue) }
    }
}
