import Foundation
import Testing
@testable import Tally

@MainActor
struct MonthRefTests {
    @Test func stepsBackAcrossYearBoundary() {
        var month = MonthRef(year: 2026, month: 10)
        month = month.previous
        #expect(month == MonthRef(year: 2026, month: 9))
        for _ in 0..<8 { month = month.previous }
        #expect(month == MonthRef(year: 2026, month: 1))
        #expect(month.previous == MonthRef(year: 2025, month: 12))
    }

    @Test func stepsForwardAcrossYearBoundary() {
        #expect(MonthRef(year: 2026, month: 12).next == MonthRef(year: 2027, month: 1))
        #expect(MonthRef(year: 2027, month: 1).previous == MonthRef(year: 2026, month: 12))
    }

    @Test func formatsAPIString() {
        #expect(MonthRef(year: 2026, month: 10).apiString == "2026-10")
        #expect(MonthRef(year: 2027, month: 1).apiString == "2027-01")
    }

    @Test func usesDeviceTimeZoneForCurrentMonth() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Pacific/Auckland")!
        let instant = Date(timeIntervalSince1970: 1_793_448_000) // 2026-10-31T12:00Z, already Nov 1 in Auckland
        #expect(MonthRef(containing: instant, calendar: calendar) == MonthRef(year: 2026, month: 11))
    }

    @Test func decodesFromAPIString() throws {
        let month = try JSONDecoder().decode(MonthRef.self, from: Data(#""2026-10""#.utf8))
        #expect(month == MonthRef(year: 2026, month: 10))
        #expect(MonthRef(apiString: month.apiString) == month)
    }

    @Test func rejectsBadMonthStrings() {
        for text in ["2026-13", "2026-00", "2026", "26-10", "2026-1", "abcd-ef", "2026-10-01"] {
            #expect(MonthRef(apiString: text) == nil)
            #expect(throws: DecodingError.self) {
                try JSONDecoder().decode(MonthRef.self, from: Data("\"\(text)\"".utf8))
            }
        }
    }

    @Test func firstDayIsTheFirstOfTheMonth() throws {
        let calendar = Calendar(identifier: .gregorian)
        let day = try #require(MonthRef(year: 2026, month: 10).firstDay(calendar: calendar))
        #expect(calendar.dateComponents([.year, .month, .day], from: day) == DateComponents(year: 2026, month: 10, day: 1))
    }
}
