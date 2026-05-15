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

private enum BillingPeriodSelection: Int {
    case current = 0
    case previous = 1

    var title: String {
        switch self {
        case .current:
            return "This billing period"
        case .previous:
            return "Last billing period"
        }
    }
}

private struct BillingMetricSnapshot {
    var start: Date
    var end: Date
    var usagePercentDelta: Double
    var tokenDeltaTotal: Int64
    var turnCount: Int

    var avgTokensPerTurn: Double? {
        guard turnCount > 0 else { return nil }
        return Double(tokenDeltaTotal) / Double(turnCount)
    }
}

private struct BillingWindowSnapshot {
    var metric: BillingMetricSnapshot
    var days: [BillingDaySnapshot]
}

private struct BillingStatsSnapshot {
    var label: String
    var period: BillingMetricSnapshot
    var windows: [BillingWindowSnapshot]
    var mixedEvents: [BillingMixedMovementEvent]
    var mixedCombinations: [BillingMixedMovementCombination]
}

private struct BillingDaySnapshot {
    var metric: BillingMetricSnapshot
    var samples: [BillingSampleDetail]
}

private struct BillingSampleDetail {
    var observedAt: Date
    var weeklyUsedPercent: Double?
    var usagePercentDelta: Double
    var tokenDelta: Int64
    var model: String?
    var reasoningEffort: String?
    var turnId: String?
}

private struct BillingSampleRow {
    var observedAt: Date
    var weeklyUsedPercent: Double?
    var usagePercentDelta: Double
    var tokenDelta: Int64
    var model: String?
    var reasoningEffort: String?
    var turnId: String?
}

private struct BillingMovementBucket {
    var model: String
    var reasoningEffort: String
    var tokenDeltaTotal: Int64
    var turnCount: Int
}

private struct BillingMixedMovementEvent {
    var observedAt: Date
    var percentDelta: Double
    var tokenDeltaTotal: Int64
    var turnCount: Int
    var combination: String
    var buckets: [BillingMovementBucket]
}

private struct BillingMixedMovementCombination {
    var combination: String
    var eventCount: Int
    var percentDelta: Double
    var tokenDeltaTotal: Int64
    var turnCount: Int
}

private struct UsageSettings: Codable {
    var billingDay: Int

    static let `default` = UsageSettings(billingDay: 12)
}

private final class SettingsStore {
    private let path: String
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    init(path: String = "\(NSHomeDirectory())/.codex/usage-monitor/settings.json") {
        self.path = path
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    }

    func load() -> UsageSettings {
        guard let data = FileManager.default.contents(atPath: path) else {
            return .default
        }
        guard let decoded = try? decoder.decode(UsageSettings.self, from: data) else {
            return .default
        }
        let day = min(31, max(1, decoded.billingDay))
        return UsageSettings(billingDay: day)
    }

    func save(_ settings: UsageSettings) {
        let day = min(31, max(1, settings.billingDay))
        let normalized = UsageSettings(billingDay: day)
        guard let data = try? encoder.encode(normalized) else { return }
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(
            atPath: dir,
            withIntermediateDirectories: true,
            attributes: nil
        )
        FileManager.default.createFile(atPath: path, contents: data)
    }
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

        let sampleCount = intScalar(
            db,
            "SELECT COUNT(*) FROM conversation_turns WHERE completed = 1"
        )
        let rawSampleCount = intScalar(db, "SELECT COUNT(*) FROM samples")
        let sessionCount = intScalar(db, "SELECT COUNT(*) FROM sessions")
        var rows = latestSamples(db)
        if rows.isEmpty, rawSampleCount > 0 {
            rows = [
                SampleRow(
                    weeklyPercent: nil,
                    turnTokens: nil,
                    latestSampleAt: nil,
                    model: nil,
                    reasoningEffort: nil,
                    weeklyResetsAt: nil,
                    parseError: "conversation turns unavailable"
                )
            ]
        }
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

    func billingStats(
        billingDay: Int,
        selection: BillingPeriodSelection,
        now: Date = Date()
    ) -> BillingStatsSnapshot {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            let empty = BillingMetricSnapshot(
                start: now,
                end: now,
                usagePercentDelta: 0,
                tokenDeltaTotal: 0,
                turnCount: 0
            )
            return BillingStatsSnapshot(
                label: selection.title,
                period: empty,
                windows: [],
                mixedEvents: [],
                mixedCombinations: []
            )
        }
        defer { sqlite3_close(db) }

        let clampedDay = min(31, max(1, billingDay))
        let calendar = calendarInCurrentTimeZone()
        let currentStart = billingPeriodStart(now: now, billingDay: clampedDay, calendar: calendar)
        let currentEnd = addBillingMonths(
            from: currentStart,
            months: 1,
            billingDay: clampedDay,
            calendar: calendar
        )
        let periodStart: Date
        let periodEnd: Date
        let label: String
        switch selection {
        case .current:
            periodStart = currentStart
            periodEnd = currentEnd
            label = BillingPeriodSelection.current.title
        case .previous:
            periodEnd = currentStart
            periodStart = addBillingMonths(
                from: periodEnd,
                months: -1,
                billingDay: clampedDay,
                calendar: calendar
            )
            label = BillingPeriodSelection.previous.title
        }

        let rows = billingSampleRows(db, endDate: periodEnd)
        let windowRanges = billingWindowBounds(
            periodStart: periodStart,
            periodEnd: periodEnd,
            calendar: calendar
        )
        var periodAccumulator = BillingMetricAccumulator(start: periodStart, end: periodEnd)
        var windowAccumulators = windowRanges.map { BillingMetricAccumulator(start: $0.start, end: $0.end) }
        var dayAccumulators = windowRanges.map { bounds in
            dayBounds(windowStart: bounds.start, windowEnd: bounds.end, calendar: calendar)
                .map { BillingMetricAccumulator(start: $0.start, end: $0.end) }
        }
        var daySamples = dayAccumulators.map { dayArray in
            dayArray.map { _ in [BillingSampleDetail]() }
        }

