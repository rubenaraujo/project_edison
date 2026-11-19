import AppKit
import AVFoundation
import CoreAudio
import ScreenCaptureKit
import ServiceManagement
import UserNotifications
import WebKit

// MARK: - Paths

let baseFolder: URL = {
    let custom = ProcessInfo.processInfo.environment["TEAMS_REC_FOLDER"]
    let path = custom ?? ("~/Teams Recordings" as NSString).expandingTildeInPath
    return URL(fileURLWithPath: path, isDirectory: true)
}()

func log(_ s: String) {
    let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    let line = "[\(f.string(from: Date()))] \(s)\n"
    try? FileManager.default.createDirectory(at: baseFolder, withIntermediateDirectories: true)
    let url = baseFolder.appendingPathComponent("recorder.log")
    if let h = try? FileHandle(forWritingTo: url) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    } else {
        try? line.write(to: url, atomically: true, encoding: .utf8)
    }
}

// MARK: - Notifications

var lastNotified: [String: Date] = [:]

var notificationsAllowed = false

func notify(_ title: String, _ message: String, key: String? = nil, every: TimeInterval = 1800, call: String? = nil) {
    if let key {
        if let t = lastNotified[key], Date().timeIntervalSince(t) < every { return }
        lastNotified[key] = Date()
    }
    if notificationsAllowed {
        let c = UNMutableNotificationContent()
        c.title = title; c.body = message
        if let call { c.userInfo = ["call": call] }
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
        return
    }
    let esc = { (t: String) in t.replacingOccurrences(of: "\\", with: "").replacingOccurrences(of: "\"", with: "'") }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", "display notification \"\(esc(message))\" with title \"\(esc(title))\""]
    try? p.run()
}

/// config.json is shared with the Python side and the Settings tab; re-read on every use.
func appConfig() -> [String: Any] {
    guard let d = try? Data(contentsOf: URL(fileURLWithPath: projectRoot + "/config.json")),
          let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return j
}

/// Titles of Microsoft Teams windows (needs the Screen Recording permission Edison already has).
func teamsWindowTitles() -> [String] {
    guard let list = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
    else { return [] }
    return list.compactMap { w in
        guard let owner = w[kCGWindowOwnerName as String] as? String, owner.localizedCaseInsensitiveContains("teams"),
              let name = w[kCGWindowName as String] as? String, !name.isEmpty else { return nil }
        return name
    }
}

func titleIsExcluded() -> Bool {
    let words = ((appConfig()["exclude_titles"] as? [String]) ?? []).map { $0.lowercased() }.filter { !$0.isEmpty }
    guard !words.isEmpty else { return false }
    return teamsWindowTitles().contains { t in words.contains { t.lowercased().contains($0) } }
}

func freeDiskGB() -> Double {
    let v = try? FileManager.default.attributesOfFileSystem(forPath: NSHomeDirectory())
    return Double((v?[.systemFreeSize] as? Int64) ?? Int64.max) / 1_073_741_824
}

