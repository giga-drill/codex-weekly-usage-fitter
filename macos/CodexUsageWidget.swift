import AppKit
import Foundation
import SQLite3

private struct UsageSnapshot {
    var weeklyPercent: Double?
    var turnTokens: Int64?
    var latestSampleAt: String?
    var model: String?
    var reasoningEffort: String?
    var fitTurnsPerPercent: Double?
    var fitTokensPerPercent: Double?
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
    var reasoningEffort: String?
    var weeklyResetsAt: Int64?
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
                turnTokens: nil,
                latestSampleAt: "database unavailable",
                model: nil,
                reasoningEffort: nil,
                fitTurnsPerPercent: nil,
                fitTokensPerPercent: nil,
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
        let today = todayUsage(db)
        let fit = modelEffortFit(db, latest: latest)
        return UsageSnapshot(
            weeklyPercent: latest?.weeklyPercent,
            turnTokens: latest?.turnTokens,
            latestSampleAt: latest?.latestSampleAt,
            model: latest?.model,
            reasoningEffort: latest?.reasoningEffort,
            fitTurnsPerPercent: fit.turns,
            fitTokensPerPercent: fit.tokens,
            parseError: latest?.parseError,
            todayUsagePercent: today.percent,
            todayUsageLevel: today.level,
            sampleCount: sampleCount,
            sessionCount: sessionCount
        )
    }

    private func latestSamples(_ db: OpaquePointer?) -> [SampleRow] {
        let effortColumn = hasColumn(db, table: "samples", column: "reasoning_effort")
            ? "reasoning_effort"
            : "NULL AS reasoning_effort"
        let usableSql = """
        SELECT weekly_used_percent, last_total_tokens, observed_at, model, \(effortColumn), weekly_resets_at, parse_error
        FROM samples
        WHERE parse_error IS NULL
          AND weekly_used_percent IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
        let fallbackSql = """
        SELECT weekly_used_percent, last_total_tokens, observed_at, model, \(effortColumn), weekly_resets_at, parse_error
        FROM samples
        ORDER BY id DESC
        LIMIT 1
        """
        guard let rows = sampleRows(db, sql: usableSql) else {
            return [
                SampleRow(
                    weeklyPercent: nil,
                    turnTokens: nil,
                    latestSampleAt: nil,
                    model: nil,
                    reasoningEffort: nil,
                    weeklyResetsAt: nil,
                    parseError: sqliteMessage(db)
                )
            ]
        }
        if !rows.isEmpty {
            return rows
        }
        if let fallbackRows = sampleRows(db, sql: fallbackSql) {
            return fallbackRows
        }
        return [
            SampleRow(
                weeklyPercent: nil,
                turnTokens: nil,
                latestSampleAt: nil,
                model: nil,
                reasoningEffort: nil,
                weeklyResetsAt: nil,
                parseError: sqliteMessage(db)
            )
        ]
    }

    private func sampleRows(_ db: OpaquePointer?, sql: String) -> [SampleRow]? {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return nil
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
                    reasoningEffort: stringColumn(statement, 4),
                    weeklyResetsAt: intColumn(statement, 5),
                    parseError: stringColumn(statement, 6)
                )
            )
        }
        return rows
    }

    private func todayUsage(_ db: OpaquePointer?) -> (percent: Double?, level: String) {
        let bounds = todayBounds()
        let sql = """
        SELECT weekly_used_percent
        FROM samples
        WHERE observed_at >= ?
          AND observed_at < ?
          AND weekly_used_percent IS NOT NULL
          AND parse_error IS NULL
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

    private func modelEffortFit(_ db: OpaquePointer?, latest: SampleRow?) -> (turns: Double?, tokens: Double?) {
        guard let latest,
              let weeklyResetsAt = latest.weeklyResetsAt,
              hasColumn(db, table: "model_effort_fits", column: "turns_per_weekly_percent") else {
            return (nil, nil)
        }
        let model = latest.model ?? "unknown"
        let effort = normalizedEffort(latest.reasoningEffort) ?? "unknown"
        let sql = """
        SELECT turns_per_weekly_percent, tokens_per_weekly_percent
        FROM model_effort_fits
        WHERE weekly_resets_at = ?
          AND model = ?
          AND reasoning_effort = ?
        LIMIT 1
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return (nil, nil)
        }
        defer { sqlite3_finalize(statement) }

        sqlite3_bind_int64(statement, 1, weeklyResetsAt)
        bindText(statement, index: 2, value: model)
        bindText(statement, index: 3, value: effort)

        guard sqlite3_step(statement) == SQLITE_ROW else {
            return (nil, nil)
        }
        return (doubleColumn(statement, 0), doubleColumn(statement, 1))
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

    private func hasColumn(_ db: OpaquePointer?, table: String, column: String) -> Bool {
        let sql = "PRAGMA table_info(\(table))"
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return false
        }
        defer { sqlite3_finalize(statement) }
        while sqlite3_step(statement) == SQLITE_ROW {
            if stringColumn(statement, 1) == column {
                return true
            }
        }
        return false
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

private final class UsageWindowController: NSObject, NSWindowDelegate {
    private let reader: UsageReader
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let window: NSPanel
    private let expandedSize = NSSize(width: 243, height: 146)
    private let expandedContentWidth: CGFloat = 215
    private let compactSize = NSSize(width: 124, height: 118)
    private let titleLabel = DragLabel(labelWithString: "This week")
    private let weeklyLabel = DragLabel(labelWithString: "--%")
    private let detailPill = DragView()
    private let transitionLabel = DragLabel(labelWithString: "1% ~= -- turns / -- tok")
    private let turnLabel = DragLabel(labelWithString: "Last turn -- tokens")
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
    private var expandedLayoutConstraints: [NSLayoutConstraint] = []
    private var compactLayoutConstraints: [NSLayoutConstraint] = []
    private var targetFrame: NSRect?
    private var trustTargetFrameUntil = Date.distantPast

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
        window.delegate = self
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
        transitionLabel.translatesAutoresizingMaskIntoConstraints = false
        transitionLabel.lineBreakMode = .byClipping
        turnLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        turnLabel.textColor = NSColor.white.withAlphaComponent(0.86)
        turnLabel.lineBreakMode = .byClipping
        metaLabel.font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .bold)
        metaLabel.textColor = NSColor.white.withAlphaComponent(0.9)
        metaLabel.translatesAutoresizingMaskIntoConstraints = false
        metaLabel.lineBreakMode = .byClipping
        configurePill(detailPill, alpha: 0.15)
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
        toggleButton.layer?.cornerRadius = 9
        toggleButton.layer?.masksToBounds = true
        toggleButton.layer?.borderWidth = 0.5
        toggleButton.layer?.borderColor = NSColor.black.withAlphaComponent(0.18).cgColor
        toggleButton.isBordered = false
        toggleButton.font = NSFont.systemFont(ofSize: 10, weight: .semibold)
        toggleButton.contentTintColor = .black
        toggleButton.target = self
        toggleButton.action = #selector(toggleCompact)

        let detailStack = NSStackView(views: [transitionLabel, metaLabel])
        detailStack.translatesAutoresizingMaskIntoConstraints = false
        detailStack.orientation = .vertical
        detailStack.alignment = .leading
        detailStack.spacing = 2
        detailPill.addSubview(detailStack)
        let stack = NSStackView(views: [titleLabel, weeklyLabel, detailPill, turnLabel])
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
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
        expandedLayoutConstraints = [
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            stack.widthAnchor.constraint(equalToConstant: expandedContentWidth),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -14),
            stack.centerYAnchor.constraint(equalTo: root.centerYAnchor, constant: 2),
            detailPill.widthAnchor.constraint(equalToConstant: expandedContentWidth),
            turnLabel.widthAnchor.constraint(equalToConstant: expandedContentWidth),
            todayBadge.trailingAnchor.constraint(equalTo: toggleButton.leadingAnchor, constant: -1),
            todayBadge.topAnchor.constraint(equalTo: root.topAnchor, constant: 19),
            todayBadge.widthAnchor.constraint(equalToConstant: 70),
            todayBadge.heightAnchor.constraint(equalToConstant: 48)
        ]
        compactLayoutConstraints = [
            stack.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 20),
            todayBadge.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            todayBadge.topAnchor.constraint(equalTo: weeklyLabel.bottomAnchor, constant: 10),
            todayBadge.widthAnchor.constraint(equalToConstant: 74),
            todayBadge.heightAnchor.constraint(equalToConstant: 46)
        ]

        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
            todayStack.centerXAnchor.constraint(equalTo: todayBadge.centerXAnchor),
            todayStack.centerYAnchor.constraint(equalTo: todayBadge.centerYAnchor),
            detailPill.heightAnchor.constraint(equalToConstant: 42),
            detailStack.leadingAnchor.constraint(equalTo: detailPill.leadingAnchor, constant: 7),
            detailStack.trailingAnchor.constraint(equalTo: detailPill.trailingAnchor, constant: -7),
            detailStack.centerYAnchor.constraint(equalTo: detailPill.centerYAnchor),
            toggleButton.widthAnchor.constraint(equalToConstant: 18),
            toggleButton.heightAnchor.constraint(equalToConstant: 18)
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
            transitionLabel.stringValue = "1% ~= -- turns / -- tok"
            turnLabel.stringValue = "Waiting for first sample"
        } else {
            transitionLabel.stringValue = formatFitLine(
                turns: snapshot.fitTurnsPerPercent,
                tokens: snapshot.fitTokensPerPercent
            )
            let turnTokens = formatTokens(snapshot.turnTokens)
            turnLabel.stringValue = "Last turn \(turnTokens) tokens"
        }

        let model = modelDisplayName(snapshot.model, effort: snapshot.reasoningEffort)
        let time = shortTime(snapshot.latestSampleAt)
        metaLabel.attributedStringValue = metaLine(model: model, time: time)
    }

    @objc private func toggleCompact() {
        isCompact.toggle()
        applyViewMode(animated: true)
    }

    private func applyViewMode(animated: Bool) {
        titleLabel.isHidden = isCompact
        detailPill.isHidden = isCompact
        turnLabel.isHidden = isCompact
        todayCaptionLabel.isHidden = isCompact
        todayValueLabel.isHidden = false
        weeklyLabel.font = NSFont.monospacedDigitSystemFont(
            ofSize: isCompact ? 28 : 34,
            weight: .semibold
        )
        toggleButton.title = isCompact ? "+" : "-"
        NSLayoutConstraint.deactivate(
            expandedToggleConstraints + compactToggleConstraints + expandedLayoutConstraints + compactLayoutConstraints
        )
        NSLayoutConstraint.activate(isCompact ? compactToggleConstraints : expandedToggleConstraints)
        NSLayoutConstraint.activate(isCompact ? compactLayoutConstraints : expandedLayoutConstraints)
        resizeWindow(to: isCompact ? compactSize : expandedSize, animated: animated)
    }

    private func resizeWindow(to size: NSSize, animated: Bool) {
        let sourceFrame = resizeSourceFrame()
        var frame = sourceFrame
        let rightEdge = sourceFrame.maxX
        frame.origin.y = frame.maxY - size.height
        frame.origin.x = rightEdge - size.width
        frame.size = size
        targetFrame = frame
        trustTargetFrameUntil = animated ? Date().addingTimeInterval(0.35) : .distantPast
        window.setFrame(frame, display: true, animate: animated)
    }

    private func resizeSourceFrame() -> NSRect {
        if Date() < trustTargetFrameUntil, let targetFrame {
            return targetFrame
        }
        return window.frame
    }

    func windowDidMove(_ notification: Notification) {
        guard Date() >= trustTargetFrameUntil else {
            return
        }
        targetFrame = window.frame
    }

    private func applyTodayStyle(level: String) {
        let style = todayStyle(level)
        todayBadge.layer?.backgroundColor = style.background.cgColor
        todayCaptionLabel.textColor = style.text.withAlphaComponent(0.72)
        todayLevelLabel.textColor = style.text
        todayValueLabel.textColor = style.text.withAlphaComponent(0.86)
    }

    private func configurePill(_ view: DragView, alpha: CGFloat) {
        view.translatesAutoresizingMaskIntoConstraints = false
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.white.withAlphaComponent(alpha).cgColor
        view.layer?.cornerRadius = 7
        view.layer?.masksToBounds = true
        view.layer?.borderWidth = 0.5
        view.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
    }

    private func positionWindow() {
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let size = window.frame.size
        window.setFrameOrigin(NSPoint(x: frame.maxX - size.width - 20, y: frame.maxY - size.height - 20))
        targetFrame = window.frame
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
    return formatTokenCount(Double(value))
}

private func formatTokenCount(_ value: Double) -> String {
    let absValue = abs(Double(value))
    if absValue >= 1_000_000 {
        return String(format: "%.1fM", value / 1_000_000)
    }
    if absValue >= 1_000 {
        return String(format: "%.1fk", value / 1_000)
    }
    return String(format: "%.0f", value)
}

private func formatFitLine(turns: Double?, tokens: Double?) -> String {
    guard let turns, let tokens else {
        return "1% ~= -- turns / -- tok"
    }
    return "1% ~= \(formatTurns(turns)) turns / \(formatTokenCount(tokens)) tok"
}

private func formatTurns(_ value: Double) -> String {
    if value >= 10 {
        return String(format: "%.0f", value)
    }
    return String(format: "%.1f", value)
}

private func modelDisplayName(_ model: String?, effort: String?) -> String {
    let modelName = model ?? "unknown"
    guard let effortLabel = effortDisplayName(effort) else {
        return modelName
    }
    return "\(modelName) (\(effortLabel))"
}

private func metaLine(model: String, time: String) -> NSAttributedString {
    let output = NSMutableAttributedString(
        string: model,
        attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 12.5, weight: .bold),
            .foregroundColor: NSColor.white.withAlphaComponent(0.9),
        ]
    )
    output.append(
        NSAttributedString(
            string: "  \(time)",
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular),
                .foregroundColor: NSColor.white.withAlphaComponent(0.55),
            ]
        )
    )
    return output
}

private func effortDisplayName(_ effort: String?) -> String? {
    guard let normalized = normalizedEffort(effort) else { return nil }
    switch normalized {
    case "xhigh":
        return "x high"
    default:
        return normalized
    }
}

private func normalizedEffort(_ effort: String?) -> String? {
    guard let effort else { return nil }
    let normalized = effort
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .lowercased()
        .replacingOccurrences(of: "-", with: " ")
        .replacingOccurrences(of: "_", with: " ")
    let compact = normalized.replacingOccurrences(of: " ", with: "")
    switch compact {
    case "":
        return nil
    case "xhigh", "extrahigh":
        return "xhigh"
    default:
        return normalized
    }
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
