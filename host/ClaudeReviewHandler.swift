import Cocoa

// Mac-side URL shim for the agent-pr-review:// scheme.
//
// This app does three things: validate the PR URL and hand off
// to the launcher on the configured Linux development host:
//
//     ssh -t <ssh-host> -- .local/bin/agent-pr-review '<pr-url>' --cli <claude|agent>
//
// Everything that makes a review happen - repo discovery, worktree preparation,
// session identity, prompt assembly, launching claude/agent - lives in vm/agent-pr-review
// and runs in the guest, where the repos and the agent CLIs are. Behavior changes go
// there, not here. T3 launches run over SSH without opening a terminal.

let HOME_DIR = FileManager.default.homeDirectoryForCurrentUser.path
let PRIMARY_URL_SCHEME = "agent-pr-review"
let REVIEW_SUPPORT_DIR_NAME = "AgentPRReview"
let LEGACY_REVIEW_SUPPORT_DIR_NAMES = ["GitHubPRReview", "ClaudeReview"]
let REVIEW_LOG_FILE_NAME = "agent-pr-review.log"

// The ssh alias of the Linux dev host. Configurable so the same shim can target the
// local VM or remote development host. The legacy SSH_HOST_FILE remains supported.
let CONFIG_FILE = "\(HOME_DIR)/.config/agent-pr-review/config.json"
let SSH_HOST_FILE = "\(HOME_DIR)/.config/agent-pr-review/ssh-host"
// Relative on purpose: the remote command runs in the guest $HOME, and a literal ~
// or $HOME here would be expanded by the Mac-side shell before ssh ever sees it.
let VM_REVIEW_CLI = ".local/bin/agent-pr-review"

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

    func configuration() throws -> [String: Any] {
        guard FileManager.default.fileExists(atPath: CONFIG_FILE) else { return [:] }
        let data = try Data(contentsOf: URL(fileURLWithPath: CONFIG_FILE))
        guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "AgentPRReview", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid config.json"])
        }
        return config
    }

    func resolveSSHHost(_ config: [String: Any]) -> String? {
        let legacy = (try? String(contentsOfFile: SSH_HOST_FILE, encoding: .utf8))?
            .split(separator: "\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first(where: { !$0.isEmpty })
        let host = config["ssh_host"] as? String ?? legacy ?? ""
        guard host.range(of: "^[A-Za-z0-9_][A-Za-z0-9._-]*$", options: .regularExpression) != nil else {
            showError("Set ssh_host in \(CONFIG_FILE) to your development host's SSH alias.")
            return nil
        }
        return host
    }

    func launchT3(sshHost: String, prURL: String) {
        // The remote server owns the review; leave the Mac app and focus untouched.
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ClearAllForwardings=yes", "--", sshHost,
            "\(VM_REVIEW_CLI) '\(shellEscape(prURL))' --cli t3code"]
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
                showError("Could not start the review on \(sshHost). See \(reviewLogPath()) for details.")
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
            -- Launch as the session's command rather than typing into a shell:
            -- the default profile may itself be an ssh-into-the-VM session, and
            -- written text lands inside the guest (where ble.sh also swallows
            -- pasted newlines into MULTILINE mode instead of executing).
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

    // agent-pr-review://<host>/<owner>/<repo>/pull/<number>?cli=claude|agent
    //   -> https://<host>/<owner>/<repo>/pull/<number>  +  --cli <claude|agent>
    //
    // Validated here only so a malformed URL fails on the Mac with a log line instead
    // of opening a terminal that immediately errors out. The VM CLI re-parses the URL
    // and derives owner/repo/number itself; --cli selects Claude Code vs Cursor agent.
    @objc func handleURL(_ event: NSAppleEventDescriptor, withReply reply: NSAppleEventDescriptor) {
        defer { NSApplication.shared.terminate(nil) }

        guard let urlString = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue else {
            log("ERROR: no URL in event")
            return
        }
        log("Received URL: \(urlString)")

        guard let components = URLComponents(string: urlString),
              components.scheme == PRIMARY_URL_SCHEME,
              components.user == nil, components.password == nil, components.port == nil,
              let host = components.host?.lowercased() else {
            log("ERROR: invalid URL")
            return
        }

        let config: [String: Any]
        do { config = try configuration() }
        catch { showError(error.localizedDescription); return }
        let hosts = (config["github_hosts"] as? [String] ?? ["github.com"]).map { $0.lowercased() }
        guard hosts.contains(host),
              components.percentEncodedPath.range(of: "^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$", options: .regularExpression) != nil else {
            showError("Invalid PR URL or host missing from github_hosts in \(CONFIG_FILE).")
            return
        }
        let pathParts = components.path.lowercased().split(separator: "/").map(String.init)
        guard !pathParts.prefix(2).contains(where: { $0 == "." || $0 == ".." }) else { return }

        let cliRaw = components.queryItems?
            .first(where: { $0.name == "cli" })?
            .value?
            .lowercased() ?? (config["default_cli"] as? String ?? "agent")
        let cli: String
        switch cliRaw {
        case "claude", "agent", "t3code":
            cli = cliRaw
        default:
            showError("Unknown review backend: \(cliRaw)")
            return
        }

        let prURL = "https://" + ([host] + pathParts).joined(separator: "/")
        log("PR URL: \(prURL)")
        log("CLI: \(cli)")

        ensureReviewSupportDir()
        guard let sshHost = resolveSSHHost(config) else { return }
        if cli == "t3code" {
            launchT3(sshHost: sshHost, prURL: prURL)
            return
        }
        let remoteCommand = "\(VM_REVIEW_CLI) '\(shellEscape(prURL))' --cli '\(shellEscape(cli))'"
        let shellCmd = "ssh -t \(sshHost) -- '\(shellEscape(remoteCommand))'"
        log("Shell command: \(shellCmd)")
        _ = launchITerm(shellCmd: shellCmd)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