/// build.sh records where the project (Python code, models, venv) lives.
let projectRoot: String = {
    if let u = Bundle.main.url(forResource: "project_root", withExtension: "txt"),
       let t = try? String(contentsOf: u, encoding: .utf8) {
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return ("~/Projects/project_edison" as NSString).expandingTildeInPath
}()

// MARK: - Call detection (Teams process using the microphone)

enum CallDetector {
    static func prop(_ sel: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    static func teamsIsUsingMic() -> Bool {
        var addr = prop(kAudioHardwarePropertyProcessObjectList)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr,
              size > 0 else { return false }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
        else { return false }

        for id in ids {
            var bAddr = prop(kAudioProcessPropertyBundleID)
            var cf: Unmanaged<CFString>?
            var bSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            guard AudioObjectGetPropertyData(id, &bAddr, 0, nil, &bSize, &cf) == noErr,
                  let bundle = cf?.takeRetainedValue() as String?,
                  bundle.lowercased().hasPrefix("com.microsoft.teams") else { continue }
            var rAddr = prop(kAudioProcessPropertyIsRunningInput)
            var running: UInt32 = 0
            var rSize = UInt32(MemoryLayout<UInt32>.size)
            if AudioObjectGetPropertyData(id, &rAddr, 0, nil, &rSize, &running) == noErr, running != 0 {
                return true
            }
        }
        return false
    }
}

// MARK: - Track writer (AAC in CAF: survives crashes)

final class TrackWriter {
    let url: URL
    private var file: AVAudioFile?
    private let queue = DispatchQueue(label: "track.writer")
    private var converter: AVAudioConverter?
    var frames: AVAudioFramePosition = 0

    init(url: URL) { self.url = url }

    func append(_ sb: CMSampleBuffer) {
        guard let pcm = TrackWriter.pcm(from: sb) else { return }
        queue.async { [self] in
            do {
                if file == nil {
                    let f = pcm.format
                    let settings: [String: Any] = [
                        AVFormatIDKey: kAudioFormatMPEG4AAC,
                        AVSampleRateKey: f.sampleRate,
                        AVNumberOfChannelsKey: Int(f.channelCount),
                        AVEncoderBitRateKey: 64_000 * Int(f.channelCount),
                    ]
                    file = try AVAudioFile(forWriting: url, settings: settings,
                                           commonFormat: .pcmFormatFloat32, interleaved: false)
                }
                guard let file else { return }
                if pcm.format == file.processingFormat {
                    try file.write(from: pcm)
                } else {
                    if converter == nil { converter = AVAudioConverter(from: pcm.format, to: file.processingFormat) }
                    guard let conv = converter,
                          let out = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: pcm.frameLength + 64)
                    else { return }
                    var fed = false
                    var err: NSError?
                    conv.convert(to: out, error: &err) { _, status in
                        if fed { status.pointee = .noDataNow; return nil }
                        fed = true; status.pointee = .haveData; return pcm
                    }
                    if err == nil { try file.write(from: out) }
                }
                frames += AVAudioFramePosition(pcm.frameLength)
            } catch {
                log("write error \(url.lastPathComponent): \(error)")
                notify("Edison: recording problem", "Could not write audio (\(error.localizedDescription)). Is the disk full?",
                       key: "write", every: 600)
            }
        }
    }

    func finish() { queue.sync { file = nil } }

    static func pcm(from sb: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard sb.isValid, CMSampleBufferGetNumSamples(sb) > 0,
              let desc = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(desc) else { return nil }
        var d = asbd.pointee
        guard let fmt = AVAudioFormat(streamDescription: &d),
              let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(CMSampleBufferGetNumSamples(sb)))
        else { return nil }
        buf.frameLength = buf.frameCapacity
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sb, at: 0, frameCount: Int32(CMSampleBufferGetNumSamples(sb)), into: buf.mutableAudioBufferList)
        return status == noErr ? buf : nil
    }
}

