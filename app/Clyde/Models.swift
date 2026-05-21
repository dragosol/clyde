//
//  Models.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//

import Foundation

// MARK: - Message Role

enum MessageRole: String, Codable {
    case user
    case assistant
    case system
}

// MARK: - Attachment

struct Attachment: Identifiable, Codable {
    let id: UUID
    var type: AttachmentType
    var fileName: String
    var mimeType: String
    var base64Data: String
    
    init(id: UUID = UUID(), type: AttachmentType, fileName: String, mimeType: String, base64Data: String) {
        self.id = id
        self.type = type
        self.fileName = fileName
        self.mimeType = mimeType
        self.base64Data = base64Data
    }
}

enum AttachmentType: String, Codable {
    case image
    case video
    case document

    /// SF Symbol for this attachment type
    var icon: String {
        switch self {
        case .image: return "photo"
        case .video: return "film"
        case .document: return "doc"
        }
    }
}

/// Maps file extensions to proper MIME types for the agent's sidecar
enum MIMEType {
    static func from(extension ext: String) -> String {
        switch ext.lowercased() {
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
        case "heic": return "image/heic"
        case "mp4": return "video/mp4"
        case "mov": return "video/quicktime"
        case "pdf": return "application/pdf"
        case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        case "xlsx": return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        case "pptx": return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        case "txt": return "text/plain"
        case "md": return "text/markdown"
        case "csv": return "text/csv"
        case "json": return "application/json"
        case "py", "swift", "js", "ts", "sh", "yaml", "yml", "html", "css", "xml":
            return "text/plain"
        default: return "application/octet-stream"
        }
    }

    static func attachmentType(for ext: String) -> AttachmentType {
        switch ext.lowercased() {
        case "png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "tiff":
            return .image
        case "mp4", "mov", "m4v", "avi", "mkv":
            return .video
        default:
            return .document
        }
    }
}

// MARK: - File Reference
/// Lightweight reference to a file stored in app sandbox.
/// The actual file lives in .../Application Support/Clyde/files/{conversationId}/
/// This avoids bloating the conversation JSON with base64 data.
struct FileReference: Identifiable, Codable {
    let id: UUID
    var fileName: String
    var mimeType: String
    var relativePath: String   // e.g. "{conversationId}/{fileName}" — relative to files/ dir
    var originalPath: String   // Original path detected in agent response
    var fileSize: Int64        // Bytes
    var createdAt: Date

    init(
        id: UUID = UUID(),
        fileName: String,
        mimeType: String,
        relativePath: String,
        originalPath: String,
        fileSize: Int64 = 0,
        createdAt: Date = Date()
    ) {
        self.id = id
        self.fileName = fileName
        self.mimeType = mimeType
        self.relativePath = relativePath
        self.originalPath = originalPath
        self.fileSize = fileSize
        self.createdAt = createdAt
    }

    /// SF Symbol and color for this file type
    var docInfo: (icon: String, color: String) {
        let ext = (fileName as NSString).pathExtension.lowercased()
        switch ext {
        case "pdf":  return ("doc.text.fill", "red")
        case "docx": return ("doc.richtext", "blue")
        case "xlsx": return ("tablecells", "green")
        case "pptx": return ("rectangle.on.rectangle", "orange")
        case "csv":  return ("tablecells", "teal")
        case "json": return ("curlybraces", "purple")
        case "py":   return ("chevron.left.forwardslash.chevron.right", "yellow")
        case "swift": return ("swift", "orange")
        case "js", "ts": return ("chevron.left.forwardslash.chevron.right", "yellow")
        case "html": return ("globe", "blue")
        case "md", "txt": return ("doc.text", "secondary")
        case "png", "jpg", "jpeg", "gif", "webp": return ("photo", "purple")
        case "mp4", "mov": return ("film", "pink")
        default:     return ("doc", "secondary")
        }
    }

    /// Human-readable file size
    var formattedSize: String {
        ByteCountFormatter.string(fromByteCount: fileSize, countStyle: .file)
    }
}

/// File extensions the app can detect and manage
enum SupportedFileExtensions {
    static let all: Set<String> = [
        "pdf", "docx", "xlsx", "pptx", "txt", "md", "csv", "json",
        "py", "swift", "js", "ts", "sh", "yaml", "yml", "html", "css", "xml",
        "png", "jpg", "jpeg", "gif", "webp", "heic",
        "mp4", "mov"
    ]
}

// MARK: - Tool Call

enum ToolStatus: String, Codable {
    case running
    case done
    case error
}

