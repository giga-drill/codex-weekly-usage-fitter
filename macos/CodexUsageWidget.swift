import AppKit
import Foundation
import SQLite3

private struct UsageSnapshot {
    var weeklyPercent: Double?
    var previousWeeklyPercent: Double?
    var turnUsagePercent: Double?
    var turnTokens: Int64?
    var latestSampleAt: String?
    var model: String?
    var parseError: String?
    var todayUsagePercent: Double?
    var todayUsageLevel: String
    var sampleCount: Int64
    var sessionCount: Int64
}

private struct SampleRow {
    var weeklyPercent: Double?
    var turnTokens: Int64?
    var latestSampleAt: String?
    var model: String?
    var parseError: String?
}

private final class UsageReader {
    private let dbPath: String

    init(dbPath: String) {
        self.dbPath = dbPath
    }

    func read() -> UsageSnapshot {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            return UsageSnapshot(
                weeklyPercent: nil,
                previousWeeklyPercent: nil,
                turnUsagePercent: nil,
                turnTokens: nil,
                latestSampleAt: "database unavailable",
                model: nil,
                parseError: sqliteMessage(db),
                todayUsagePercent: nil,
                todayUsageLevel: "low",
                sampleCount: 0,
                sessionCount: 0
            )
        }
        defer { sqlite3_close(db) }

        let sampleCount = intScalar(db, "SELECT COUNT(*) FROM samples")
        let sessionCount = intScalar(db, "SELECT COUNT(*) FROM sessions")
        let rows = latestSamples(db)
        let latest = rows.first
        let previous = rows.dropFirst().first
        let turnUsagePercent = usageDelta(from: previous?.weeklyPercent, to: latest?.weeklyPercent)
        let today = todayUsage(db)
        return UsageSnapshot(
            weeklyPercent: latest?.weeklyPercent,
            previousWeeklyPercent: previous?.weeklyPercent,
            turnUsagePercent: turnUsagePercent,
            turnTokens: latest?.turnTokens,
            latestSampleAt: latest?.latestSampleAt,
            model: latest?.model,
            parseError: latest?.parseError,
            todayUsagePercent: today.percent,
            todayUsageLevel: today.level,
            sampleCount: sampleCount,
            sessionCount: sessionCount
        )
    }

    private func latestSamples(_ db: OpaquePointer?) -> [SampleRow] {
        let sql = """
        SELECT weekly_used_percent, last_total_tokens, observed_at, model, parse_error
        FROM samples
        ORDER BY id DESC
        LIMIT 2
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return [
                SampleRow(
                    weeklyPercent: nil,
                    turnTokens: nil,
                    latestSampleAt: nil,
                    model: nil,
                    parseError: sqliteMessage(db)
                )
            ]
        }
        defer { sqlite3_finalize(statement) }

        var rows: [SampleRow] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            rows.append(
                SampleRow(
                    weeklyPercent: doubleColumn(statement, 0),
                    turnTokens: intColumn(statement, 1),
                    latestSampleAt: stringColumn(statement, 2),
                    model: stringColumn(statement, 3),
                    parseError: stringColumn(statement, 4)
                )
            )
        }
        return rows
    }

    private func usageDelta(from previous: Double?, to current: Double?) -> Double? {
        guard let current, let previous else { return nil }
        return max(0, current - previous)
    }

    private func todayUsage(_ db: OpaquePointer?) -> (percent: Double?, level: String) {
        let bounds = todayBounds()
        let sql = """
        SELECT weekly_used_percent
        FROM samples
        WHERE observed_at >= ?
          AND observed_at < ?
          AND weekly_used_percent IS NOT NULL
        ORDER BY observed_at, id
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return (nil, "low")
        }
        defer { sqlite3_finalize(statement) }

        bindText(statement, index: 1, value: bounds.start)
        bindText(statement, index: 2, value: bounds.end)

        var firstPercent: Double?
        var lastPercent: Double?
        while sqlite3_step(statement) == SQLITE_ROW {
            let percent = sqlite3_column_double(statement, 0)
            if firstPercent == nil {
                firstPercent = percent
            }
            lastPercent = percent
        }

        guard let firstPercent, let lastPercent else {
            return (nil, "low")
        }
        let percent = max(0, lastPercent - firstPercent)
        return (percent, todayUsageLevel(percent))
    }

    private func intScalar(_ db: OpaquePointer?, _ sql: String) -> Int64 {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return 0
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW else {
            return 0
        }
        return sqlite3_column_int64(statement, 0)
    }
}