// MARK: - Recorder (ScreenCaptureKit: system audio + microphone)

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var system: TrackWriter?
    private var mic: TrackWriter?
    private(set) var folder: URL?
    private var startedAt = Date()
    var isRecording: Bool { stream != nil }

    @discardableResult
    func start() async -> Bool {
        guard stream == nil else { return true }
        if freeDiskGB() < 1.0 {
            notify("Edison: disk almost full", "Less than 1 GB free — call not recorded.", key: "disk", every: 3600)
            log("not recording: low disk")
            return false
        }
        if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized {
            notify("Edison: microphone access missing",
                   "Allow Edison in System Settings → Privacy & Security → Microphone.", key: "perm-mic", every: 3600)
        }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else { log("no display"); return false }

            let cfg = SCStreamConfiguration()
            cfg.capturesAudio = true
            cfg.captureMicrophone = true
            cfg.excludesCurrentProcessAudio = true
            cfg.sampleRate = 48000
            cfg.channelCount = 2
            cfg.width = 2; cfg.height = 2
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            let s = SCStream(filter: filter, configuration: cfg, delegate: self)

            let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
            let g = DateFormatter(); g.dateFormat = "HHmm"
            let dir = baseFolder.appendingPathComponent(f.string(from: Date()))
                .appendingPathComponent("\(g.string(from: Date()))_call")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            folder = dir
            FileManager.default.createFile(atPath: dir.appendingPathComponent(".recording").path, contents: nil)
            system = TrackWriter(url: dir.appendingPathComponent("others.caf"))
            mic = TrackWriter(url: dir.appendingPathComponent("me.caf"))

            let q = DispatchQueue(label: "capture.audio")
            try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: q)
            try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: q)
            try await s.startCapture()
            stream = s
            startedAt = Date()
            log("recording started -> \(dir.path)")
            return true
        } catch {
            log("start failed: \(error)")
            stream = nil
            notify("Edison: could not record",
                   "Check System Settings → Privacy & Security → Screen & System Audio Recording and Microphone.",
                   key: "start-fail", every: 900)
            return false
        }
    }

    func stop(discard: Bool = false) async {
        guard let s = stream else { return }
        stream = nil
        try? await s.stopCapture()
        system?.finish(); mic?.finish()
        log("recording stopped")
        if let dir = folder {
            try? FileManager.default.removeItem(at: dir.appendingPathComponent(".recording"))
            let minSeconds = (appConfig()["min_call_seconds"] as? Int) ?? 15
            let seconds = Date().timeIntervalSince(startedAt)
            if discard || seconds < Double(minSeconds) {
                log(discard ? "recording discarded (skipped by user or excluded title)" : "recording discarded (\(Int(seconds)) s, shorter than \(minSeconds) s)")
                try? FileManager.default.removeItem(at: dir)
                let day = dir.deletingLastPathComponent()
                if ((try? FileManager.default.contentsOfDirectory(atPath: day.path)) ?? ["x"]).filter({ $0 != ".DS_Store" }).isEmpty {
                    try? FileManager.default.removeItem(at: day)
                }
            } else {
                Postprocess.run(folder: dir)
            }
        }
        system = nil; mic = nil; folder = nil
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        switch type {
        case .audio: system?.append(sb)
        case .microphone: mic?.append(sb)
        default: break
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("stream error: \(error)")
        notify("Edison: recording interrupted", "The capture stopped unexpectedly; what was recorded is being processed.",
               key: "stream-err", every: 300)
        Task { await self.stop() }
    }
}

// MARK: - Post-processing (transcription script, runs detached)

enum CallStore {
    static let fm = FileManager.default

    /// All call folders, newest first.
    static func calls() -> [URL] {
        var out: [URL] = []
        for day in ((try? fm.contentsOfDirectory(atPath: baseFolder.path)) ?? []).sorted(by: >) {
            let dayURL = baseFolder.appendingPathComponent(day)
            for call in ((try? fm.contentsOfDirectory(atPath: dayURL.path)) ?? []).sorted(by: >) where call.hasSuffix("_call") {
                out.append(dayURL.appendingPathComponent(call))
            }
        }
        return out
    }

    static func exists(_ dir: URL, _ name: String) -> Bool { fm.fileExists(atPath: dir.appendingPathComponent(name).path) }

    static func hasTracks(_ dir: URL) -> Bool {
        ["me.caf", "others.caf", "me.m4a", "others.m4a"].contains { exists(dir, $0) }
    }

