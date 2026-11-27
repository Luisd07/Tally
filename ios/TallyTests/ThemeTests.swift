import Foundation
import Testing
@testable import Tally

@MainActor
struct ThemeTests {
    private func money(_ text: String) -> Money {
        Money(Decimal(string: text)!)
    }

    private func transaction(
        bucket: Bucket = .personal,
        pending: Bool = false,
        reviewed: Bool = false,
        isSplit: Bool = false
    ) -> Transaction {
        Transaction(
            id: UUID(),
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            transactedAt: nil,
            amount: money("-85.00"),
            descriptionRaw: "HERTZ RENTAL CAR",
            merchant: nil,
            categoryId: nil,
            bucket: bucket,
            source: .manual,
            externalId: nil,
            pending: pending,
            reviewed: reviewed,
            notes: nil,
            eventId: nil,
            parentId: nil,
            reimbursementId: nil,
            isSplit: isSplit,
            createdAt: .now,
            updatedAt: .now
        )
    }

    // MARK: AmountText

    @Test func outflowIsUnsignedAndPrimary() {
        let shown = AmountText.Presentation(money("-85.00"), role: .flow)
        #expect(shown.text == "$85.00")
        #expect(shown.tone == .primary)
    }

    @Test func inflowGetsPlusAndMoneyIn() {
        let shown = AmountText.Presentation(money("1500.00"), role: .flow)
        #expect(shown.text == "+$1,500.00")
        #expect(shown.tone == .moneyIn)
    }

    @Test func zeroFlowIsUnsignedAndPrimary() {
        let shown = AmountText.Presentation(money("0"), role: .flow)
        #expect(shown.text == "$0.00")
        #expect(shown.tone == .primary)
    }

    @Test func amountRoleDropsTheSignEitherWay() {
        #expect(AmountText.Presentation(money("391.10"), role: .amount) == .init(money("-391.10"), role: .amount))
        #expect(AmountText.Presentation(money("391.10"), role: .amount).text == "$391.10")
        #expect(AmountText.Presentation(money("391.10"), role: .amount).tone == .primary)
    }

    @Test func neutralRoleIsUnsignedAndSecondary() {
        for text in ["-250.00", "250.00"] {
            let shown = AmountText.Presentation(money(text), role: .neutral)
            #expect(shown.text == "$250.00")
            #expect(shown.tone == .secondary)
        }
    }

    @Test func transfersAreNeutralAndEverythingElseFlows() {
        #expect(transaction(bucket: .transfer).amountRole == .neutral)
        for bucket in [Bucket.personal, .business, .reimbursable, .income, .other("gifts")] {
            #expect(transaction(bucket: bucket).amountRole == .flow)
        }
    }

    // MARK: TransactionStatus

    @Test func transactionsListShowsReviewPendingAndSplitInOrder() {
        let row = transaction(pending: true, reviewed: false, isSplit: true)
        #expect(TransactionStatus.of(row, in: .transactions) == [.unreviewed, .pending, .split])
    }

    @Test func reviewedSettledRowHasNoStatus() {
        #expect(TransactionStatus.of(transaction(reviewed: true), in: .transactions).isEmpty)
    }

    @Test func inboxLeavesOutUnreviewedAndLeadsWithActionableStatuses() {
        let row = transaction(pending: true, reviewed: false)
        let statuses = TransactionStatus.of(row, needsResplit: true, awaitingBankMatch: true, in: .inbox)
        #expect(statuses == [.needsResplit, .awaitingBankMatch, .pending])
    }

    @Test func sameTransactionGetsTheSameLabelsInBothLists() {
        let row = transaction(pending: true, reviewed: true, isSplit: true)
        #expect(TransactionStatus.of(row, in: .inbox) == TransactionStatus.of(row, in: .transactions))
    }

    @Test func onlyNeedsResplitDrawsAttention() {
        for status in TransactionStatus.allCases {
            #expect(status.tone == (status == .needsResplit ? .attention : .neutral))
        }
    }

    @Test func everyStatusHasALabelAndSymbol() {
        #expect(Set(TransactionStatus.allCases.map(\.label)).count == TransactionStatus.allCases.count)
        #expect(TransactionStatus.allCases.allSatisfy { !$0.symbol.isEmpty })
        #expect(TransactionStatus.awaitingBankMatch.label == "Waiting for bank match")
    }

    // MARK: Theme and glyphs