private final class DragView: NSView {
    override var mouseDownCanMoveWindow: Bool {
        true
    }
}

private final class DragLabel: NSTextField {
    override var mouseDownCanMoveWindow: Bool {
        true
    }
}

private final class UsageWindowController: NSObject {
    private let reader: UsageReader
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let window: NSPanel
    private let expandedSize = NSSize(width: 300, height: 156)
    private let compactSize = NSSize(width: 208, height: 72)
    private let titleLabel = DragLabel(labelWithString: "This week")
    private let weeklyLabel = DragLabel(labelWithString: "--%")
    private let transitionLabel = DragLabel(labelWithString: "Prev -- -> Now --")
    private let turnLabel = DragLabel(labelWithString: "Last turn -- / -- tokens")
    private let metaLabel = DragLabel(labelWithString: "")
    private let todayBadge = DragView()
    private let todayCaptionLabel = DragLabel(labelWithString: "Today")
    private let todayLevelLabel = DragLabel(labelWithString: "LOW")
    private let todayValueLabel = DragLabel(labelWithString: "+0.0%")
    private let toggleButton = NSButton(title: "-", target: nil, action: nil)
    private var timer: Timer?
    private var isCompact = false
    private var expandedToggleConstraints: [NSLayoutConstraint] = []
    private var compactToggleConstraints: [NSLayoutConstraint] = []

    init(reader: UsageReader) {
        self.reader = reader
        self.window = NSPanel(
            contentRect: NSRect(origin: .zero, size: expandedSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        super.init()
        configureWindow()
        configureStatusMenu()
        configureContent()
    }

    func start() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        RunLoop.main.add(timer!, forMode: .common)
        window.orderFrontRegardless()
    }

    private func configureWindow() {
        window.isOpaque = false
        window.backgroundColor = NSColor.clear
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        window.ignoresMouseEvents = false
        window.isMovableByWindowBackground = true
        window.hasShadow = true
        positionWindow()
    }

    private func configureStatusMenu() {
        statusItem.button?.title = "Codex --%"
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Show Usage", action: #selector(showWindow), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Hide Usage", action: #selector(hideWindow), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        for item in menu.items {
            item.target = self
        }
        statusItem.menu = menu
    }

    private func configureContent() {
        let root = DragView(frame: window.contentView?.bounds ?? .zero)
        root.translatesAutoresizingMaskIntoConstraints = false
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.72).cgColor
        root.layer?.cornerRadius = 12
        root.layer?.masksToBounds = true