    static func isProcessing(_ dir: URL) -> Bool {
        guard let t = try? String(contentsOf: dir.appendingPathComponent(".processing"), encoding: .utf8),
              let pid = Int32(t.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        return kill(pid, 0) == 0
    }

    static func needsProcessing(_ dir: URL) -> Bool {
        hasTracks(dir) && !exists(dir, "transcript.json") && !isProcessing(dir)
    }

    static func title(_ dir: URL) -> String {
        let day = dir.deletingLastPathComponent().lastPathComponent
        let t = dir.lastPathComponent.prefix(4)
        let time = t.count == 4 ? "\(t.prefix(2)):\(t.suffix(2))" : String(t)
        let mark = isProcessing(dir) ? "  ⏳" : (needsProcessing(dir) ? "  ⚠︎" : "")
        return "\(day)  \(time)\(mark)"
    }
}

enum Postprocess {
    static func run(folder: URL, reprocess: Bool = false) {
        let script = projectRoot + "/transcribe.sh"
        guard FileManager.default.isExecutableFile(atPath: script) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script, folder.path] + (reprocess ? ["--reprocess"] : [])
        p.standardOutput = nil; p.standardError = nil
        try? p.run()
    }

    static func maintenance() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: projectRoot + "/venv/bin/python")
        p.arguments = [projectRoot + "/maintenance.py"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        p.standardOutput = nil; p.standardError = nil
        try? p.run()
    }

    /// Calls whose raw tracks are still on disk without a transcript (crash, restart, failed run).
    static func recoverUnprocessed(resetAttempts: Bool = false) {
        for dir in CallStore.calls() where CallStore.needsProcessing(dir) {
            try? CallStore.fm.removeItem(at: dir.appendingPathComponent(".recording"))
            if resetAttempts { try? CallStore.fm.removeItem(at: dir.appendingPathComponent(".attempts")) }
            log("processing pending call \(dir.path)")
            run(folder: dir)
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate, UNUserNotificationCenterDelegate {
    let recorder = Recorder()
    var item: NSStatusItem!
    var statusLine: NSMenuItem!
    var openItem: NSMenuItem!
    var loginItem: NSMenuItem!
    var skipItem: NSMenuItem!
    var skipUntilIdle = false
    var timer: Timer?
    var lastActive = Date.distantPast
    var busy = false
    var paused = false
    var retryAfter = Date.distantPast
    var window: NSWindow?
    var web: WKWebView?
    var server: Process?
    var iconCache: [Bool: NSImage] = [:]

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        statusLine = NSMenuItem(title: "Waiting for a call…", action: nil, keyEquivalent: "")
        menu.addItem(statusLine)
        menu.addItem(.separator())
        openItem = menu.addItem(withTitle: "Open Edison…", action: #selector(openWindow), keyEquivalent: "e")
        openItem.target = self
        menu.addItem(withTitle: "Open recordings folder", action: #selector(openFolder), keyEquivalent: "o").target = self
        skipItem = menu.addItem(withTitle: "Don't record this call", action: #selector(skipCall), keyEquivalent: "")
        skipItem.target = self
        loginItem = menu.addItem(withTitle: "Launch at login", action: #selector(toggleLogin), keyEquivalent: "")
        loginItem.target = self
        let pause = menu.addItem(withTitle: "Pause auto-detection", action: #selector(togglePause(_:)), keyEquivalent: "p")
        pause.target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(quit), keyEquivalent: "q").target = self
        menu.delegate = self
        item.menu = menu
        render()
        installMainMenu()
        // Silent start at login is on by default; the menu can turn it off.
        if !UserDefaults.standard.bool(forKey: "loginConfigured") {
            UserDefaults.standard.set(true, forKey: "loginConfigured")
            try? SMAppService.mainApp.register()
        }

        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { ok, _ in
            DispatchQueue.main.async { notificationsAllowed = ok }
        }
        // Trigger permission prompts early.
        AVCaptureDevice.requestAccess(for: .audio) { _ in }
        CGRequestScreenCaptureAccess()

        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.tick() }
        log("app started, folder \(baseFolder.path)")
        Postprocess.recoverUnprocessed()
        Postprocess.maintenance()
        Timer.scheduledTimer(withTimeInterval: 24 * 3600, repeats: true) { _ in Postprocess.maintenance() }
    }

    func tick() {
        checkDoneNotifications()
        guard !busy, !paused else { return }
        let active = CallDetector.teamsIsUsingMic()
        if active { lastActive = Date() }
        if !active && Date().timeIntervalSince(lastActive) > 6 { skipUntilIdle = false }
        if active && !recorder.isRecording && !skipUntilIdle && Date() >= retryAfter {
            if titleIsExcluded() { skipUntilIdle = true; log("not recording: excluded meeting title"); return }
            busy = true
            Task {
                if !(await recorder.start()) { retryAfter = Date().addingTimeInterval(60) }
                busy = false; render()
            }
        } else if recorder.isRecording && active && titleIsExcluded() {
            skipUntilIdle = true
            busy = true
            Task { await recorder.stop(discard: true); busy = false; render() }
        } else if !active && recorder.isRecording && Date().timeIntervalSince(lastActive) > 6 {
            busy = true
            Task { await recorder.stop(); busy = false; render() }
        }
    }

    @objc func skipCall() {
        skipUntilIdle = true
        log("call skipped by user")
        if recorder.isRecording {
            busy = true
            Task { await recorder.stop(discard: true); busy = false; render() }
        }
        render()
    }

    /// transcribe.sh drops a marker in .notify/ when a call has been processed.
    func checkDoneNotifications() {
        let fm = FileManager.default
        let dir = baseFolder.appendingPathComponent(".notify")
        guard let files = try? fm.contentsOfDirectory(atPath: dir.path), !files.isEmpty else { return }
        let enabled = (appConfig()["notify_when_done"] as? Bool) ?? true
        for f in files {
            let u = dir.appendingPathComponent(f)
            defer { try? fm.removeItem(at: u) }
            guard enabled, let id = try? String(contentsOf: u, encoding: .utf8),
                  !(window?.isVisible == true && NSApp.isActive),
                  let d = try? Data(contentsOf: baseFolder.appendingPathComponent(id).appendingPathComponent("transcript.json")),
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
            let title = (j["title"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "Call processed"
            let pending = ((j["clusters"] as? [[String: Any]]) ?? [])
                .filter { ["unknown", "suggested"].contains($0["status"] as? String ?? "") }.count
            let desc = (j["description"] as? String) ?? ""
            let body = pending > 0 ? "\(pending) speaker\(pending == 1 ? "" : "s") to identify" : desc
            notify("Edison · " + title, body, call: id)
        }
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let id = r.notification.request.content.userInfo["call"] as? String
        DispatchQueue.main.async { self.show(call: id) }
        done()
    }

    func glyph(recording: Bool) -> NSImage {
        if let c = iconCache[recording] { return c }
        let url = Bundle.main.url(forResource: "menubar@2x", withExtension: "png")
        guard let url, let base = NSImage(contentsOf: url) else {
            return NSImage(systemSymbolName: recording ? "record.circle.fill" : "record.circle", accessibilityDescription: nil)!
        }
        base.size = NSSize(width: 18, height: 18)
        var img = base
        if recording {
            img = NSImage(size: base.size, flipped: false) { r in
                base.draw(in: r)
                NSColor.systemRed.set()
                r.fill(using: .sourceAtop)
                return true
            }
        }
        img.isTemplate = !recording
        iconCache[recording] = img
        return img
    }

    func render() {
        let rec = recorder.isRecording
        item.button?.image = glyph(recording: rec)
        statusLine.title = rec ? "Recording call…" : (paused ? "Detection paused" : "Waiting for a call…")
    }

    @objc func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            if svc.status == .enabled { try svc.unregister() } else { try svc.register() }
        } catch {
            notify("Edison", "Could not change the login setting: \(error.localizedDescription)")
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        let inCall = recorder.isRecording || (CallDetector.teamsIsUsingMic() && !skipUntilIdle)
        skipItem.isEnabled = inCall
        skipItem.title = skipUntilIdle ? "Not recording this call" : "Don't record this call"
        skipItem.state = skipUntilIdle ? .on : .off
        if skipUntilIdle { skipItem.isEnabled = false }
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        let n = pendingSpeakers() + CallStore.calls().filter { CallStore.needsProcessing($0) }.count
        openItem.title = n > 0 ? "Open Edison (\(n) need attention)…" : "Open Edison…"
    }

    func pendingSpeakers() -> Int {
        let fm = FileManager.default
        var count = 0
        for day in (try? fm.contentsOfDirectory(atPath: baseFolder.path)) ?? [] {
            let dayURL = baseFolder.appendingPathComponent(day)
            for call in (try? fm.contentsOfDirectory(atPath: dayURL.path)) ?? [] where call.hasSuffix("_call") {
                let f = dayURL.appendingPathComponent(call).appendingPathComponent("transcript.json")
                guard let d = try? Data(contentsOf: f),
                      let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                      let cl = j["clusters"] as? [[String: Any]] else { continue }
                count += cl.filter { ["unknown", "suggested"].contains($0["status"] as? String ?? "") }.count
            }
        }
        return count
    }

    // MARK: window

    func installMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit"); editItem.submenu = edit
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = main
    }

    func ensureServer() {
        if let s = server, s.isRunning { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: projectRoot + "/venv/bin/python")
        p.arguments = [projectRoot + "/server.py"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        p.environment = ProcessInfo.processInfo.environment.merging(["EDISON_WATCH_PARENT": "1"]) { $1 }
        p.standardOutput = nil; p.standardError = nil
        do { try p.run(); server = p; log("manager server started") } catch { log("server failed: \(error)") }
    }

    @objc func openWindow() { show(call: nil) }

    func show(call: String?) {
        ensureServer()
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable],
                             backing: .buffered, defer: false)
            w.title = "Edison"
            w.minSize = NSSize(width: 860, height: 520)
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.setFrameAutosaveName("EdisonMain")
            let v = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            w.contentView = v
            w.center()
            window = w; web = v
        }
        NSApp.setActivationPolicy(.regular)
        if let icon = Bundle.main.url(forResource: "Edison", withExtension: "icns").flatMap({ NSImage(contentsOf: $0) }) {
            NSApp.applicationIconImage = icon
        }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if let call, let web, web.url?.host == "127.0.0.1" {
            let enc = call.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? call
            web.evaluateJavaScript("location.hash = 'call=\(enc)'")
        } else if web?.url?.host != "127.0.0.1" {
            loadUI(attempt: 0, call: call)
        }
    }

    func loadUI(attempt: Int, call: String? = nil) {
        let frag = call.map { "#call=" + ($0.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? $0) } ?? ""
        guard let web, let url = URL(string: "http://127.0.0.1:8765/" + frag) else { return }
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 2)
        req.httpMethod = "HEAD"
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 { web.load(URLRequest(url: url)) }
                else if attempt < 40 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self?.loadUI(attempt: attempt + 1, call: call) } }
                else { web.loadHTMLString("<body style='font:15px -apple-system;padding:40px'>Could not start the Edison manager. See recorder.log.</body>", baseURL: nil) }
            }
        }.resume()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { openWindow() }
        return true
    }

    func windowWillClose(_ n: Notification) {
        web?.loadHTMLString("", baseURL: nil)
        NSApp.setActivationPolicy(.accessory)
    }

    @objc func openFolder() {
        try? FileManager.default.createDirectory(at: baseFolder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(baseFolder)
    }

    @objc func togglePause(_ s: NSMenuItem) {
        paused.toggle()
        s.state = paused ? .on : .off
        render()
    }

    @objc func quit() {
        Task { @MainActor in await recorder.stop(); server?.terminate(); NSApp.terminate(nil) }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
