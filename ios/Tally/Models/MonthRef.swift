import Foundation

/// A calendar month, as sent to the API in `?month=YYYY-MM`.
struct MonthRef: Hashable {
    let year: Int
    let month: Int

    init(year: Int, month: Int) {
        self.year = year
        self.month = month
    }

    /// The month containing now in the device time zone.
    init(containing date: Date = .now, calendar: Calendar = .current) {
        let parts = calendar.dateComponents([.year, .month], from: date)
        self.init(year: parts.year ?? 1970, month: parts.month ?? 1)
    }

    /// The month `day` falls in.
    init(containing day: CalendarDate) {
        self.init(year: day.year, month: day.month)
    }

    /// Reads `YYYY-MM`, the inverse of `apiString`; nil for anything else.
    init?(apiString: String) {
        let parts = apiString.split(separator: "-")
        guard parts.count == 2, parts[0].count == 4, parts[1].count == 2,
              let year = Int(parts[0]), let month = Int(parts[1]), (1...12).contains(month)
        else { return nil }
        self.init(year: year, month: month)
    }

    var apiString: String { String(format: "%04d-%02d", year, month) }

    var previous: MonthRef {
        month == 1 ? MonthRef(year: year - 1, month: 12) : MonthRef(year: year, month: month - 1)
    }

    var next: MonthRef {
        month == 12 ? MonthRef(year: year + 1, month: 1) : MonthRef(year: year, month: month + 1)
    }

    static func < (lhs: MonthRef, rhs: MonthRef) -> Bool {
        (lhs.year, lhs.month) < (rhs.year, rhs.month)
    }

    /// Midnight on the 1st of the month in `calendar`.
    func firstDay(calendar: Calendar = .current) -> Date? {
        calendar.date(from: DateComponents(year: year, month: month))
    }

    func title(calendar: Calendar = .current) -> String {
        guard let date = firstDay(calendar: calendar) else { return apiString }
        var style = Date.FormatStyle.dateTime.month(.wide).year()
        style.calendar = calendar
        style.timeZone = calendar.timeZone
        return date.formatted(style)
    }
}

extension MonthRef: Decodable {
    /// Decodes the API's `YYYY-MM` string; anything else throws.
    init(from decoder: Decoder) throws {
        let text = try decoder.singleValueContainer().decode(String.self)
        guard let month = MonthRef(apiString: text) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Invalid month \(text)"))
        }
        self = month
    }
}
