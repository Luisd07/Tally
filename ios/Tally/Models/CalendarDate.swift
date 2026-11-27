import Foundation

/// A date-only value (`"2026-10-08"`) with no time zone, so it never shifts a day when displayed.
struct CalendarDate: Codable, Hashable, Comparable {
    let year: Int
    let month: Int
    let day: Int

    init(year: Int, month: Int, day: Int) {
        self.year = year
        self.month = month
        self.day = day
    }

    init(from decoder: Decoder) throws {
        let string = try decoder.singleValueContainer().decode(String.self)
        let parts = string.split(separator: "-", omittingEmptySubsequences: false).map { Int($0) }
        guard parts.count == 3, let year = parts[0], let month = parts[1], let day = parts[2] else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Invalid date \(string)")
            )
        }
        self.init(year: year, month: month, day: day)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(apiString)
    }

    /// `YYYY-MM-DD`, as the API sends and takes it.
    var apiString: String { String(format: "%04d-%02d-%02d", year, month, day) }

    static func < (lhs: CalendarDate, rhs: CalendarDate) -> Bool {
        (lhs.year, lhs.month, lhs.day) < (rhs.year, rhs.month, rhs.day)
    }

    /// The day `date` falls on in `calendar`.
    init(date: Date, calendar: Calendar = .current) {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        self.init(year: parts.year ?? 1970, month: parts.month ?? 1, day: parts.day ?? 1)
    }

    /// Local midnight of this day in `calendar`.
    func date(in calendar: Calendar = .current) -> Date? {
        calendar.date(from: DateComponents(year: year, month: month, day: day))
    }

    func formatted(_ style: Date.FormatStyle = .dateTime.month(.abbreviated).day(), calendar: Calendar = .current) -> String {
        guard let date = date(in: calendar) else { return "\(year)-\(month)-\(day)" }
        var style = style
        style.calendar = calendar
        style.timeZone = calendar.timeZone
        return date.formatted(style)
    }
}

extension CalendarDate {
    /// Days in this date's month, by the ISO 8601 (Gregorian) calendar the API's dates are in.
    var daysInMonth: Int {
        switch month {
        case 2: year.isMultiple(of: 4) && (!year.isMultiple(of: 100) || year.isMultiple(of: 400)) ? 29 : 28
        case 4, 6, 9, 11: 30
        default: 31
        }
    }

    /// The next day on or after `today` that falls on `day` of a month. A day past the month's end (31 in
    /// a 30-day month) lands on the month's last day.
    static func nextOccurrence(ofDay day: Int, onOrAfter today: CalendarDate) -> CalendarDate {
        let thisMonth = clamped(day: day, year: today.year, month: today.month)
        if thisMonth >= today { return thisMonth }
        let wraps = today.month == 12
        return clamped(day: day, year: wraps ? today.year + 1 : today.year, month: wraps ? 1 : today.month + 1)
    }

    private static func clamped(day: Int, year: Int, month: Int) -> CalendarDate {
        let first = CalendarDate(year: year, month: month, day: 1)
        return CalendarDate(year: year, month: month, day: min(day, first.daysInMonth))
    }

    /// Whole days from this day to `other`; negative when `other` is earlier.
    func days(until other: CalendarDate, calendar: Calendar = .current) -> Int? {
        guard let start = date(in: calendar), let end = other.date(in: calendar) else { return nil }
        return calendar.dateComponents([.day], from: start, to: end).day
    }
}