        titleLabel.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        titleLabel.textColor = NSColor.white.withAlphaComponent(0.62)
        weeklyLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 34, weight: .semibold)
        weeklyLabel.textColor = .white
        transitionLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        transitionLabel.textColor = NSColor.white.withAlphaComponent(0.86)
        turnLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        turnLabel.textColor = NSColor.white.withAlphaComponent(0.86)
        metaLabel.font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        metaLabel.textColor = NSColor.white.withAlphaComponent(0.68)
        todayCaptionLabel.font = NSFont.systemFont(ofSize: 9, weight: .semibold)
        todayCaptionLabel.alignment = .center
        todayLevelLabel.font = NSFont.systemFont(ofSize: 12, weight: .bold)
        todayLevelLabel.alignment = .center
        todayValueLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .semibold)
        todayValueLabel.alignment = .center
        todayBadge.translatesAutoresizingMaskIntoConstraints = false
        todayBadge.wantsLayer = true
        todayBadge.layer?.cornerRadius = 8
        todayBadge.layer?.masksToBounds = true
        toggleButton.translatesAutoresizingMaskIntoConstraints = false
        toggleButton.wantsLayer = true
        toggleButton.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.96).cgColor
        toggleButton.layer?.cornerRadius = 11
        toggleButton.layer?.masksToBounds = true
        toggleButton.layer?.borderWidth = 0.5
        toggleButton.layer?.borderColor = NSColor.black.withAlphaComponent(0.18).cgColor
        toggleButton.isBordered = false
        toggleButton.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        toggleButton.contentTintColor = .black
        toggleButton.target = self
        toggleButton.action = #selector(toggleCompact)

        let stack = NSStackView(views: [titleLabel, weeklyLabel, transitionLabel, turnLabel, metaLabel])
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        let todayStack = NSStackView(views: [todayCaptionLabel, todayLevelLabel, todayValueLabel])
        todayStack.translatesAutoresizingMaskIntoConstraints = false
        todayStack.orientation = .vertical
        todayStack.alignment = .centerX
        todayStack.spacing = 2

        window.contentView = NSView()
        window.contentView?.addSubview(root)
        root.addSubview(stack)
        root.addSubview(todayBadge)
        root.addSubview(toggleButton)
        todayBadge.addSubview(todayStack)
        expandedToggleConstraints = [
            toggleButton.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -10),
            toggleButton.topAnchor.constraint(equalTo: root.topAnchor, constant: 10)
        ]
        compactToggleConstraints = [
            toggleButton.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -10),
            toggleButton.topAnchor.constraint(equalTo: root.topAnchor, constant: 10)
        ]

        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -16),
            stack.centerYAnchor.constraint(equalTo: root.centerYAnchor),
            todayBadge.trailingAnchor.constraint(equalTo: toggleButton.leadingAnchor, constant: -8),
            todayBadge.topAnchor.constraint(equalTo: root.topAnchor, constant: 14),
            todayBadge.widthAnchor.constraint(equalToConstant: 82),
            todayBadge.heightAnchor.constraint(equalToConstant: 48),
            todayStack.centerXAnchor.constraint(equalTo: todayBadge.centerXAnchor),
            todayStack.centerYAnchor.constraint(equalTo: todayBadge.centerYAnchor),
            toggleButton.widthAnchor.constraint(equalToConstant: 22),
            toggleButton.heightAnchor.constraint(equalToConstant: 22)
        ])
        applyViewMode(animated: false)
    }

    private func refresh() {
        let snapshot = reader.read()
        let percentText = snapshot.weeklyPercent.map { formatPercent($0) } ?? "--%"
        weeklyLabel.stringValue = percentText
        statusItem.button?.title = "Codex \(percentText)"
        let todayPercent = snapshot.todayUsagePercent.map { formatSignedPercent($0) } ?? "--"
        todayLevelLabel.stringValue = snapshot.todayUsageLevel.uppercased()
        todayValueLabel.stringValue = todayPercent
        applyTodayStyle(level: snapshot.todayUsageLevel)

        if let error = snapshot.parseError, !error.isEmpty {
            transitionLabel.stringValue = "Parse issue"
            turnLabel.stringValue = error
        } else if snapshot.sampleCount == 0 {
            transitionLabel.stringValue = "Prev -- -> Now --"
            turnLabel.stringValue = "Waiting for first sample"
        } else {
            let previous = snapshot.previousWeeklyPercent.map { formatPercent($0) } ?? "--%"
            transitionLabel.stringValue = "Prev \(previous) -> Now \(percentText)"
            let turnUsage = snapshot.turnUsagePercent.map { formatSignedPercent($0) } ?? "--"
            let turnTokens = formatTokens(snapshot.turnTokens)
            turnLabel.stringValue = "Last turn \(turnUsage) / \(turnTokens) tokens"
        }

        let model = snapshot.model ?? "unknown"
        let time = shortTime(snapshot.latestSampleAt)
        metaLabel.stringValue = "\(model)  \(time)"
    }

    @objc private func toggleCompact() {
        isCompact.toggle()
        applyViewMode(animated: true)
    }

    private func applyViewMode(animated: Bool) {
        titleLabel.isHidden = isCompact
        transitionLabel.isHidden = isCompact
        turnLabel.isHidden = isCompact
        metaLabel.isHidden = isCompact
        todayCaptionLabel.isHidden = isCompact
        todayValueLabel.isHidden = false
        weeklyLabel.font = NSFont.monospacedDigitSystemFont(
            ofSize: isCompact ? 28 : 34,
            weight: .semibold
        )
        toggleButton.title = isCompact ? "+" : "-"
        NSLayoutConstraint.deactivate(expandedToggleConstraints + compactToggleConstraints)
        NSLayoutConstraint.activate(isCompact ? compactToggleConstraints : expandedToggleConstraints)
        resizeWindow(to: isCompact ? compactSize : expandedSize, animated: animated)
    }

    private func resizeWindow(to size: NSSize, animated: Bool) {
        var frame = window.frame
        frame.origin.x = frame.maxX - size.width
        frame.origin.y = frame.maxY - size.height
        frame.size = size
        if animated {
            window.animator().setFrame(frame, display: true)
        } else {
            window.setFrame(frame, display: true)
        }
    }

    private func applyTodayStyle(level: String) {
        let style = todayStyle(level)
        todayBadge.layer?.backgroundColor = style.background.cgColor
        todayCaptionLabel.textColor = style.text.withAlphaComponent(0.72)
        todayLevelLabel.textColor = style.text
        todayValueLabel.textColor = style.text.withAlphaComponent(0.86)
    }

    private func positionWindow() {
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let size = window.frame.size
        window.setFrameOrigin(NSPoint(x: frame.maxX - size.width - 20, y: frame.maxY - size.height - 20))
    }

    @objc private func showWindow() {
        window.orderFrontRegardless()
    }

    @objc private func hideWindow() {
        window.orderOut(nil)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: UsageWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let dbPath = CommandLine.arguments.dropFirst().first
            ?? "\(NSHomeDirectory())/.codex/usage-monitor/usage.sqlite"
        controller = UsageWindowController(reader: UsageReader(dbPath: dbPath))
        controller?.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}