struct ToolCall: Identifiable, Codable {
    let id: UUID
    var toolName: String
    var arguments: String
    var result: String?
    var output: String?      // Full tool output (shown in expanded view)
    var status: ToolStatus
    var isExpanded: Bool = false

    init(id: UUID = UUID(), toolName: String, arguments: String = "", result: String? = nil, output: String? = nil, status: ToolStatus = .running, isExpanded: Bool = false) {
        self.id = id
        self.toolName = toolName
        self.arguments = arguments
        self.result = result
        self.output = output
        self.status = status
        self.isExpanded = isExpanded
    }
}

// MARK: - Message Block (ordered content)

/// Represents a single block of content in the message stream.
/// Blocks are stored in order so narration text and tool calls interleave correctly.
enum MessageBlockContent: Codable, Equatable {
    case text(String)
    case toolCall(UUID)  // references a ToolCall by its id
    case question(String) // references a QuestionData by its id — renders inline
    case permissionRequest(String) // references a PermissionRequestData by its id
    case plan(String) // references a PlanData by plan.id — renders inline plan card
}

struct MessageBlock: Identifiable, Codable, Equatable {
    let id: UUID
    var content: MessageBlockContent

    init(id: UUID = UUID(), content: MessageBlockContent) {
        self.id = id
        self.content = content
    }
}

// MARK: - Chat Message

struct ChatMessage: Identifiable, Codable {
    let id: UUID
    var role: MessageRole
    var content: String
    var attachments: [Attachment]
    var toolCalls: [ToolCall]
    var thinkingContent: String?
    var files: [FileReference]
    var timestamp: Date
    var isStreaming: Bool = false
    /// True while this message sits in the queue waiting for the current
    /// agent stream to finish. Set when the user types during an active
    /// stream; cleared when the agent actually starts processing it.
    /// Drives the orange queued-bubble tint in MessageBubbleView.
    var isQueued: Bool = false
    /// Ordered content blocks for interleaved rendering of text and tool calls.
    /// When non-empty, the view renders these in order instead of the old
    /// "all tool calls then all content" layout.
    var contentBlocks: [MessageBlock] = []
    /// Pending question from agent's ask_user tool (nil when no question active).
    var pendingQuestions: [QuestionData] = []
    /// Pending folder permission requests from the agent.
    var pendingPermissions: [PermissionRequestData] = []
    /// Plans referenced by inline plan blocks. The same plan_id is updated
    /// in place across multiple plan_state events so the card animates.
    var plans: [PlanData] = []

    init(
        id: UUID = UUID(),
        role: MessageRole,
        content: String,
        attachments: [Attachment] = [],
        toolCalls: [ToolCall] = [],
        thinkingContent: String? = nil,
        files: [FileReference] = [],
        timestamp: Date = Date(),
        isStreaming: Bool = false,
        isQueued: Bool = false
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.attachments = attachments
        self.toolCalls = toolCalls
        self.thinkingContent = thinkingContent
        self.files = files
        self.timestamp = timestamp
        self.isStreaming = isStreaming
        self.isQueued = isQueued
    }

    /// Custom decoder: existing saved conversations won't have the `files` or
    /// `contentBlocks` keys, so we use decodeIfPresent to default gracefully.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        role = try container.decode(MessageRole.self, forKey: .role)
        content = try container.decode(String.self, forKey: .content)
        attachments = try container.decodeIfPresent([Attachment].self, forKey: .attachments) ?? []
        toolCalls = try container.decodeIfPresent([ToolCall].self, forKey: .toolCalls) ?? []
        thinkingContent = try container.decodeIfPresent(String.self, forKey: .thinkingContent)
        files = try container.decodeIfPresent([FileReference].self, forKey: .files) ?? []
        timestamp = try container.decode(Date.self, forKey: .timestamp)
        isStreaming = try container.decodeIfPresent(Bool.self, forKey: .isStreaming) ?? false
        isQueued = try container.decodeIfPresent(Bool.self, forKey: .isQueued) ?? false
        contentBlocks = try container.decodeIfPresent([MessageBlock].self, forKey: .contentBlocks) ?? []
        pendingQuestions = try container.decodeIfPresent([QuestionData].self, forKey: .pendingQuestions) ?? []
        pendingPermissions = try container.decodeIfPresent([PermissionRequestData].self, forKey: .pendingPermissions) ?? []
        plans = try container.decodeIfPresent([PlanData].self, forKey: .plans) ?? []
    }
}

// MARK: - Conversation

struct Conversation: Identifiable, Codable {
    let id: UUID
    var title: String
    var messages: [ChatMessage]
    var projectId: UUID?
    var isPinned: Bool
    var createdAt: Date
    var updatedAt: Date

