import Foundation
import Testing
@testable import Tally

@MainActor
struct SpendingTests {
    private final class BundleToken {}

    private func transaction(
        _ amount: String,
        bucket: Bucket = .personal,
        pending: Bool = false,
        isSplit: Bool = false
    ) -> Transaction {
        Transaction(
            id: UUID(),
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            transactedAt: nil,
            amount: Money(Decimal(string: amount)!),
            descriptionRaw: "Test",
            merchant: nil,
            categoryId: nil,
            bucket: bucket,
            source: .manual,
            externalId: nil,
            pending: pending,
            reviewed: true,
            notes: nil,
            eventId: nil,
            parentId: nil,
            reimbursementId: nil,
            isSplit: isSplit,
            createdAt: .now,
            updatedAt: .now
        )
    }

    private func fixtureData(_ name: String) throws -> Data {
        let url = try #require(Bundle(for: BundleToken.self).url(forResource: name, withExtension: "json"))
        return try Data(contentsOf: url)
    }

    private func total(_ transactions: [Transaction]) -> Money {
        Spending.total(of: transactions)
    }

    @Test func totalCountsOnlyPersonalAndBusinessPostedNonSplitRows() {
        let list = [
            transaction("-10.00"),
            transaction("-5.50", bucket: .business),
            transaction("-40.00", bucket: .reimbursable),
            transaction("-100.00", bucket: .transfer),
            transaction("500.00", bucket: .income),
            transaction("-7.00", pending: true),
            transaction("-60.00", isSplit: true),
        ]
        #expect(total(list) == Money(Decimal(string: "15.50")!))
    }

    @Test func aRefundNetsAgainstSpend() {
        let list = [transaction("-30.00"), transaction("12.00")]
        #expect(total(list) == Money(Decimal(string: "18.00")!))
    }

    @Test func totalMatchesTheServersDashboardForTheSameMonth() throws {
        let dashboard = try JSONDecoder.api.decode(Dashboard.self, from: fixtureData("paired-dashboard-2026-10"))
        let page = try JSONDecoder.api.decode(TransactionPage.self, from: fixtureData("paired-transactions-2026-10"))
        #expect(page.nextCursor == nil)
        #expect(total(page.items) == dashboard.totalSpent)
        #expect(total(page.items) == dashboard.byCategory.reduce(Money(0)) { $0 + $1.spent })
    }

    @Test func noTransactionsTotalZero() {
        #expect(total([]) == Money(0))
    }
}
