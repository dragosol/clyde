//
//  ClydeApp.swift
//  Clyde
//
//  Created by Dragos Robu on 2026-04-02.
//  Refactored 2026-04-14 — native Settings scene (⌘,), no more sheet chrome.
//

import SwiftUI
import AppKit

@main
struct ClydeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var agentManager = AgentManager()
    @StateObject private var viewModel = AppViewModel()

    init() {
        // Force thin overlay scrollbars app-wide (Safari-style: appear on scroll, fade out)
        UserDefaults.standard.set("WhenScrolling", forKey: "AppleShowScrollBars")

        // Phase 3: machine-aware default backend. On the first launch only
        // (before any @AppStorage("backend_kind") has been written), pick a
        // sensible default based on hw.memsize:
        //   ≥32 GB → Clyde Pro (mlx-vlm + turbo3)
        //   16-24 GB → Clyde Flash (forked mlx-flash + turbo3)
        //   <16 GB → leave as llama.cpp (legacy fallback)
        if UserDefaults.standard.object(forKey: "backend_kind") == nil {
            let memBytes = ProcessInfo.processInfo.physicalMemory
            let gb = Double(memBytes) / 1_073_741_824.0
            let kind: BackendKind
            if gb >= 32 {
                kind = .mlxVLMPro
            } else if gb >= 16 {
                kind = .mlxFlashVLM
            } else {
                kind = .llamaCpp
            }
            UserDefaults.standard.set(kind.rawValue, forKey: "backend_kind")
            UserDefaults.standard.set(kind.defaultPort, forKey: "backend_port")
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(agentManager)
                .environmentObject(viewModel)
                .frame(minWidth: 700, minHeight: 450)
                .onAppear {
                    appDelegate.agentManager = agentManager
                    agentManager.onLaunch()
                    WeatherService.shared.start()
                }
        }
        .defaultSize(width: 1000, height: 700)
        .windowStyle(.automatic)
        .windowToolbarStyle(.unified(showsTitle: false))

        // Secondary window for full-screen asset graph view.
        Window("Asset Graph", id: "asset-graph") {
            FullWindowGraphView()
                .environmentObject(agentManager)
                .environmentObject(viewModel)
                .frame(minWidth: 400, minHeight: 400)
        }
        .defaultSize(width: 700, height: 700)
        .windowStyle(.automatic)

        // Native macOS Settings scene — floating window summoned by ⌘,
        // (matches Finder Settings / Notes Settings aesthetic).
        // Replaces the old .sheet(isPresented:) presentation in ContentView.
        Settings {
            SettingsView()
                .environmentObject(agentManager)
                .environmentObject(viewModel)
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var agentManager: AgentManager?

    func applicationWillTerminate(_ notification: Notification) {
        agentManager?.onQuit()
    }
}
