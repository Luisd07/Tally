import Foundation

/// A CSV's first data row read with a draft mapping, as the server would read it (`_parse_record` in
/// api/app/services/import_parsers.py), so a wrong sign or day/month order shows before any row is imported.
struct ImportPreview: Equatable {
    /// Why a field can't be read from the row.
    struct Problem: Error, Equatable {
        let message: String

        init(_ message: String) {
            self.message = message
        }
    }

    let date: Result<CalendarDate, Problem>
    /// Signed as the server stores it: spending is negative.
    let amount: Result<Money, Problem>
    let description: Result<String, Problem>

    /// Nil when the file has no data row.
    init?(sample: CSVSample, draft: ImportMappingDraft) {
        guard let row = sample.firstDataRow(hasHeader: draft.hasHeader) else { return nil }
        let cell = { (column: String) in Self.cell(column, of: row, in: sample, hasHeader: draft.hasHeader) }
        date = cell(draft.dateColumn).flatMap { Self.date($0, format: draft.dateFormat) }
        amount = Self.amount(draft, cell: cell).map(Money.init)
        description = cell(draft.descriptionColumn).flatMap {
            $0.isEmpty ? .failure(Problem("No description.")) : .success($0)
        }
    }

    /// "−$4.75 (money out)", "$6.00 (money in)", or "$0.00".
    static func amountText(_ money: Money) -> String {
        let size = Money(abs(money.amount)).formatted()
        if money.amount < 0 { return "−\(size) (money out)" }
        return money.amount > 0 ? "\(size) (money in)" : size
    }

    private static func cell(
        _ column: String, of row: [String], in sample: CSVSample, hasHeader: Bool
    ) -> Result<String, Problem> {
        guard !column.isEmpty else { return .failure(Problem("Choose a column.")) }
        guard let position = sample.position(of: column, hasHeader: hasHeader) else {
            return .failure(Problem("This file has no column “\(column)”."))
        }
        return .success(CSVSample.cell(row, at: position))
    }

    private static func date(_ text: String, format: String) -> Result<CalendarDate, Problem> {
        guard let strptime = StrptimeFormat(format) else {
            return .failure(Problem("Preview unavailable for this format."))
        }
        guard let date = strptime.date(from: text) else {
            return .failure(Problem("Can't read “\(text)” as \(DateFormatPreset.label(for: format))."))
        }
        return .success(date)
    }

    /// The server's `_row_amount`: one column, flipped when spending is positive, or credit minus debit.
    private static func amount(
        _ draft: ImportMappingDraft, cell: (String) -> Result<String, Problem>
    ) -> Result<Decimal, Problem> {
        switch draft.amountMode {
        case .single:
            return cell(draft.amountColumn).flatMap(parseAmount).map {
                draft.signConvention == .positiveIsOut ? -$0 : $0
            }
        case .debitCredit:
            return cell(draft.debitColumn).flatMap { debit in
                cell(draft.creditColumn).flatMap { credit in debitCredit(debit: debit, credit: credit) }
            }
        }
    }

    private static func debitCredit(debit: String, credit: String) -> Result<Decimal, Problem> {
        guard !debit.isEmpty || !credit.isEmpty else { return .failure(Problem("No debit or credit amount.")) }
        let out = debit.isEmpty ? .success(0) : parseAmount(debit).map { abs($0) }
        let into = credit.isEmpty ? .success(0) : parseAmount(credit).map { abs($0) }
        return out.flatMap { out in into.map { $0 - out } }
    }

    /// The server's `_parse_amount`: `$1,234.50`, `(12.00)` and `12.00-` all read; at most two decimals.
    private static func parseAmount(_ text: String) -> Result<Decimal, Problem> {
        var value = text.filter { !"$, ".contains($0) }
        guard !value.isEmpty else { return .failure(Problem("No amount.")) }
        var negative = false
        if value.hasPrefix("("), value.hasSuffix(")") {
            negative = true
            value = String(value.dropFirst().dropLast())
        }
        if value.hasSuffix("-") {
            negative = true
            value.removeLast()
        }
        guard value.wholeMatch(of: /[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?/) != nil,
              let amount = Decimal(string: value, locale: Locale(identifier: "en_US_POSIX"))
        else { return .failure(Problem("Can't read “\(text)” as an amount.")) }
        return checked(amount, text).map { negative ? -$0 : $0 }
    }

    /// `amount` within the server's NUMERIC(12,2) column, in whole cents.
    private static func checked(_ amount: Decimal, _ text: String) -> Result<Decimal, Problem> {
        guard abs(amount) <= maxAmount else { return .failure(Problem("“\(text)” is too large.")) }
        guard Money(amount).roundedToCents.amount == amount else {
            return .failure(Problem("“\(text)” has more than 2 decimal places."))
        }
        return .success(amount)
    }

    private static let maxAmount = Decimal(string: "9999999999.99")!
}

/// A `strptime` format as a POSIX `DateFormatter`, for the directives the presets and the server's checks use:
/// %Y %y %m %d %b %B and %%, with anything else literal. A pattern of Python's digit widths (four for %Y, two for
/// %y, one or two for %m and %d) and month names guards the formatter, which reads looser.
struct StrptimeFormat {
    private let formatter: DateFormatter
    private let shape: Regex<AnyRegexOutput>

    /// Nil when `format` has a directive outside that set.
    init?(_ format: String) {
        var pattern = ""
        var shape = ""
        var characters = format.makeIterator()
        while let character = characters.next() {
            guard character == "%" else {
                pattern += character == "'" ? "''" : "'\(character)'"
                shape += NSRegularExpression.escapedPattern(for: String(character))
                continue
            }
            guard let directive = characters.next().flatMap({ Self.directives[$0] }) else { return nil }
            pattern += directive.pattern
            shape += "(?:\(directive.shape))"
        }
        guard let regex = try? Regex(shape) else { return nil }
        self.shape = regex.ignoresCase()
        formatter = Self.formatter(pattern)
    }

    /// The day `text` names in this format, or nil when it doesn't match or isn't a real day.
    func date(from text: String) -> CalendarDate? {
        guard text.wholeMatch(of: shape) != nil, let date = formatter.date(from: text) else { return nil }
        return CalendarDate(date: date, calendar: formatter.calendar)
    }

    private static func formatter(_ pattern: String) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = posix
        formatter.calendar = utcCalendar
        formatter.timeZone = utcCalendar.timeZone
        formatter.isLenient = false
        formatter.dateFormat = pattern
        // Python's %y reads 69–99 as 19xx and 00–68 as 20xx.
        formatter.twoDigitStartDate = utcCalendar.date(from: DateComponents(year: 1969, month: 1, day: 1))
        return formatter
    }

    private static let posix = Locale(identifier: "en_US_POSIX")

    private static let utcCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }()

    private static let directives: [Character: (pattern: String, shape: String)] = {
        let names = DateFormatter()
        names.locale = posix
        return [
            "Y": ("yyyy", #"\d{4}"#),
            "y": ("yy", #"\d{2}"#),
            "m": ("M", "1[0-2]|0[1-9]|[1-9]"),
            "d": ("d", "3[01]|[12][0-9]|0[1-9]|[1-9]"),
            "b": ("MMM", names.shortMonthSymbols.joined(separator: "|")),
            "B": ("MMMM", names.monthSymbols.joined(separator: "|")),
            "%": ("'%'", "%"),
        ]
    }()
}
