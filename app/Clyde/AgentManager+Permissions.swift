//
//  AgentManager+Permissions.swift
//  Clyde
//
//  Created 2026-04-16.
//
//  Handles two concerns:
//
//  1. **ClydeEngine.app bundle** — a minimal .app wrapper around python3
//     so that macOS shows "Clyde Engine" (not "python3") in Full Disk Access
//     and other TCC prompts. The bundle is auto-created at first launch and
//     refreshed when the underlying python3 binary changes.
//
//  2. **Permission checking** — probes whether ClydeEngine has the
//     permissions it needs (Full Disk Access for Messages DB, etc.)
//     and surfaces status to the UI via published properties.
//

import Foundation
import SQLite3

// MARK: - Permission Model

enum ClydePermission: String, CaseIterable, Identifiable {
    case fullDiskAccess
    case contacts
    case calendars
    case reminders

    var id: String { rawValue }

    var title: String {
        switch self {
        case .fullDiskAccess: return "Full Disk Access"
        case .contacts:       return "Contacts"
        case .calendars:      return "Calendars"
        case .reminders:      return "Reminders"
        }
    }

    var subtitle: String {
        switch self {
        case .fullDiskAccess: return "Read your Messages history"
        case .contacts:       return "Look up contacts for messaging"
        case .calendars:      return "View and create calendar events"
        case .reminders:      return "View and create reminders"
        }
    }

    var icon: String {
        switch self {
        case .fullDiskAccess: return "lock.shield"
        case .contacts:       return "person.crop.circle"
        case .calendars:      return "calendar"
        case .reminders:      return "checklist"
        }
    }

    /// The System Settings pane to open for this permission.
    var settingsURL: URL? {
        switch self {
        case .fullDiskAccess:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
        case .contacts:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts")
        case .calendars:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars")
        case .reminders:
            return URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders")
        }
    }
}

// MARK: - AgentManager Extension

extension AgentManager {

    // ── ClydeEngine.app bundle ──────────────────────────────────────