    // Per-conversation inspector state — persisted so switching chats
    // and relaunching Clyde restores the exact UI state.
    var thinkingEnabled: Bool?
    var lastActiveSkill: String?          // e.g. "General", "Quick Research"
    var lastActiveSkillInfo: SkillActivityInfo?

    init(
        id: UUID = UUID(),
        title: String = "New Conversation",
        messages: [ChatMessage] = [],
        projectId: UUID? = nil,
        isPinned: Bool = false,
        createdAt: Date = Date(),
        updatedAt: Date = Date(),
        thinkingEnabled: Bool? = nil,
        lastActiveSkill: String? = nil,
        lastActiveSkillInfo: SkillActivityInfo? = nil
    ) {
        self.id = id
        self.title = title
        self.messages = messages
        self.projectId = projectId
        self.isPinned = isPinned
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.thinkingEnabled = thinkingEnabled
        self.lastActiveSkill = lastActiveSkill
        self.lastActiveSkillInfo = lastActiveSkillInfo
    }
}

// MARK: - API Models

struct ChatCompletionRequest: Codable {
    let model: String
    let messages: [APIMessage]
    let stream: Bool
    let temperature: Double?
    let maxTokens: Int?
    
    enum CodingKeys: String, CodingKey {
        case model, messages, stream, temperature
        case maxTokens = "max_tokens"
    }
}

struct APIMessage: Codable {
    let role: String
    let content: [ContentPart]
}

enum ContentPart: Codable {
    case text(String)
    case imageUrl(ImageURL)
    
    struct ImageURL: Codable {
        let url: String
    }
    
    enum CodingKeys: String, CodingKey {
        case type, text
        case imageUrl = "image_url"
    }
    
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decode(String.self, forKey: .type)
        
        switch type {
        case "text":
            let text = try container.decode(String.self, forKey: .text)
            self = .text(text)
        case "image_url":
            let imageUrl = try container.decode(ImageURL.self, forKey: .imageUrl)
            self = .imageUrl(imageUrl)
        default:
            throw DecodingError.dataCorruptedError(forKey: .type, in: container, debugDescription: "Unknown content type")
        }
    }
    
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        
        switch self {
        case .text(let text):
            try container.encode("text", forKey: .type)
            try container.encode(text, forKey: .text)
        case .imageUrl(let imageUrl):
            try container.encode("image_url", forKey: .type)
            try container.encode(imageUrl, forKey: .imageUrl)
        }
    }
}

struct ChatCompletionChunk: Codable {
    let choices: [Choice]
    
    struct Choice: Codable {
        let delta: Delta
    }
    
    struct Delta: Codable {
        let content: String?
        let clyde_metrics: ClydeMetricsPayload?
        let clyde_context: ClydeContextPayload?
    }

    struct ClydeMetricsPayload: Codable {
        let phase: String?
        let tokens: Int?
        let tps: Double?
        let elapsed: Double?
        let thinking_chars: Int?
        let content_chars: Int?
    }

    struct ClydeContextPayload: Codable {
        let tokens: Int?
        let threshold: Int?
        let messages: Int?
    }
}

// MARK: - Question Data (ask_user)

/// Data for a multiple-choice question from the agent's ask_user tool.
struct QuestionData: Identifiable, Codable {
    let id: String            // question_id from agent (e.g. "q_abc12345")
    var question: String       // The question text
    var choices: [String]      // e.g. ["Option A", "Option B", "Option C"]
    var allowOther: Bool       // Whether to show a free-text "Other" option
    var selectedChoice: String? // The user's answer (nil = not yet answered)
    var isAnswered: Bool { selectedChoice != nil }
}

// MARK: - Permission Request Data

/// Data for a folder permission request from the agent.
/// When a tool tries to access a path outside allowed directories,
/// the agent emits this so Clyde can show a permission card.
struct PermissionRequestData: Identifiable, Codable {
    let id: String            // permission_id from agent (e.g. "perm_abc12345")
    var path: String           // The specific file/folder being accessed
    var folder: String         // The top-level folder to grant access to
    var granted: Bool?         // nil = pending, true = granted, false = denied

    var isPending: Bool { granted == nil }
}

// MARK: - Plan Data (task_plan / task_update / task_complete)

/// One step in a multi-step task plan. Mirrors plan.PlanStep on the agent side.
struct PlanStepData: Identifiable, Codable, Equatable {
    let id: String              // e.g. "s_abc123" — agent's step ID
    var description: String
    var status: String          // pending | in_progress | done | failed | skipped
    var notes: String
    var failedAttempts: [String]
    var wasInterrupted: Bool
    var type: String            // normal | user_interjection | subplan
    var subplanId: String?