private func sqliteMessage(_ db: OpaquePointer?) -> String? {
    guard let message = sqlite3_errmsg(db) else { return nil }
    return String(cString: message)
}

private let sqliteTransient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private func bindText(_ statement: OpaquePointer?, index: Int32, value: String) {
    sqlite3_bind_text(statement, index, value, -1, sqliteTransient)
}

private func doubleColumn(_ statement: OpaquePointer?, _ index: Int32) -> Double? {
    sqlite3_column_type(statement, index) == SQLITE_NULL ? nil : sqlite3_column_double(statement, index)
}

private func intColumn(_ statement: OpaquePointer?, _ index: Int32) -> Int64? {
    sqlite3_column_type(statement, index) == SQLITE_NULL ? nil : sqlite3_column_int64(statement, index)
}

private func stringColumn(_ statement: OpaquePointer?, _ index: Int32) -> String? {
    guard sqlite3_column_type(statement, index) != SQLITE_NULL,
          let text = sqlite3_column_text(statement, index) else {
        return nil
    }
    return String(cString: text)
}

private func formatPercent(_ value: Double) -> String {
    if value.rounded() == value {
        return "\(Int(value))%"
    }
    return String(format: "%.1f%%", value)
}

private func formatSignedPercent(_ value: Double) -> String {
    if value == 0 {
        return "+0.0%"
    }
    return String(format: "+%.1f%%", value)
}

private func formatTokens(_ value: Int64?) -> String {
    guard let value else { return "--" }
    let absValue = abs(Double(value))
    if absValue >= 1_000_000 {
        return String(format: "%.1fM", Double(value) / 1_000_000)
    }
    if absValue >= 1_000 {
        return String(format: "%.1fk", Double(value) / 1_000)
    }
    return "\(value)"
}

private func todayBounds() -> (start: String, end: String) {
    let calendar = Calendar.current
    let start = calendar.startOfDay(for: Date())
    let end = calendar.date(byAdding: .day, value: 1, to: start) ?? start
    return (sqliteUTCString(start), sqliteUTCString(end))
}

private func sqliteUTCString(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.calendar = Calendar(identifier: .iso8601)
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'+00:00'"
    return formatter.string(from: date)
}

private func todayUsageLevel(_ value: Double) -> String {
    if value < 15 {
        return "low"
    }
    if value <= 28 {
        return "medium"
    }
    return "high"
}

private func todayStyle(_ level: String) -> (background: NSColor, text: NSColor) {
    switch level {
    case "medium":
        return (
            NSColor(calibratedRed: 0.95, green: 0.72, blue: 0.16, alpha: 0.95),
            NSColor(calibratedWhite: 0.08, alpha: 1)
        )
    case "high":
        return (
            NSColor(calibratedRed: 0.86, green: 0.18, blue: 0.18, alpha: 0.95),
            .white
        )
    default:
        return (
            NSColor(calibratedRed: 0.13, green: 0.62, blue: 0.34, alpha: 0.95),
            .white
        )
    }
}

private func shortTime(_ iso: String?) -> String {
    guard let iso, !iso.isEmpty else { return "no sample" }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    guard let date = formatter.date(from: iso) else { return iso }
    let out = DateFormatter()
    out.dateStyle = .none
    out.timeStyle = .short
    return out.string(from: date)
}

let app = NSApplication.shared
private let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