    /// Path to the ClydeEngine.app bundle in Application Support.
    /// In production, ClydeEngine lives inside Clyde.app/Contents/MacOS/
    /// but for FDA we still need the .app bundle for TCC registration.
    static var clydeEngineBundlePath: String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return "\(home)/Library/Application Support/Clyde/ClydeEngine.app"
    }

    /// Path to the actual executable inside the bundle.
    static var clydeEngineExecutable: String {
        // Prefer bundled in the main app
        let bundled = Bundle.main.bundlePath + "/Contents/MacOS/ClydeEngine"
        if FileManager.default.fileExists(atPath: bundled) { return bundled }
        return "\(clydeEngineBundlePath)/Contents/MacOS/ClydeEngine"
    }

    /// Ensures the ClydeEngine.app bundle exists and is up to date.
    /// Called from onLaunch() before any agent startup.
    ///
    /// The bundle structure:
    /// ```
    /// ~/Library/Application Support/Clyde/ClydeEngine.app/
    ///   Contents/
    ///     Info.plist          (CFBundleName = "Clyde Engine")
    ///     MacOS/
    ///       ClydeEngine       (copy of python3 framework binary)
    /// ```
    ///
    /// When the user adds ClydeEngine.app to Full Disk Access, macOS
    /// displays it as "Clyde Engine" — clean and professional.
    func ensureClydeEngineBundle() {
        let fm = FileManager.default
        let bundlePath = Self.clydeEngineBundlePath
        let macosDir = "\(bundlePath)/Contents/MacOS"
        let execPath = Self.clydeEngineExecutable
        let plistPath = "\(bundlePath)/Contents/Info.plist"
        let pythonPath = "/opt/homebrew/bin/python3"

        // Find the Python *framework* binary — the actual executable that runs.
        // The Homebrew python3 is a small stub that exec's into the framework
        // binary. If we copy the stub, macOS TCC still sees the framework identity.
        // We must copy the framework binary itself so ClydeEngine has its own identity.
        //
        // Discovery: run `python3 -c "import sys; print(sys.executable)"` to find
        // the framework binary, then resolve that path.
        let candidatePaths = [pythonPath, "/usr/local/bin/python3"]
        var sourcePython: String?

        for candidate in candidatePaths where fm.fileExists(atPath: candidate) {
            // Ask Python itself where its framework binary lives
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: candidate)
            probe.arguments = ["-c", "import sys; print(sys.executable)"]
            let pipe = Pipe()
            probe.standardOutput = pipe
            probe.standardError = FileHandle.nullDevice
            do {
                try probe.run()
                probe.waitUntilExit()
                let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                // Resolve the framework binary path
                let resolved = URL(fileURLWithPath: output).resolvingSymlinksInPath().path
                // Now find the actual framework Python binary that gets exec'd
                // Typically: .../Python.framework/Versions/X.Y/Resources/Python.app/Contents/MacOS/Python
                let frameworkBin = resolved
                    .components(separatedBy: "/Frameworks/Python.framework/")
                    .first
                    .map { "\($0)/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python" }

                if let fwBin = frameworkBin, fm.fileExists(atPath: fwBin) {
                    sourcePython = fwBin
                } else if fm.fileExists(atPath: resolved) {
                    sourcePython = resolved
                }
                if sourcePython != nil { break }
            } catch {
                continue
            }
        }

        // Fallback: just resolve symlinks on the python3 path
        if sourcePython == nil {
            for candidate in candidatePaths where fm.fileExists(atPath: candidate) {
                sourcePython = URL(fileURLWithPath: candidate).resolvingSymlinksInPath().path
                break
            }
        }

        guard let sourcePython else {
            print("[Permissions] python3 not found — cannot create ClydeEngine bundle")
            return
        }

        // Check if we need to create or update
        let needsCreate = !fm.fileExists(atPath: execPath)
        var needsUpdate = false

        if !needsCreate {
            // Compare modification dates — update if python3 is newer
            let sourceAttrs = try? fm.attributesOfItem(atPath: sourcePython)
            let destAttrs = try? fm.attributesOfItem(atPath: execPath)
            if let sourceDate = sourceAttrs?[.modificationDate] as? Date,
               let destDate = destAttrs?[.modificationDate] as? Date,
               sourceDate > destDate {
                needsUpdate = true
            }
        }

        guard needsCreate || needsUpdate else {
            print("[Permissions] ClydeEngine.app bundle is up to date")
            return
        }

        do {
            // Create directory structure
            try fm.createDirectory(atPath: macosDir, withIntermediateDirectories: true)

            // Copy python3 as ClydeEngine
            if fm.fileExists(atPath: execPath) {
                try fm.removeItem(atPath: execPath)
            }
            try fm.copyItem(atPath: sourcePython, toPath: execPath)

            // Make executable
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: execPath)

            // Write Info.plist
            let plist: [String: Any] = [
                "CFBundleName": "Clyde Engine",
                "CFBundleDisplayName": "Clyde Engine",
                "CFBundleIdentifier": "com.clyde.engine",
                "CFBundleExecutable": "ClydeEngine",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1.0",
                "CFBundleShortVersionString": "1.0",
                "LSUIElement": true,  // No dock icon
                "NSHighResolutionCapable": true
            ]
            let plistData = try PropertyListSerialization.data(
                fromPropertyList: plist, format: .xml, options: 0
            )
            try plistData.write(to: URL(fileURLWithPath: plistPath))

            // Re-sign the bundle so macOS trusts it
            let codesign = Process()
            codesign.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
            codesign.arguments = ["--force", "--deep", "--sign", "-", bundlePath]
            codesign.standardOutput = FileHandle.nullDevice
            codesign.standardError = FileHandle.nullDevice
            try codesign.run()
            codesign.waitUntilExit()

            let verb = needsCreate ? "Created" : "Updated"
            print("[Permissions] \(verb) ClydeEngine.app bundle at \(bundlePath)")
        } catch {
            print("[Permissions] Failed to create ClydeEngine bundle: \(error)")
        }
    }

    // ── Permission Checks ───────────────────────────────────────────

    /// Check if Full Disk Access is granted for **ClydeEngine** (the binary
    /// the agent actually runs as), NOT for Clyde.app itself.
    ///
    /// We spawn ClydeEngine to try opening chat.db — this is the only reliable
    /// way to check FDA for a different binary, since macOS TCC grants are
    /// per-binary/per-bundle-id.
    static func checkFullDiskAccess() -> Bool {
        let engineExec = clydeEngineExecutable
        let fm = FileManager.default

        // If ClydeEngine doesn't exist yet, FDA can't be granted
        guard fm.fileExists(atPath: engineExec) else { return false }

        let home = fm.homeDirectoryForCurrentUser.path
        let dbPath = "\(home)/Library/Messages/chat.db"

        // Spawn ClydeEngine (python3 copy) to test opening chat.db
        let process = Process()
        process.executableURL = URL(fileURLWithPath: engineExec)
        process.arguments = ["-c", """
            import sqlite3, sys
            try:
                conn = sqlite3.connect('file:\(dbPath)?mode=ro', uri=True)
                conn.execute('SELECT 1 FROM message LIMIT 1')
                conn.close()
                print('OK')
            except Exception as e:
                print(f'FAIL: {e}')
                sys.exit(1)
            """]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    /// Run an osascript probe, optionally launching the target app first.
    /// Returns true if the script exits with status 0.
    private static func osascriptProbe(app: String, script: String, launchFirst: Bool = true) -> Bool {
        if launchFirst {
            // Launch the app in background without stealing focus
            let launcher = Process()
            launcher.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            launcher.arguments = ["-gj", "-a", app]
            launcher.standardOutput = FileHandle.nullDevice
            launcher.standardError = FileHandle.nullDevice
            try? launcher.run()
            launcher.waitUntilExit()

            // Wait briefly for the app to become responsive
            Thread.sleep(forTimeInterval: 0.5)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            process.waitUntilExit()
            let ok = process.terminationStatus == 0
            print("[Permissions] \(app) probe: \(ok ? "granted" : "denied") (exit=\(process.terminationStatus))")
            return ok
        } catch {
            print("[Permissions] \(app) probe error: \(error)")
            return false
        }
    }

    /// Check Contacts access via AppleScript (matches how the agent accesses contacts).
    /// Pre-launches Contacts.app to avoid -600 "not running" errors.
    static func checkContactsAccess() -> Bool {
        osascriptProbe(app: "Contacts", script: "tell application \"Contacts\" to count people")
    }

    /// Check Calendar access via AppleScript.
    static func checkCalendarAccess() -> Bool {
        osascriptProbe(app: "Calendar", script: "tell application \"Calendar\" to count calendars")
    }

    /// Check Reminders access via AppleScript.
    static func checkRemindersAccess() -> Bool {
        osascriptProbe(app: "Reminders", script: "tell application \"Reminders\" to count lists")
    }
}