    enum CodingKeys: String, CodingKey {
        case id, description, status, notes, type
        case failedAttempts = "failed_attempts"
        case wasInterrupted = "was_interrupted"
        case subplanId = "subplan_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        description = try c.decode(String.self, forKey: .description)
        status = try c.decode(String.self, forKey: .status)
        notes = (try? c.decode(String.self, forKey: .notes)) ?? ""
        failedAttempts = (try? c.decode([String].self, forKey: .failedAttempts)) ?? []
        wasInterrupted = (try? c.decode(Bool.self, forKey: .wasInterrupted)) ?? false
        type = (try? c.decode(String.self, forKey: .type)) ?? "normal"
        subplanId = try? c.decode(String.self, forKey: .subplanId)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(description, forKey: .description)
        try c.encode(status, forKey: .status)
        try c.encode(notes, forKey: .notes)
        try c.encode(failedAttempts, forKey: .failedAttempts)
        try c.encode(wasInterrupted, forKey: .wasInterrupted)
        try c.encode(type, forKey: .type)
        try c.encodeIfPresent(subplanId, forKey: .subplanId)
    }

    var isDone: Bool { status == "done" }
    var isInProgress: Bool { status == "in_progress" }
    var isFailed: Bool { status == "failed" }
    var isPending: Bool { status == "pending" }
    var isSkipped: Bool { status == "skipped" }
}

/// A multi-step plan with a goal, ordered steps, and an optional output file.
/// Mirrors plan.Plan on the agent side. The agent emits this via the
/// <<plan_state:JSON>> SSE marker after every task_* tool call.
struct PlanData: Identifiable, Codable, Equatable {
    let id: String              // e.g. "plan_abc123"
    var goal: String
    var status: String          // pending_approval | active | paused | completed | failed | cancelled
    var outputFile: String?
    var steps: [PlanStepData]
    var createdAt: Double
    var updatedAt: Double
    var completedAt: Double?
    var parentStepId: String?
    var parentPlanId: String?

    enum CodingKeys: String, CodingKey {
        case id, goal, status, steps
        case outputFile = "output_file"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case completedAt = "completed_at"
        case parentStepId = "parent_step_id"
        case parentPlanId = "parent_plan_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        goal = try c.decode(String.self, forKey: .goal)
        status = try c.decode(String.self, forKey: .status)
        outputFile = try? c.decode(String.self, forKey: .outputFile)
        steps = try c.decode([PlanStepData].self, forKey: .steps)
        createdAt = (try? c.decode(Double.self, forKey: .createdAt)) ?? 0
        updatedAt = (try? c.decode(Double.self, forKey: .updatedAt)) ?? 0
        completedAt = try? c.decode(Double.self, forKey: .completedAt)
        parentStepId = try? c.decode(String.self, forKey: .parentStepId)
        parentPlanId = try? c.decode(String.self, forKey: .parentPlanId)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(goal, forKey: .goal)
        try c.encode(status, forKey: .status)
        try c.encode(steps, forKey: .steps)
        try c.encodeIfPresent(outputFile, forKey: .outputFile)
        try c.encode(createdAt, forKey: .createdAt)
        try c.encode(updatedAt, forKey: .updatedAt)
        try c.encodeIfPresent(completedAt, forKey: .completedAt)
        try c.encodeIfPresent(parentStepId, forKey: .parentStepId)
        try c.encodeIfPresent(parentPlanId, forKey: .parentPlanId)
    }

    var totalSteps: Int { steps.count }
    var doneSteps: Int { steps.filter { $0.isDone }.count }
    var progressFraction: Double {
        guard totalSteps > 0 else { return 0 }
        return Double(doneSteps) / Double(totalSteps)
    }
    var isPendingApproval: Bool { status == "pending_approval" }
    var isActive: Bool { status == "active" }
    var isCompleted: Bool { status == "completed" }
    var isCancelled: Bool { status == "cancelled" }
    var isArchived: Bool { isCompleted || isCancelled || status == "failed" }
}

// MARK: - Stream Delta

