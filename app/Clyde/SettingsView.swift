//
//  SettingsView.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//  Refactored 2026-04-14 — unified Models pane (IA Direction A).
//  Refactored 2026-04-14 (v2) — native Settings window presentation:
//    • Hosted by a `Settings { }` scene in ClydeApp (⌘, keyboard shortcut).
//    • No more `.sheet` chrome, no more forced `frame(width:height:)`,
//      no more "Done" toolbar button — the native window supplies those.
//    • Per-tab uses a plain padded VStack instead of `.formStyle(.grouped)`,
//      which had been nesting rounded rectangles inside the sheet.
//

import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var viewModel: AppViewModel

    @AppStorage("show_thinking") private var showThinking = false
    @AppStorage("show_tool_calls") private var showToolCalls = true
    @AppStorage("auto_title") private var autoTitle = true
    @AppStorage("sound_effects") private var soundEffects = false
    @AppStorage("theme") private var theme = "auto"

    enum SettingsTab: Hashable {
        case models, appearance, behavior, permissions
    }
    @State private var selectedTab: SettingsTab = .models

    var body: some View {
        TabView(selection: $selectedTab) {
            // Unified Models pane — replaces the old Connection + Model Parameters tabs.
            ModelsSettingsPane()
                .tabItem {
                    Label("Models", systemImage: "cube.box")
                }
                .tag(SettingsTab.models)

            appearanceTab
                .tabItem {
                    Label("Appearance", systemImage: "paintbrush")
                }
                .tag(SettingsTab.appearance)

            behaviorTab
                .tabItem {
                    Label("Behavior", systemImage: "slider.horizontal.3")
                }
                .tag(SettingsTab.behavior)

            PermissionsSettingsPane()
                .tabItem {
                    Label("Permissions", systemImage: "shield.checkered")
                }
                .tag(SettingsTab.permissions)
        }
        // Persist non-routing prefs on change. The Models pane writes via
        // AppStorage + updateAPISettings() directly.
        .onChange(of: showThinking)  { _, _ in saveSettings() }
        .onChange(of: showToolCalls) { _, _ in saveSettings() }
        .onChange(of: autoTitle)     { _, _ in saveSettings() }
        .onChange(of: soundEffects)  { _, _ in saveSettings() }
        .onChange(of: theme)         { _, _ in saveSettings() }
        .onReceive(NotificationCenter.default.publisher(for: .openPermissionsTab)) { _ in
            selectedTab = .permissions
        }
    }

    // MARK: - Appearance

    private var appearanceTab: some View {
        VStack(alignment: .leading, spacing: 18) {
            LabeledContent {
                Picker("", selection: $theme) {
                    Text("Auto").tag("auto")
                    Text("Light").tag("light")
                    Text("Dark").tag("dark")
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .frame(maxWidth: 260)
            } label: {
                Text("Theme:")
            }
            Spacer()
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 20)
        .frame(minWidth: 480, idealWidth: 520)
    }

    // MARK: - Behavior

    private var behaviorTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Show thinking by default", isOn: $showThinking)
            Toggle("Show tool calls", isOn: $showToolCalls)
            Toggle("Auto-title conversations", isOn: $autoTitle)
            Toggle("Sound effects", isOn: $soundEffects)
            Spacer()
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 20)
        .frame(minWidth: 480, idealWidth: 520)
    }

    // MARK: - Persistence

    private func saveSettings() {
        let persistence = PersistenceManager.shared
        persistence.showThinking  = showThinking
        persistence.showToolCalls = showToolCalls
        persistence.autoTitle     = autoTitle
        persistence.soundEffects  = soundEffects
        persistence.theme         = theme
        viewModel.updateAPISettings()
    }
}