    @Test func launchEnvironmentPicksDirectionAndAccent() {
        #expect(Theme(launchEnvironment: [:]) == .standard)
        #expect(Theme(launchEnvironment: ["TALLY_THEME": "B", "TALLY_ACCENT": "indigo"]) == Theme(direction: .b, accent: .indigo))
        #expect(Theme(launchEnvironment: ["TALLY_THEME": "b"]).direction == .b)
        #expect(Theme(launchEnvironment: ["TALLY_THEME": "C", "TALLY_ACCENT": "red"]) == .standard)
        #expect(Theme.standard == Theme(direction: .mix, accent: .blue))
        #expect(Theme.standard.usesCards && Theme.standard.isDense && !Theme.standard.showsGlyphs)
    }

    @Test func categoryGlyphMatchesWholeWords() {
        #expect(CategoryGlyph.symbol(for: "Groceries") == "cart")
        #expect(CategoryGlyph.symbol(for: "Car payment") == "car")
        #expect(CategoryGlyph.symbol(for: "Credit card fees") == "tag")
        #expect(CategoryGlyph.symbol(for: nil) == "questionmark.circle")
        #expect(CategoryGlyph.symbol(for: "Uncategorized") == "questionmark.circle")
    }

    // MARK: MonthGlance

    private func glance(
        spent: String = "391.10",
        received: String = "1500.00",
        expected: String = "1750.00",
        budget: String = "0.00",
        categories: [CategorySpend] = [],
        bills: [UpcomingBill] = [],
        elapsed: Double = 0.5
    ) -> MonthGlance {
        let home = Home(
            month: "2026-10",
            spendingByBucket: SpendingByBucket(buckets: [], total: money(spent)),
            owedToYou: money("120.00"),
            income: IncomeSummary(expected: money(expected), received: money(received)),
            billsDue: [],
            lastUpdated: nil
        )
        let dashboard = Dashboard(
            month: MonthRef(year: 2026, month: 10),
            byCategory: categories,
            totalSpent: money(spent),
            totalBudget: money(budget),
            trend: []
        )
        return MonthGlance(
            home: home, dashboard: dashboard, toReview: 13, missingReceipts: 2, bills: bills, elapsed: elapsed
        )
    }

    private func category(_ name: String, spent: String, budget: String?) -> CategorySpend {
        CategorySpend(
            categoryId: UUID(), name: name, budget: budget.map(money), spent: money(spent), remaining: nil
        )
    }

    @Test func withoutBudgetsLeftIsExpectedIncomeLessSpending() {
        let month = glance()
        #expect(!month.hasBudget)
        #expect(month.limit == money("1750.00"))
        #expect(month.left == money("1358.90"))
    }

    @Test func budgetSetsTheLimitAndPace() {
        let month = glance(spent: "900.00", budget: "1500.00", elapsed: 0.5)
        #expect(month.limit == money("1500.00"))
        #expect(month.left == money("600.00"))
        #expect(month.paceTarget == money("750.00"))
        #expect(month.isOverPace)
        #expect(month.underPace == money("-150.00"))
    }

    @Test func billsTotalCountsBillsWithAnAmount() {
        let bill = UpcomingBill(
            id: UUID(), name: "Car Payment", date: CalendarDate(year: 2026, month: 10, day: 25),
            amount: money("450.00"), isCard: false
        )
        let card = UpcomingBill(
            id: UUID(), name: "Visa", date: CalendarDate(year: 2026, month: 10, day: 22), amount: nil, isCard: true
        )
        #expect(glance(bills: [bill, card]).billsTotal == money("450.00"))
    }

    @Test func elapsedIsFullForPastMonthsAndPartialForThisOne() {
        let today = CalendarDate(year: 2026, month: 10, day: 19)
        #expect(MonthGlance.elapsed(in: MonthRef(year: 2026, month: 9), today: today) == 1)
        #expect(MonthGlance.elapsed(in: MonthRef(year: 2026, month: 11), today: today) == 0)
        #expect(MonthGlance.elapsed(in: MonthRef(year: 2026, month: 10), today: today) == 19.0 / 31.0)
    }

    @Test func upcomingBillsAreThisMonthsCardsAndActiveRecurringFromToday() {
        let today = CalendarDate(year: 2026, month: 10, day: 19)
        let card = BillDue(
            kind: .card, id: UUID(), name: "Visa", dueDate: CalendarDate(year: 2026, month: 10, day: 22), amount: nil
        )
        let nextMonthCard = BillDue(
            kind: .card, id: UUID(), name: "Amex", dueDate: CalendarDate(year: 2026, month: 11, day: 1), amount: nil
        )
        let bills = MonthGlance.upcomingBills(
            subscriptions: [], homeBills: [nextMonthCard, card], month: MonthRef(year: 2026, month: 10), today: today
        )
        #expect(bills.map(\.name) == ["Visa"])
        #expect(MonthGlance.upcomingBills(
            subscriptions: [], homeBills: [card], month: MonthRef(year: 2026, month: 9), today: today
        ).isEmpty)
    }
}