        for row in rows {
            let movement = max(0, row.usagePercentDelta)

            if periodStart <= row.observedAt && row.observedAt < periodEnd {
                periodAccumulator.addSample(tokenDelta: row.tokenDelta, usageDelta: movement)
                for windowIndex in windowAccumulators.indices where windowAccumulators[windowIndex].contains(row.observedAt) {
                    windowAccumulators[windowIndex].addSample(tokenDelta: row.tokenDelta, usageDelta: movement)
                    for dayIndex in dayAccumulators[windowIndex].indices where dayAccumulators[windowIndex][dayIndex].contains(row.observedAt) {
                        dayAccumulators[windowIndex][dayIndex].addSample(
                            tokenDelta: row.tokenDelta,
                            usageDelta: movement
                        )
                        daySamples[windowIndex][dayIndex].append(
                            BillingSampleDetail(
                                observedAt: row.observedAt,
                                weeklyUsedPercent: row.weeklyUsedPercent,
                                usagePercentDelta: movement,
                                tokenDelta: row.tokenDelta,
                                model: row.model,
                                reasoningEffort: row.reasoningEffort,
                                turnId: row.turnId
                            )
                        )
                        break
                    }
                    break
                }
            }
        }

        let windows = windowAccumulators.enumerated().map { index, metric in
            BillingWindowSnapshot(
                metric: metric.snapshot(),
                days: dayAccumulators[index].enumerated().map { dayIndex, dayMetric in
                    BillingDaySnapshot(
                        metric: dayMetric.snapshot(),
                        samples: daySamples[index][dayIndex]
                    )
                }
            )
        }
        let mixedEvents = mixedMovementEvents(
            db,
            startDate: periodStart,
            endDate: periodEnd
        )
        let mixedCombinations = mixedMovementCombinations(from: mixedEvents)
        return BillingStatsSnapshot(
            label: label,
            period: periodAccumulator.snapshot(),
            windows: windows,
            mixedEvents: mixedEvents,
            mixedCombinations: mixedCombinations
        )
    }

    private func latestSamples(_ db: OpaquePointer?) -> [SampleRow] {
        let effortColumn = hasColumn(db, table: "conversation_turns", column: "reasoning_effort")
            ? "reasoning_effort"
            : "NULL AS reasoning_effort"
        let usableSql = """
        SELECT
            weekly_used_percent_end AS weekly_percent,
            token_delta,
            end_observed_at AS observed_at,
            model,
            \(effortColumn),
            weekly_resets_at
        FROM conversation_turns
        WHERE completed = 1
          AND weekly_used_percent_end IS NOT NULL
        ORDER BY end_observed_at DESC, id DESC
        LIMIT 1
        """
        let fallbackSql = """
        SELECT
            weekly_used_percent_end AS weekly_percent,
            token_delta,
            end_observed_at AS observed_at,
            model,
            \(effortColumn),
            weekly_resets_at
        FROM conversation_turns
        WHERE completed = 1
        ORDER BY end_observed_at DESC, id DESC
        LIMIT 1
        """
        guard let rows = sampleRows(db, sql: usableSql) else { return [] }
        if !rows.isEmpty {
            return rows
        }
        if let fallbackRows = sampleRows(db, sql: fallbackSql) {
            return fallbackRows
        }
        return []
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
                    parseError: nil
                )
            )
        }
        return rows
    }

    private func todayUsage(_ db: OpaquePointer?) -> (percent: Double?, level: String) {
        let bounds = todayBounds()
        let sql = """
        SELECT
            COALESCE(SUM(weekly_percent_delta), 0),
            COUNT(*)
        FROM conversation_turns
        WHERE completed = 1
          AND end_observed_at >= ?
          AND end_observed_at < ?
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return (nil, "low")
        }
        defer { sqlite3_finalize(statement) }

        bindText(statement, index: 1, value: bounds.start)
        bindText(statement, index: 2, value: bounds.end)

        guard sqlite3_step(statement) == SQLITE_ROW else {
            return (nil, "low")
        }
        let turnCount = sqlite3_column_int64(statement, 1)
        guard turnCount > 0 else {
            return (nil, "low")
        }
        let percent = sqlite3_column_double(statement, 0)
        return (percent, todayUsageLevel(percent))
    }

    private func modelEffortFit(_ db: OpaquePointer?, latest: SampleRow?) -> (turns: Double?, tokens: Double?) {
        guard let latest,
              hasColumn(db, table: "model_effort_fits", column: "turns_per_weekly_percent") else {
            return (nil, nil)
        }
        let model = latest.model ?? "unknown"
        let effort = normalizedEffort(latest.reasoningEffort) ?? "unknown"
        if hasColumn(db, table: "model_effort_global_fits", column: "turns_per_weekly_percent") {
            let sql = """
            SELECT turns_per_weekly_percent, tokens_per_weekly_percent
            FROM model_effort_global_fits
            WHERE model = ?
              AND reasoning_effort = ?
            LIMIT 1
            """
            if let fit = queryModelEffortFit(db, sql: sql, bindings: [model, effort]) {
                return fit
            }
        }
        guard let weeklyResetsAt = latest.weeklyResetsAt else {
            return (nil, nil)
        }
        let sql = """
        SELECT turns_per_weekly_percent, tokens_per_weekly_percent
        FROM model_effort_fits
        WHERE weekly_resets_at = ?
          AND model = ?
          AND reasoning_effort = ?
        LIMIT 1
        """
        return queryModelEffortFit(db, sql: sql, int64Binding: weeklyResetsAt, bindings: [model, effort]) ?? (nil, nil)
    }

    private func queryModelEffortFit(
        _ db: OpaquePointer?,
        sql: String,
        int64Binding: Int64? = nil,
        bindings: [String]
    ) -> (turns: Double?, tokens: Double?)? {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return nil
        }
        defer { sqlite3_finalize(statement) }

        var index: Int32 = 1
        if let int64Binding {
            sqlite3_bind_int64(statement, index, int64Binding)
            index += 1
        }
        for value in bindings {
            bindText(statement, index: index, value: value)
            index += 1
        }

        guard sqlite3_step(statement) == SQLITE_ROW else {
            return nil
        }
        return (doubleColumn(statement, 0), doubleColumn(statement, 1))
    }

    private func billingSampleRows(_ db: OpaquePointer?, endDate: Date) -> [BillingSampleRow] {
        let sql = """
        SELECT
            end_observed_at,
            weekly_used_percent_end,
            weekly_percent_delta,
            token_delta,
            model,
            reasoning_effort,
            last_internal_turn_id
        FROM conversation_turns
        WHERE completed = 1
          AND end_observed_at < ?
        ORDER BY end_observed_at, id
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return []
        }
        defer { sqlite3_finalize(statement) }
        bindText(statement, index: 1, value: sqliteUTCString(endDate))

        var rows: [BillingSampleRow] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            guard let observedText = stringColumn(statement, 0),
                  let observedAt = parseSQLiteTimestamp(observedText) else {
                continue
            }
            rows.append(
                BillingSampleRow(
                    observedAt: observedAt,
                    weeklyUsedPercent: doubleColumn(statement, 1),
                    usagePercentDelta: doubleColumn(statement, 2) ?? 0,
                    tokenDelta: intColumn(statement, 3) ?? 0,
                    model: stringColumn(statement, 4),
                    reasoningEffort: stringColumn(statement, 5),
                    turnId: stringColumn(statement, 6)
                )
            )
        }
        return rows
    }

    private func mixedMovementEvents(
        _ db: OpaquePointer?,
        startDate: Date,
        endDate: Date
    ) -> [BillingMixedMovementEvent] {
        guard hasColumn(db, table: "usage_movement_events", column: "buckets_json") else {
            return []
        }
        let sql = """
        SELECT observed_at, percent_delta, token_delta_total, turn_count, buckets_json
        FROM usage_movement_events
        WHERE observed_at >= ?
          AND observed_at < ?
          AND bucket_count > 1
        ORDER BY observed_at, id
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            return []
        }
        defer { sqlite3_finalize(statement) }
        bindText(statement, index: 1, value: sqliteUTCString(startDate))
        bindText(statement, index: 2, value: sqliteUTCString(endDate))

        var output: [BillingMixedMovementEvent] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            guard let observedText = stringColumn(statement, 0),
                  let observedAt = parseSQLiteTimestamp(observedText) else {
                continue
            }
            let buckets = parseMovementBuckets(stringColumn(statement, 4))
            let combination = movementCombinationLabel(buckets)
            output.append(
                BillingMixedMovementEvent(
                    observedAt: observedAt,
                    percentDelta: doubleColumn(statement, 1) ?? 0,
                    tokenDeltaTotal: intColumn(statement, 2) ?? 0,
                    turnCount: Int(intColumn(statement, 3) ?? 0),
                    combination: combination,
                    buckets: buckets
                )
            )
        }
        return output
    }

    private func mixedMovementCombinations(
        from events: [BillingMixedMovementEvent]
    ) -> [BillingMixedMovementCombination] {
        var summary: [String: BillingMixedMovementCombination] = [:]
        for event in events {
            if var bucket = summary[event.combination] {
                bucket.eventCount += 1
                bucket.percentDelta += event.percentDelta
                bucket.tokenDeltaTotal += event.tokenDeltaTotal
                bucket.turnCount += event.turnCount
                summary[event.combination] = bucket
            } else {
                summary[event.combination] = BillingMixedMovementCombination(
                    combination: event.combination,
                    eventCount: 1,
                    percentDelta: event.percentDelta,
                    tokenDeltaTotal: event.tokenDeltaTotal,
                    turnCount: event.turnCount
                )
            }
        }
        return summary.values.sorted {
            if $0.percentDelta == $1.percentDelta {
                return $0.tokenDeltaTotal > $1.tokenDeltaTotal
            }
            return $0.percentDelta > $1.percentDelta
        }
    }

    private func parseMovementBuckets(_ text: String?) -> [BillingMovementBucket] {
        guard let text,
              let data = text.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data),
              let rows = value as? [[String: Any]] else {
            return []
        }
        var output: [BillingMovementBucket] = []
        output.reserveCapacity(rows.count)
        for row in rows {
            let modelText = (row["model"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let rawEffort = row["reasoning_effort"] as? String
            let effort = normalizedEffort(rawEffort) ?? "unknown"
            let tokenValue = row["token_delta_total"] as? NSNumber
            let turnValue = row["turn_count"] as? NSNumber
            let model = (modelText?.isEmpty == false ? modelText : nil) ?? "unknown"
            output.append(
                BillingMovementBucket(
                    model: model,
                    reasoningEffort: effort,
                    tokenDeltaTotal: tokenValue?.int64Value ?? 0,
                    turnCount: turnValue?.intValue ?? 0
                )
            )
        }
        return output
    }

    private func movementCombinationLabel(_ buckets: [BillingMovementBucket]) -> String {
        if buckets.isEmpty {
            return "unknown"
        }
        let labels = buckets.map { bucket in
            "\(bucket.model)/\(bucket.reasoningEffort)"
        }
        return labels.sorted().joined(separator: " + ")
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

private struct BillingMetricAccumulator {
    let start: Date
    let end: Date
    var usagePercentDelta: Double = 0
    var tokenDeltaTotal: Int64 = 0
    var turnCount: Int = 0

    mutating func addSample(tokenDelta: Int64, usageDelta: Double) {
        usagePercentDelta += usageDelta
        tokenDeltaTotal += tokenDelta
        if tokenDelta > 0 {
            turnCount += 1
        }
    }

    func contains(_ date: Date) -> Bool {
        start <= date && date < end
    }

    func snapshot() -> BillingMetricSnapshot {
        BillingMetricSnapshot(
            start: start,
            end: end,
            usagePercentDelta: usagePercentDelta,
            tokenDeltaTotal: tokenDeltaTotal,
            turnCount: turnCount
        )
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

private final class VerticallyCenteredTextFieldCell: NSTextFieldCell {
    private func adjustedFrame(toVerticallyCenterText rect: NSRect) -> NSRect {
        var titleRect = super.titleRect(forBounds: rect)
        let minimumHeight = cellSize(forBounds: rect).height
        let heightDelta = titleRect.height - minimumHeight
        if heightDelta > 0 {
            titleRect.origin.y += floor(heightDelta / 2.0)
            titleRect.size.height = minimumHeight
        }
        return titleRect
    }

    override func titleRect(forBounds rect: NSRect) -> NSRect {
        adjustedFrame(toVerticallyCenterText: rect)
    }

    override func drawInterior(withFrame cellFrame: NSRect, in controlView: NSView) {
        super.drawInterior(withFrame: adjustedFrame(toVerticallyCenterText: cellFrame), in: controlView)
    }

    override func edit(
        withFrame rect: NSRect,
        in controlView: NSView,
        editor textObj: NSText,
        delegate: Any?,
        event: NSEvent?
    ) {
        super.edit(
            withFrame: adjustedFrame(toVerticallyCenterText: rect),
            in: controlView,
            editor: textObj,
            delegate: delegate,
            event: event
        )
    }

    override func select(
        withFrame rect: NSRect,
        in controlView: NSView,
        editor textObj: NSText,
        delegate: Any?,
        start selStart: Int,
        length selLength: Int
    ) {
        super.select(
            withFrame: adjustedFrame(toVerticallyCenterText: rect),
            in: controlView,
            editor: textObj,
            delegate: delegate,
            start: selStart,
            length: selLength
        )
    }
}

private final class UsageWindowController: NSObject, NSWindowDelegate {
    private let reader: UsageReader
    private let settingsStore = SettingsStore()
    private var settings: UsageSettings
    private let statsController: StatsWindowController
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let window: NSPanel
    private let expandedSize = NSSize(width: 243, height: 146)
    private let expandedContentWidth: CGFloat = 215
    private let compactSize = NSSize(width: 124, height: 118)
    private let titleLabel = DragLabel(labelWithString: "This week")
    private let weeklyLabel = DragLabel(labelWithString: "--%")
    private let detailPill = DragView()
    private let transitionLabel = DragLabel(labelWithString: "1% ~= -- conversations / -- tok")
    private let turnLabel = DragLabel(labelWithString: "Last conversation -- tokens")
    private let metaLabel = DragLabel(labelWithString: "")
    private let todayBadge = DragView()
    private let todayCaptionLabel = DragLabel(labelWithString: "Today")
    private let todayLevelLabel = DragLabel(labelWithString: "LOW")
    private let todayValueLabel = DragLabel(labelWithString: "+0.0%")
    private let statsButton = NSButton(title: "s", target: nil, action: nil)
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
        self.settings = .default
        self.statsController = StatsWindowController(
            reader: reader,
            settingsStore: settingsStore,
            initialSettings: .default
        )
        self.window = NSPanel(
            contentRect: NSRect(origin: .zero, size: expandedSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        super.init()
        self.settings = settingsStore.load()
        self.statsController.updateSettings(settings)
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
        menu.addItem(NSMenuItem(title: "Show Stats", action: #selector(showStats), keyEquivalent: ""))
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
        statsButton.translatesAutoresizingMaskIntoConstraints = false
        statsButton.wantsLayer = true
        statsButton.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.96).cgColor
        statsButton.layer?.cornerRadius = 9
        statsButton.layer?.masksToBounds = true
        statsButton.layer?.borderWidth = 0.5
        statsButton.layer?.borderColor = NSColor.black.withAlphaComponent(0.18).cgColor
        statsButton.isBordered = false
        statsButton.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .semibold)
        statsButton.contentTintColor = .black
        statsButton.target = self
        statsButton.action = #selector(showStats)
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
        root.addSubview(statsButton)
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
            statsButton.centerXAnchor.constraint(equalTo: toggleButton.centerXAnchor),
            statsButton.topAnchor.constraint(equalTo: toggleButton.bottomAnchor, constant: 6),
            statsButton.widthAnchor.constraint(equalToConstant: 18),
            statsButton.heightAnchor.constraint(equalToConstant: 18),
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
            transitionLabel.stringValue = "1% ~= -- conversations / -- tok"
            turnLabel.stringValue = "Waiting for first conversation"
        } else {
            transitionLabel.stringValue = formatFitLine(
                turns: snapshot.fitTurnsPerPercent,
                tokens: snapshot.fitTokensPerPercent
            )
            let turnTokens = formatTokens(snapshot.turnTokens)
            turnLabel.stringValue = "Last conversation \(turnTokens) tokens"
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

    @objc private func showStats() {
        statsController.show(anchor: resizeSourceFrame())
    }

    @objc private func hideWindow() {
        window.orderOut(nil)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}

private enum StatsTreeKind {
    case week
    case day
    case sample
    case mixedSection
    case mixedCombination
    case mixedEvent
}

private final class StatsTreeNode {
    let text: String
    let kind: StatsTreeKind
    let children: [StatsTreeNode]

    init(text: String, kind: StatsTreeKind, children: [StatsTreeNode] = []) {
        self.text = text
        self.kind = kind
        self.children = children
    }
}

private final class StatsWindowController: NSObject, NSWindowDelegate, NSOutlineViewDataSource, NSOutlineViewDelegate {
    private let reader: UsageReader
    private let settingsStore: SettingsStore
    private var settings: UsageSettings
    private let window: NSPanel
    private var periodSelection: BillingPeriodSelection = .current
    private let periodThisButton = NSButton(title: "This period", target: nil, action: nil)
    private let periodLastButton = NSButton(title: "Last period", target: nil, action: nil)
    private let billingDayField = NSTextField(string: "")
    private let billingDayStepper = NSStepper()
    private let saveButton = NSButton(title: "Save", target: nil, action: nil)
    private let titleLabel = NSTextField(labelWithString: "Billing Stats")
    private let rangeLabel = NSTextField(labelWithString: "--")
    private let usageLabel = NSTextField(labelWithString: "Usage: --")
    private let tokenLabel = NSTextField(labelWithString: "Tokens: --")
    private let turnLabel = NSTextField(labelWithString: "Conversations: --")
    private let avgLabel = NSTextField(labelWithString: "Avg / conversation: --")
    private let cleanFitLabel = NSTextField(labelWithString: "Clean 1%: --")
    private let mixedLabel = NSTextField(labelWithString: "Mixed: --")
    private let outlineView = NSOutlineView(frame: .zero)
    private let scrollView = NSScrollView()
    private var treeRoots: [StatsTreeNode] = []
    private var collapseOnNextRefresh = true
    private let statsWindowWidth: CGFloat = 430
    private let minStatsWindowHeight: CGFloat = 240
    private let maxStatsWindowHeight: CGFloat = 560
    private let minOutlineHeight: CGFloat = 70

    init(reader: UsageReader, settingsStore: SettingsStore, initialSettings: UsageSettings) {
        self.reader = reader
        self.settingsStore = settingsStore
        self.settings = initialSettings
        self.window = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 430, height: 360),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        super.init()
        configureWindow()
        configureContent()
        applySettingsToInputs()
        applyPeriodButtonStyles()
    }

    func updateSettings(_ settings: UsageSettings) {
        self.settings = settings
        applySettingsToInputs()
        if window.isVisible {
            collapseOnNextRefresh = true
            refresh()
        }
    }

    func show(anchor: NSRect? = nil) {
        collapseOnNextRefresh = true
        refresh()
        if let anchor {
            position(near: anchor)
        }
        ensureVisibleFrame()
        NSApp.activate(ignoringOtherApps: true)
        window.orderFrontRegardless()
        window.makeKey()
    }

    private func configureWindow() {
        window.title = "Codex Usage Stats"
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: statsWindowWidth, height: minStatsWindowHeight)
        window.maxSize = NSSize(width: statsWindowWidth, height: maxStatsWindowHeight)
        window.delegate = self
        if let screen = NSScreen.main {
            let frame = screen.visibleFrame
            window.setFrameOrigin(NSPoint(x: frame.maxX - 450, y: frame.maxY - 560))
        }
    }

    private func ensureVisibleFrame() {
        let visibleFrames = NSScreen.screens.map(\.visibleFrame)
        let current = window.frame
        let isVisible = visibleFrames.contains { $0.intersects(current) }
        if !isVisible, let screen = NSScreen.main {
            let frame = screen.visibleFrame
            window.setFrameOrigin(NSPoint(x: frame.maxX - window.frame.width - 30, y: frame.maxY - window.frame.height - 80))
        }
    }

    private func position(near anchor: NSRect) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let gap: CGFloat = 12
        var x = anchor.maxX - window.frame.width
        var y = anchor.minY - window.frame.height - gap
        if y < visible.minY + 10 {
            y = anchor.maxY + gap
        }
        x = max(visible.minX + 10, min(x, visible.maxX - window.frame.width - 10))
        y = max(visible.minY + 10, min(y, visible.maxY - window.frame.height - 10))
        window.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func configureContent() {
        let root = NSView(frame: .zero)
        root.translatesAutoresizingMaskIntoConstraints = false
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(calibratedWhite: 0.11, alpha: 0.98).cgColor
        window.contentView = root

        titleLabel.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        titleLabel.textColor = .white
        titleLabel.translatesAutoresizingMaskIntoConstraints = false

        rangeLabel.font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .medium)
        rangeLabel.textColor = NSColor.white.withAlphaComponent(0.72)
        rangeLabel.translatesAutoresizingMaskIntoConstraints = false

        configurePeriodButton(periodThisButton, action: #selector(selectThisPeriod))
        configurePeriodButton(periodLastButton, action: #selector(selectLastPeriod))

        billingDayField.cell = VerticallyCenteredTextFieldCell(textCell: "")
        billingDayField.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        billingDayField.alignment = .center
        billingDayField.isEditable = true
        billingDayField.isSelectable = true
        billingDayField.backgroundColor = NSColor.white.withAlphaComponent(0.95)
        billingDayField.drawsBackground = false
        billingDayField.textColor = .black
        billingDayField.isBordered = false
        billingDayField.focusRingType = .none
        billingDayField.wantsLayer = true
        billingDayField.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.95).cgColor
        billingDayField.layer?.cornerRadius = 6
        billingDayField.layer?.masksToBounds = true
        billingDayField.translatesAutoresizingMaskIntoConstraints = false

        billingDayStepper.minValue = 1
        billingDayStepper.maxValue = 31
        billingDayStepper.increment = 1
        billingDayStepper.target = self
        billingDayStepper.action = #selector(changeBillingDayByStepper)
        billingDayStepper.translatesAutoresizingMaskIntoConstraints = false

        saveButton.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        saveButton.isBordered = false
        saveButton.wantsLayer = true
        saveButton.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.95).cgColor
        saveButton.layer?.cornerRadius = 6
        saveButton.layer?.masksToBounds = true
        saveButton.contentTintColor = .black
        saveButton.target = self
        saveButton.action = #selector(saveBillingDay)
        saveButton.translatesAutoresizingMaskIntoConstraints = false

        usageLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        tokenLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        turnLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        avgLabel.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        cleanFitLabel.font = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .medium)
        mixedLabel.font = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .medium)
        [usageLabel, tokenLabel, turnLabel, avgLabel, cleanFitLabel, mixedLabel].forEach { label in
            label.textColor = NSColor.white.withAlphaComponent(0.9)
            label.translatesAutoresizingMaskIntoConstraints = false
        }
        let periodSummaryRow = NSStackView(views: [usageLabel, tokenLabel, turnLabel])
        periodSummaryRow.orientation = .horizontal
        periodSummaryRow.alignment = .firstBaseline
        periodSummaryRow.spacing = 14
        periodSummaryRow.translatesAutoresizingMaskIntoConstraints = false

        let detailSummaryStack = NSStackView(views: [avgLabel, cleanFitLabel, mixedLabel])
        detailSummaryStack.orientation = .vertical
        detailSummaryStack.alignment = .leading
        detailSummaryStack.spacing = 3
        detailSummaryStack.translatesAutoresizingMaskIntoConstraints = false

        let summaryStack = NSStackView(views: [periodSummaryRow, detailSummaryStack])
        summaryStack.orientation = .vertical
        summaryStack.alignment = .leading
        summaryStack.spacing = 5
        summaryStack.translatesAutoresizingMaskIntoConstraints = false

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("detail"))
        column.resizingMask = .autoresizingMask
        outlineView.addTableColumn(column)
        outlineView.outlineTableColumn = column
        outlineView.headerView = nil
        outlineView.selectionHighlightStyle = .none
        outlineView.rowSizeStyle = .small
        outlineView.indentationPerLevel = 14
        outlineView.focusRingType = .none
        outlineView.usesAlternatingRowBackgroundColors = false
        outlineView.backgroundColor = NSColor(calibratedWhite: 1.0, alpha: 0.08)
        outlineView.delegate = self
        outlineView.dataSource = self
        outlineView.translatesAutoresizingMaskIntoConstraints = true
        outlineView.autoresizingMask = [.width]

        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = false
        scrollView.documentView = outlineView

        root.addSubview(titleLabel)
        root.addSubview(rangeLabel)
        root.addSubview(periodThisButton)
        root.addSubview(periodLastButton)
        root.addSubview(billingDayField)
        root.addSubview(billingDayStepper)
        root.addSubview(saveButton)
        root.addSubview(summaryStack)
        root.addSubview(scrollView)

        NSLayoutConstraint.activate([
            billingDayField.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            billingDayField.topAnchor.constraint(equalTo: root.topAnchor, constant: 12),
            billingDayField.widthAnchor.constraint(equalToConstant: 44),
            billingDayField.heightAnchor.constraint(equalToConstant: 24),

            billingDayStepper.leadingAnchor.constraint(equalTo: billingDayField.trailingAnchor, constant: 6),
            billingDayStepper.centerYAnchor.constraint(equalTo: billingDayField.centerYAnchor),

            saveButton.leadingAnchor.constraint(equalTo: billingDayStepper.trailingAnchor, constant: 8),
            saveButton.centerYAnchor.constraint(equalTo: billingDayField.centerYAnchor),
            saveButton.widthAnchor.constraint(equalToConstant: 44),
            saveButton.heightAnchor.constraint(equalToConstant: 24),

            periodThisButton.leadingAnchor.constraint(equalTo: saveButton.trailingAnchor, constant: 10),
            periodThisButton.centerYAnchor.constraint(equalTo: billingDayField.centerYAnchor),
            periodThisButton.widthAnchor.constraint(equalToConstant: 84),
            periodThisButton.heightAnchor.constraint(equalToConstant: 24),

            periodLastButton.leadingAnchor.constraint(equalTo: periodThisButton.trailingAnchor, constant: 8),
            periodLastButton.centerYAnchor.constraint(equalTo: billingDayField.centerYAnchor),
            periodLastButton.widthAnchor.constraint(equalToConstant: 84),
            periodLastButton.heightAnchor.constraint(equalToConstant: 24),
            periodLastButton.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -14),

            titleLabel.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            titleLabel.topAnchor.constraint(equalTo: billingDayField.bottomAnchor, constant: 10),
            rangeLabel.leadingAnchor.constraint(equalTo: titleLabel.trailingAnchor, constant: 8),
            rangeLabel.centerYAnchor.constraint(equalTo: titleLabel.centerYAnchor),
            rangeLabel.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -14),

            summaryStack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            summaryStack.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 12),
            summaryStack.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -14),

            scrollView.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            scrollView.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -14),
            scrollView.topAnchor.constraint(equalTo: summaryStack.bottomAnchor, constant: 12),
            scrollView.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -14)
        ])
    }

    private func configurePeriodButton(_ button: NSButton, action: Selector) {
        button.translatesAutoresizingMaskIntoConstraints = false
        button.isBordered = false
        button.wantsLayer = true
        button.layer?.cornerRadius = 7
        button.layer?.masksToBounds = true
        button.layer?.borderWidth = 0.5
        button.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        button.target = self
        button.action = action
    }

    private func applyPeriodButtonStyles() {
        stylePeriodButton(periodThisButton, selected: periodSelection == .current)
        stylePeriodButton(periodLastButton, selected: periodSelection == .previous)
    }

    private func stylePeriodButton(_ button: NSButton, selected: Bool) {
        if selected {
            button.layer?.backgroundColor = NSColor.systemBlue.withAlphaComponent(0.95).cgColor
            button.layer?.borderColor = NSColor.systemBlue.withAlphaComponent(0.95).cgColor
            button.contentTintColor = .white
        } else {
            button.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.2).cgColor
            button.layer?.borderColor = NSColor.white.withAlphaComponent(0.4).cgColor
            button.contentTintColor = NSColor.white.withAlphaComponent(0.92)
        }
    }

    private func applySettingsToInputs() {
        let day = min(31, max(1, settings.billingDay))
        billingDayField.stringValue = "\(day)"
        billingDayStepper.doubleValue = Double(day)
    }

    private func refresh() {
        let stats = reader.billingStats(
            billingDay: settings.billingDay,
            selection: periodSelection
        )
        let latestSnapshot = reader.read()
        titleLabel.stringValue = stats.label
        rangeLabel.stringValue = "\(formatMonthDay(stats.period.start)) - \(formatMonthDay(stats.period.end))"
        usageLabel.stringValue = "Usage: +\(formatPercentCompact(stats.period.usagePercentDelta))%"
        tokenLabel.stringValue = "Tokens: \(formatTokenCount(Double(stats.period.tokenDeltaTotal))) tok"
        turnLabel.stringValue = "Conversations: \(stats.period.turnCount)"
        if let avg = stats.period.avgTokensPerTurn {
            avgLabel.stringValue = "Avg / conversation: \(formatTokenCount(avg)) tok"
        } else {
            avgLabel.stringValue = "Avg / conversation: --"
        }
        let modelText = modelDisplayName(latestSnapshot.model, effort: latestSnapshot.reasoningEffort)
        if let turns = latestSnapshot.fitTurnsPerPercent,
           let tokens = latestSnapshot.fitTokensPerPercent {
            cleanFitLabel.stringValue = "Clean 1% (\(modelText)): \(formatTurns(turns)) conversations / \(formatTokenCount(tokens)) tok"
        } else {
            cleanFitLabel.stringValue = "Clean 1% (\(modelText)): waiting for clean movement"
        }
        if stats.mixedEvents.isEmpty {
            mixedLabel.stringValue = "Mixed: none in this period"
        } else {
            let totalPercent = stats.mixedCombinations.reduce(0.0) { $0 + $1.percentDelta }
            mixedLabel.stringValue = "Mixed: \(stats.mixedEvents.count) events / +\(formatPercentCompact(totalPercent))%"
        }
        treeRoots = buildTree(stats: stats)
        outlineView.reloadData()
        if collapseOnNextRefresh {
            collapseAll()
            collapseOnNextRefresh = false
        }
        syncOutlineDocumentSize()
        resizeWindowToVisibleRows(animated: window.isVisible)
    }

    private func buildTree(stats: BillingStatsSnapshot) -> [StatsTreeNode] {
        var roots = stats.windows.map { window in
            let dayChildren = window.days.map { day -> StatsTreeNode in
                let sampleChildren = day.samples.map { sample in
                    StatsTreeNode(
                        text: sampleLine(sample),
                        kind: .sample
                    )
                }
                return StatsTreeNode(
                    text: dayLine(day.metric),
                    kind: .day,
                    children: sampleChildren
                )
            }
            return StatsTreeNode(
                text: weekLine(window.metric),
                kind: .week,
                children: dayChildren
            )
        }
        let mixedCombinationNodes = stats.mixedCombinations.map { combination in
            let eventNodes = stats.mixedEvents
                .filter { $0.combination == combination.combination }
                .map { event in
                    StatsTreeNode(
                        text: mixedEventLine(event),
                        kind: .mixedEvent
                    )
                }
            return StatsTreeNode(
                text: mixedCombinationLine(combination),
                kind: .mixedCombination,
                children: eventNodes
            )
        }
        if mixedCombinationNodes.isEmpty {
            roots.append(
                StatsTreeNode(
                    text: "Mixed movement observations   none in this period",
                    kind: .mixedSection
                )
            )
        } else {
            let totalPercent = stats.mixedCombinations.reduce(0.0) { $0 + $1.percentDelta }
            let totalTokens = stats.mixedCombinations.reduce(Int64(0)) { $0 + $1.tokenDeltaTotal }
            let totalTurns = stats.mixedCombinations.reduce(0) { $0 + $1.turnCount }
            roots.append(
                StatsTreeNode(
                    text: "Mixed movement observations   +\(formatPercentCompact(totalPercent))%   \(formatTokenCount(Double(totalTokens))) tok   \(totalTurns) conv",
                    kind: .mixedSection,
                    children: mixedCombinationNodes
                )
            )
        }
        return roots
    }

    private func weekLine(_ metric: BillingMetricSnapshot) -> String {
        "\(formatMonthDay(metric.start)) - \(formatMonthDay(metric.end))   +\(formatPercentCompact(metric.usagePercentDelta))%   \(formatTokenCount(Double(metric.tokenDeltaTotal))) tok   \(metric.turnCount) conv"
    }

    private func dayLine(_ metric: BillingMetricSnapshot) -> String {
        "\(formatMonthDay(metric.start))   +\(formatPercentCompact(metric.usagePercentDelta))%   \(formatTokenCount(Double(metric.tokenDeltaTotal))) tok   \(metric.turnCount) conv"
    }

    private func sampleLine(_ sample: BillingSampleDetail) -> String {
        let movement = "+\(formatPercentCompact(sample.usagePercentDelta))%"
        let tokenText = "\(formatTokenCount(Double(sample.tokenDelta))) tok"
        let model = modelDisplayName(sample.model, effort: sample.reasoningEffort)
        return "\(movement)  \(tokenText)  \(model)"
    }

    private func mixedCombinationLine(_ combination: BillingMixedMovementCombination) -> String {
        "\(combination.combination)   \(combination.eventCount)x   +\(formatPercentCompact(combination.percentDelta))%   \(formatTokenCount(Double(combination.tokenDeltaTotal))) tok   \(combination.turnCount) conv"
    }

    private func mixedEventLine(_ event: BillingMixedMovementEvent) -> String {
        let at = formatMonthDay(event.observedAt) + " " + formatHourMinute(event.observedAt)
        return "\(at)   +\(formatPercentCompact(event.percentDelta))%   \(formatTokenCount(Double(event.tokenDeltaTotal))) tok   \(event.turnCount) conv"
    }

    private func collapseAll() {
        for root in treeRoots {
            outlineView.collapseItem(root, collapseChildren: true)
        }
    }

    private func syncOutlineDocumentSize() {
        let width = max(1, scrollView.contentSize.width)
        let rowHeight = max(14, outlineView.rowHeight + outlineView.intercellSpacing.height)
        let rowCount = max(1, outlineView.numberOfRows)
        let height = CGFloat(rowCount) * rowHeight + 2

        var frame = outlineView.frame
        frame.size.width = width
        frame.size.height = height
        outlineView.frame = frame

        var clipOrigin = scrollView.contentView.bounds.origin
        let maxOriginY = max(0, height - scrollView.contentSize.height)
        if clipOrigin.y > maxOriginY {
            clipOrigin.y = maxOriginY
            scrollView.contentView.scroll(to: clipOrigin)
        }
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    private func resizeWindowToVisibleRows(animated: Bool) {
        guard let root = window.contentView else { return }
        root.layoutSubtreeIfNeeded()
        syncOutlineDocumentSize()
        outlineView.layoutSubtreeIfNeeded()

        let fixedHeight = max(0, root.bounds.height - scrollView.frame.height)
        let rowHeight = max(14, outlineView.rowHeight + outlineView.intercellSpacing.height)
        let visibleRowCount = max(1, outlineView.numberOfRows)
        let idealOutlineHeight = max(minOutlineHeight, CGFloat(visibleRowCount) * rowHeight + 8)
        let clampedOutlineHeight = min(idealOutlineHeight, maxStatsWindowHeight - fixedHeight)
        let targetHeight = min(maxStatsWindowHeight, max(minStatsWindowHeight, fixedHeight + clampedOutlineHeight))

        let oldFrame = window.frame
        if abs(oldFrame.height - targetHeight) < 0.5 {
            return
        }

        var newFrame = oldFrame
        newFrame.size.width = statsWindowWidth
        newFrame.size.height = targetHeight
        newFrame.origin.y = oldFrame.maxY - targetHeight

        let visible = window.screen?.visibleFrame ?? NSScreen.main?.visibleFrame
        if let visible {
            newFrame.origin.x = min(max(newFrame.origin.x, visible.minX + 8), visible.maxX - newFrame.width - 8)
            newFrame.origin.y = min(max(newFrame.origin.y, visible.minY + 8), visible.maxY - newFrame.height - 8)
        }
        window.setFrame(newFrame, display: true, animate: animated)
    }

    func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
        if let node = item as? StatsTreeNode {
            return node.children.count
        }
        return treeRoots.count
    }

    func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
        if let node = item as? StatsTreeNode {
            return node.children[index]
        }
        return treeRoots[index]
    }

    func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool {
        guard let node = item as? StatsTreeNode else { return false }
        return !node.children.isEmpty
    }

    func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any) -> NSView? {
        guard let node = item as? StatsTreeNode else { return nil }
        let identifier = NSUserInterfaceItemIdentifier("detailCell")
        let cell: NSTableCellView
        if let reused = outlineView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView {
            cell = reused
        } else {
            cell = NSTableCellView()
            cell.identifier = identifier
            let textField = NSTextField(labelWithString: "")
            textField.translatesAutoresizingMaskIntoConstraints = false
            textField.lineBreakMode = .byTruncatingTail
            cell.addSubview(textField)
            cell.textField = textField
            NSLayoutConstraint.activate([
                textField.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
                textField.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                textField.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
            ])
        }
        cell.textField?.stringValue = node.text
        switch node.kind {
        case .week:
            cell.textField?.font = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .semibold)
            cell.textField?.textColor = NSColor.white.withAlphaComponent(0.96)
        case .day:
            cell.textField?.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)
            cell.textField?.textColor = NSColor.white.withAlphaComponent(0.88)
        case .sample:
            cell.textField?.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
            cell.textField?.textColor = NSColor.white.withAlphaComponent(0.72)
        case .mixedSection:
            cell.textField?.font = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .semibold)
            cell.textField?.textColor = NSColor.systemOrange.withAlphaComponent(0.96)
        case .mixedCombination:
            cell.textField?.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)
            cell.textField?.textColor = NSColor.white.withAlphaComponent(0.86)
        case .mixedEvent:
            cell.textField?.font = NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular)
            cell.textField?.textColor = NSColor.white.withAlphaComponent(0.72)
        }
        return cell
    }

    func outlineViewItemDidExpand(_ notification: Notification) {
        syncOutlineDocumentSize()
        resizeWindowToVisibleRows(animated: true)
    }

    func outlineViewItemDidCollapse(_ notification: Notification) {
        syncOutlineDocumentSize()
        resizeWindowToVisibleRows(animated: true)
    }

    @objc private func selectThisPeriod() {
        periodSelection = .current
        applyPeriodButtonStyles()
        collapseOnNextRefresh = true
        refresh()
    }

    @objc private func selectLastPeriod() {
        periodSelection = .previous
        applyPeriodButtonStyles()
        collapseOnNextRefresh = true
        refresh()
    }

    @objc private func changeBillingDayByStepper() {
        let day = Int(billingDayStepper.doubleValue.rounded())
        billingDayField.stringValue = "\(day)"
    }

    @objc private func saveBillingDay() {
        let parsed = Int(billingDayField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines))
        let day = min(31, max(1, parsed ?? settings.billingDay))
        settings.billingDay = day
        settingsStore.save(settings)
        applySettingsToInputs()
        collapseOnNextRefresh = true
        refresh()
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
        return "1% ~= -- conversations / -- tok"
    }
    return "1% ~= \(formatTurns(turns)) conversations / \(formatTokenCount(tokens)) tok"
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

private func parseSQLiteTimestamp(_ text: String) -> Date? {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    if let date = formatter.date(from: text) {
        return date
    }
    return formatter.date(from: text.replacingOccurrences(of: "+00:00", with: "Z"))
}

private func calendarInCurrentTimeZone() -> Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone.current
    return calendar
}

private func billingPeriodStart(now: Date, billingDay: Int, calendar: Calendar) -> Date {
    let components = calendar.dateComponents([.year, .month], from: now)
    guard let year = components.year, let month = components.month else {
        return calendar.startOfDay(for: now)
    }
    let currentMonthStart = billingMonthStart(
        year: year,
        month: month,
        billingDay: billingDay,
        calendar: calendar
    )
    if now >= currentMonthStart {
        return currentMonthStart
    }
    return addBillingMonths(
        from: currentMonthStart,
        months: -1,
        billingDay: billingDay,
        calendar: calendar
    )
}

private func addBillingMonths(
    from date: Date,
    months: Int,
    billingDay: Int,
    calendar: Calendar
) -> Date {
    guard let shifted = calendar.date(byAdding: .month, value: months, to: date) else {
        return date
    }
    let components = calendar.dateComponents([.year, .month], from: shifted)
    guard let year = components.year, let month = components.month else {
        return shifted
    }
    return billingMonthStart(year: year, month: month, billingDay: billingDay, calendar: calendar)
}

private func billingMonthStart(
    year: Int,
    month: Int,
    billingDay: Int,
    calendar: Calendar
) -> Date {
    let day = min(billingDay, daysInMonth(year: year, month: month, calendar: calendar))
    let components = DateComponents(
        timeZone: calendar.timeZone,
        year: year,
        month: month,
        day: day
    )
    return calendar.date(from: components) ?? Date()
}

private func daysInMonth(year: Int, month: Int, calendar: Calendar) -> Int {
    var components = DateComponents()
    components.year = year
    components.month = month
    components.day = 1
    let first = calendar.date(from: components) ?? Date()
    let range = calendar.range(of: .day, in: .month, for: first)
    return range?.count ?? 31
}

private func billingWindowBounds(
    periodStart: Date,
    periodEnd: Date,
    calendar: Calendar
) -> [(start: Date, end: Date)] {
    var cursor = periodStart
    var windows: [(start: Date, end: Date)] = []
    while cursor < periodEnd {
        let candidate = calendar.date(byAdding: .day, value: 7, to: cursor) ?? periodEnd
        let end = min(candidate, periodEnd)
        windows.append((start: cursor, end: end))
        cursor = end
    }
    return windows
}

private func dayBounds(
    windowStart: Date,
    windowEnd: Date,
    calendar: Calendar
) -> [(start: Date, end: Date)] {
    var cursor = windowStart
    var days: [(start: Date, end: Date)] = []
    while cursor < windowEnd {
        let candidate = calendar.date(byAdding: .day, value: 1, to: cursor) ?? windowEnd
        let end = min(candidate, windowEnd)
        days.append((start: cursor, end: end))
        cursor = end
    }
    return days
}

private func formatMonthDay(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone.current
    formatter.dateFormat = "MMM d"
    return formatter.string(from: value)
}

private func formatHourMinute(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone.current
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: value)
}

private func formatPercentCompact(_ value: Double) -> String {
    if value.rounded() == value {
        return "\(Int(value))"
    }
    return String(format: "%.1f", value)
}

let app = NSApplication.shared
private let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
