import Cocoa

// macOS URL shim. Shared Python code validates configuration and dispatches to
// the runtime via SSH, devcontainer exec, or locally. This app only owns Mac UI.

let HOME_DIR = FileManager.default.homeDirectoryForCurrentUser.path
let PRIMARY_URL_SCHEME = "agent-pr-review"
let REVIEW_SUPPORT_DIR_NAME = "AgentPRReview"
let LEGACY_REVIEW_SUPPORT_DIR_NAMES = ["GitHubPRReview", "ClaudeReview"]
let REVIEW_LOG_FILE_NAME = "agent-pr-review.log"

let LAUNCH_HELPER = "\(HOME_DIR)/Library/Application Support/AgentPRReview/native/launch-review.py"

func shellEscape(_ value: String) -> String {
    value.replacingOccurrences(of: "'", with: "'\\''")
}

func reviewSupportDir() -> String {
    "\(HOME_DIR)/Library/Application Support/\(REVIEW_SUPPORT_DIR_NAME)"
}

func legacyReviewSupportDirs() -> [String] {
    LEGACY_REVIEW_SUPPORT_DIR_NAMES.map { "\(HOME_DIR)/Library/Application Support/\($0)" }
}

func ensureReviewSupportDir() {
    let fileManager = FileManager.default
    let targetDir = reviewSupportDir()

    if !fileManager.fileExists(atPath: targetDir) {
        for legacyDir in legacyReviewSupportDirs() where fileManager.fileExists(atPath: legacyDir) {
            try? fileManager.moveItem(atPath: legacyDir, toPath: targetDir)
            break
        }
    }

    try? fileManager.createDirectory(atPath: targetDir, withIntermediateDirectories: true, attributes: nil)
}

func reviewLogPath() -> String {
    "\(reviewSupportDir())/\(REVIEW_LOG_FILE_NAME)"
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleURL(_:withReply:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )
    }

    func log(_ msg: String) {
        ensureReviewSupportDir()
        let logFile = reviewLogPath()
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let ts = formatter.string(from: Date())
        let line = "\(ts) \(msg)\n"
        if let fh = FileHandle(forWritingAtPath: logFile) {
            fh.seekToEndOfFile()
            fh.write(line.data(using: .utf8)!)
            fh.closeFile()
        } else {
            FileManager.default.createFile(atPath: logFile, contents: line.data(using: .utf8))
        }
    }

    func describeURL(_ url: String) -> [String: String]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: LAUNCH_HELPER)
        process.arguments = ["--describe", url]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                showError(String(data: data, encoding: .utf8) ?? "Invalid review configuration")
                return nil
            }
            return try JSONSerialization.jsonObject(with: data) as? [String: String]
        } catch {
            showError("Could not validate review request: \(error.localizedDescription). Rerun host/install.sh.")
            return nil
        }
    }

    func launchT3(url: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: LAUNCH_HELPER)
        process.arguments = [url]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            // Drain before waiting: git diagnostics can exceed the pipe buffer.
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            log(String(data: data, encoding: .utf8) ?? "")
            guard process.terminationStatus == 0 else {
                showError("Could not start the review in the configured environment. See \(reviewLogPath()) for details.")
                return
            }
        } catch {
            log("T3 launch error: \(error)")
            showError("Could not launch T3 review: \(error.localizedDescription)")
        }
    }

    func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Agent PR Review"
        alert.informativeText = message
        alert.alertStyle = .warning
        NSApplication.shared.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    func launchITerm(shellCmd: String) -> Bool {
        let script = """
        on run argv
            set shellCommand to item 1 of argv

        tell application "iTerm2"
            activate
            -- Start a new process even when the default profile is a remote shell.
            if (count of windows) = 0 then
                create window with default profile command shellCommand
            else
                tell current window
                    create tab with default profile command shellCommand
                end tell
            end if
        end tell
        end run
        """

        log("Running AppleScript...")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script, shellCmd]
        let errPipe = Pipe()
        process.standardError = errPipe
        try? process.run()
        process.waitUntilExit()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        let errStr = String(data: errData, encoding: .utf8) ?? ""
        log("osascript exit code: \(process.terminationStatus), stderr: \(errStr)")
        return process.terminationStatus == 0
    }

    @objc func handleURL(_ event: NSAppleEventDescriptor, withReply reply: NSAppleEventDescriptor) {
        defer { NSApplication.shared.terminate(nil) }

        guard let urlString = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue else {
            log("ERROR: no URL in event")
            return
        }
        log("Received URL: \(urlString)")

        guard urlString.hasPrefix("\(PRIMARY_URL_SCHEME)://"),
              let request = describeURL(urlString), let cli = request["cli"] else { return }
        ensureReviewSupportDir()
        if cli == "t3code" {
            launchT3(url: urlString)
            return
        }
        let shellCmd = "'\(shellEscape(LAUNCH_HELPER))' '\(shellEscape(urlString))'"
        if !launchITerm(shellCmd: shellCmd) {
            showError("Could not open iTerm2. See \(reviewLogPath()) for details.")
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
