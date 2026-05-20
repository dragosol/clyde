//
//  PermissionsView.swift
//  Clyde
//
//  Created 2026-04-16.
//
//  Permissions pane displayed as a tab in Settings (⌘,).
//  Shows each macOS permission Clyde needs, with live status checks
//  and deep-link buttons to the exact System Settings pane.
//
//  Also shown when the user agrees to grant permissions from the
//  in-chat prompt (via the permissionsDeepLink notification).
//

import SwiftUI

struct PermissionsSettingsPane: View {
    @State private var permissionStatuses: [ClydePermission: Bool] = [:]
    @State private var isChecking = false

    private let permissions: [ClydePermission] = ClydePermission.allCases

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Privacy disclaimer
            privacyBanner
                .padding(.bottom, 16)

            // Permission cards
            VStack(spacing: 10) {
                ForEach(permissions) { permission in
                    permissionRow(permission)
                }
            }

            Spacer(minLength: 16)

            // Recheck button
            HStack {
                Button(action: checkAllPermissions) {
                    HStack(spacing: 6) {
                        if isChecking {
                            ProgressView()
                                .scaleEffect(0.6)
                                .frame(width: 14, height: 14)
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                        Text("Recheck Permissions")
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.regular)
                .disabled(isChecking)

                Spacer()

                if allGranted {
                    HStack(spacing: 4) {
                        Image(systemName: "checkmark.seal.fill")
                            .foregroundStyle(.green)
                        Text("All permissions granted")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 20)
        .frame(minWidth: 480, idealWidth: 520)
        .onAppear { checkAllPermissions() }
    }

    // MARK: - Privacy Banner

    private var privacyBanner: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "lock.shield.fill")
                .font(.title3)
                .foregroundStyle(.green)
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 4) {
                Text("Private & Local Only")
                    .font(.callout)
                    .fontWeight(.medium)

                Text("Clyde runs entirely on your Mac. Your data never leaves this device. Clyde will always ask before modifying files or performing sensitive actions like sending messages.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.green.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.green.opacity(0.15), lineWidth: 0.5)
        )
    }

    // MARK: - Permission Row

    private func permissionRow(_ permission: ClydePermission) -> some View {
        let isGranted = permissionStatuses[permission] ?? false

        return HStack(spacing: 12) {
            // Status icon
            Image(systemName: isGranted ? "checkmark.circle.fill" : "circle")
                .font(.body)
                .foregroundColor(isGranted ? .green : .gray.opacity(0.4))
                .frame(width: 20)

            // Icon
            Image(systemName: permission.icon)
                .font(.callout)
                .foregroundStyle(isGranted ? .primary : .secondary)
                .frame(width: 20)

            // Text
            VStack(alignment: .leading, spacing: 1) {
                Text(permission.title)
                    .font(.callout)

                Text(permission.subtitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                // FDA-specific hint: tell user exactly what to look for
                if permission == .fullDiskAccess && !isGranted {
                    Text("Add \"Clyde Engine\" in System Settings → Full Disk Access")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .padding(.top, 2)
                }
            }

            Spacer()

            if !isGranted {
                Button("Open Settings") {
                    openSettings(for: permission)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(isGranted ? Color.green.opacity(0.04) : Color.clear)
        )
    }

    // MARK: - Helpers

    private var allGranted: Bool {
        permissions.allSatisfy { permissionStatuses[$0] == true }
    }

    private func checkAllPermissions() {
        isChecking = true
        Task { @MainActor in
            let results: [ClydePermission: Bool] = [
                .fullDiskAccess: AgentManager.checkFullDiskAccess(),
                .contacts: AgentManager.checkContactsAccess(),
                .calendars: AgentManager.checkCalendarAccess(),
                .reminders: AgentManager.checkRemindersAccess(),
            ]
            withAnimation(.easeInOut(duration: 0.2)) {
                permissionStatuses = results
                isChecking = false
            }
        }
    }

    private func openSettings(for permission: ClydePermission) {
        guard let url = permission.settingsURL else { return }
        NSWorkspace.shared.open(url)
    }
}

// MARK: - In-Chat Permission Card

/// An ask_user-style card shown in chat when a macOS tool needs
/// permissions that haven't been granted yet. Matches the visual
/// style of QuestionCardView.
struct PermissionPromptCard: View {
    let permissionName: String
    let onAccept: () -> Void
    let onDecline: () -> Void
    @State private var answered = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack(spacing: 8) {
                Image(systemName: "shield.checkered")
                    .foregroundStyle(.orange)
                Text("Permissions Needed")
                    .font(.callout)
                    .fontWeight(.medium)
            }

            Text("Before we proceed, Clyde needs some macOS permissions to \(permissionName). Everything stays private and local on your Mac.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // Buttons
            if !answered {
                HStack(spacing: 10) {
                    Button(action: {
                        withAnimation { answered = true }
                        onAccept()
                    }) {
                        Text("Sounds good, let's do it!")
                            .font(.caption)
                            .fontWeight(.medium)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 7)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)

                    Button(action: {
                        withAnimation { answered = true }
                        onDecline()
                    }) {
                        Text("Not now")
                            .font(.caption)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 7)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
            } else {
                HStack(spacing: 4) {
                    Image(systemName: "checkmark.circle")
                        .foregroundStyle(.green)
                    Text("Opening Settings...")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(14)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(Color.orange.opacity(0.2), lineWidth: 0.5)
        )
        .frame(maxWidth: 380)
    }
}