struct StreamDelta {
    enum DeltaType {
        case content(String)
        case toolStart(name: String, argsPreview: String)  // *using bash* `ls -la`
        case toolDone(name: String, isError: Bool, summary: String, output: String?)  // *bash · saved* or *web_search · 8 results*
        case thinking(String)                                // <think>...</think> content
        case thinkingStart                                   // *thinking...* status marker
        case separator                                       // --- divider before final answer
        case question(QuestionData)                          // <<question:JSON>> multiple-choice prompt
        case permissionRequest(PermissionRequestData)        // <<permission_request:JSON>> folder access
        case planState(state: String, plan: PlanData?)       // <<plan_state:JSON>> active plan snapshot
        case compacting(summary: String)                     // *compacting · 180K/168K tokens*
        case compactDone(summary: String)                    // *compact_done · 180K→45K tokens*
        case recovering(stage: String, detail: String)       // *recovering · stage · detail*
        case recoveryDone(detail: String)                    // *recovery_done · detail*
        case metrics(LiveMetrics)                            // live tok/s, phase, etc
        case contextSnapshot(ContextPressure)                // conversation tokens vs threshold
        case done
    }

    let type: DeltaType
}

/// Snapshot of live generation metrics for the streaming-cursor chip.
/// Phase: "thinking" | "content" | "tool" | "" (idle).
struct LiveMetrics: Equatable {
    let phase: String
    let tokens: Int
    let tps: Double
    let elapsed: Double
    let thinkingChars: Int
    let contentChars: Int
}

/// Snapshot of conversation context pressure — total tokens so far
/// vs the agent's compaction threshold. Emitted at each turn iteration
/// and again after compaction so the pressure graph shows the drop.
struct ContextPressure: Equatable {
    let tokens: Int
    let threshold: Int
    let messages: Int
    let timestamp: Date

    /// 0.0…1.0 — fraction of the compaction threshold consumed.
    var fraction: Double {
        guard threshold > 0 else { return 0 }
        return min(1.0, Double(tokens) / Double(threshold))
    }
}


// MARK: - Graph Data (Asset Graph)

struct GraphNode: Identifiable, Codable, Equatable {
    let id: String
    let type: String
    let title: String
    let body: String
    let status: String
    let createdAt: Double
    let updatedAt: Double
    let sourceTool: String

    // Metadata is a free-form dict on the Python side. We decode only
    // the fields we use and ignore the rest.
    let metadata: [String: String]?

    enum CodingKeys: String, CodingKey {
        case id, type, title, body, status, metadata
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case sourceTool = "source_tool"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        type = try c.decode(String.self, forKey: .type)
        title = try c.decode(String.self, forKey: .title)
        body = try c.decode(String.self, forKey: .body)
        status = try c.decode(String.self, forKey: .status)
        createdAt = try c.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
        updatedAt = try c.decodeIfPresent(Double.self, forKey: .updatedAt) ?? 0
        sourceTool = try c.decodeIfPresent(String.self, forKey: .sourceTool) ?? ""
        // Decode metadata permissively: try [String: String], fall back to nil
        metadata = try? c.decodeIfPresent([String: String].self, forKey: .metadata)
    }
}

struct GraphEdge: Codable, Equatable, Identifiable {
    let edgeId: String
    let fromId: String
    let toId: String
    let kind: String
    let note: String
    let createdAt: Double

    var id: String { edgeId }

    enum CodingKeys: String, CodingKey {
        case edgeId = "id"
        case fromId = "from_id"
        case toId = "to_id"
        case kind, note
        case createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        edgeId = try c.decodeIfPresent(String.self, forKey: .edgeId) ?? UUID().uuidString
        fromId = try c.decode(String.self, forKey: .fromId)
        toId = try c.decode(String.self, forKey: .toId)
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        note = try c.decodeIfPresent(String.self, forKey: .note) ?? ""
        createdAt = try c.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
    }
}

struct GraphData: Codable, Equatable {
    let nodes: [GraphNode]
    let edges: [GraphEdge]
    let version: Int
}


// MARK: - Skill Activity (Inspector Panel)

/// Metadata for a single skill (inner "skill" object from /v1/skills/active).
struct SkillDetail: Codable, Equatable {
    let name: String
    let label: String
    let description: String
    let icon: String
    let tools: [String]
    let enableThinking: Bool
    let enforcesResearchBudget: Bool
    let requiresPlanning: Bool

    enum CodingKeys: String, CodingKey {
        case name, label, description, icon, tools
        case enableThinking = "enable_thinking"
        case enforcesResearchBudget = "enforces_research_budget"
        case requiresPlanning = "requires_planning"
    }
}

/// Full response from GET /v1/skills/active/{conversation_id}.
struct SkillActivityInfo: Codable, Equatable {
    let conversationId: String
    let activeSkill: String
    let autoRoute: Bool
    let userLocked: Bool
    let skill: SkillDetail

    enum CodingKeys: String, CodingKey {
        case conversationId = "conversation_id"
        case activeSkill = "active_skill"
        case autoRoute = "auto_route"
        case userLocked = "user_locked"
        case skill
    }
}
