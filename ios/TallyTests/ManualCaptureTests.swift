import Foundation
import Testing
@testable import Tally

@MainActor
struct ManualCaptureTests {
    func entry(
        direction: ManualCapture.Direction = .expense,
        categoryName: String? = nil,
        description: String = "",
        merchant: String = "",
        notes: String = ""
    ) -> ManualCapture {
        ManualCapture(
            amount: Money(Decimal(string: "12.5")!),
            direction: direction,
            accountId: UUID(),
            bucket: .business,
            categoryId: UUID(),
            categoryName: categoryName,
            eventId: UUID(),
            date: CalendarDate(year: 2026, month: 3, day: 4),
            description: description,
            merchant: merchant,
            notes: notes
        )
    }

    @Test func anExpenseIsNegativeAndMoneyInIsPositive() {
        #expect(entry(direction: .expense).capture().amount == Money(Decimal(string: "-12.5")!))
        #expect(entry(direction: .moneyIn).capture().amount == Money(Decimal(string: "12.5")!))
    }

    @Test func theCaptureCarriesTheEntryFields() {
        let source = entry(description: "Lunch")
        let capture = source.capture()
        #expect(capture.accountId == source.accountId)
        #expect(capture.postedDate == source.date)
        #expect(capture.categoryId == source.categoryId)
        #expect(capture.bucket == .business)
        #expect(capture.eventId == source.eventId)
    }

    @Test func theDescriptionFallsBackToTheCategoryNameThenByDirection() {
        #expect(entry(categoryName: "Groceries", description: "  ").capture().descriptionRaw == "Groceries")
        #expect(entry(direction: .expense).capture().descriptionRaw == "Manual expense")
        #expect(entry(direction: .moneyIn).capture().descriptionRaw == "Manual income")
    }

    @Test func aTypedDescriptionWinsOverTheCategoryName() {
        #expect(entry(categoryName: "Groceries", description: "  Lunch\n").capture().descriptionRaw == "Lunch")
    }

    @Test func merchantAndNotesAreTrimmedOrNil() {
        let trimmed = entry(merchant: " Cafe ", notes: "\tfor Sam \n").capture()
        #expect(trimmed.merchant == "Cafe")
        #expect(trimmed.notes == "for Sam")
        let blank = entry(merchant: "  ", notes: "").capture()
        #expect(blank.merchant == nil)
        #expect(blank.notes == nil)
    }
}
