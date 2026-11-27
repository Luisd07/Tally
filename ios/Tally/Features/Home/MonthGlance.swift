import Foundation

/// How a month is going, composed on the phone from Home, the dashboard, the Inbox and missing-receipt counts and
/// the subscriptions list. Nothing here is stored.
struct MonthGlance {
    let home: Home
    let dashboard: Dashboard
    /// Unreviewed Inbox items.
    let toReview: Int
    /// Business and reimbursable expenses this year without a receipt.
    let missingReceipts: Int
    /// Bills still to come before the month ends, by date.
    let bills: [UpcomingBill]
    /// How much of the month has passed, from 0 to 1.
    let elapsed: Double

    var spent: Money { home.spendingByBucket.total }
    var received: Money { home.income.received }
    var expected: Money { home.income.expected }

    var hasBudget: Bool { dashboard.hasBudget }

    /// What spending is measured against: the total budget, or the income expected when no budget is set.
    var limit: Money { hasBudget ? dashboard.totalBudget : expected }

    /// What is left of `limit`; negative when over it.
    var left: Money { limit - spent }

    /// What spending would be by now at an even pace through the month.
    var paceTarget: Money { Money(limit.amount * Decimal(elapsed)).roundedToCents }

    /// How far spending is under the even pace; negative when over it.
    var underPace: Money { paceTarget - spent }

    var isOverPace: Bool { spent > paceTarget }

    /// Spent as a share of `limit`; 0 without a limit.
    var spentFraction: Double { Self.fraction(spent, of: limit) }

    /// Bills still due this month.
    var billsTotal: Money { bills.reduce(Money(0)) { $0 + ($1.amount ?? Money(0)) } }

    static func fraction(_ part: Money, of whole: Money) -> Double {
        guard whole > Money(0) else { return 0 }
        return part.chartValue / whole.chartValue
    }

    /// How much of `month` has passed on `today`: 1 for a past month, 0 for a future one.
    static func elapsed(in month: MonthRef, today: CalendarDate) -> Double {
        let current = MonthRef(containing: today)
        if month < current { return 1 }
        if current < month { return 0 }
        return Double(today.day) / Double(today.daysInMonth)
    }

    /// Active subscriptions and bills due from `today` to the end of `month`, plus card payments Home lists in
    /// that span, by date. Empty for any month but the current one.
    static func upcomingBills(
        subscriptions: [Subscription], homeBills: [BillDue], month: MonthRef, today: CalendarDate
    ) -> [UpcomingBill] {
        guard MonthRef(containing: today) == month else { return [] }
        let monthEnd = CalendarDate(year: today.year, month: today.month, day: today.daysInMonth)
        let span = today...monthEnd
        let recurring = subscriptions.compactMap { row -> UpcomingBill? in
            guard row.active, let due = row.nextDue, span.contains(due) else { return nil }
            return UpcomingBill(id: row.id, name: row.name, date: due, amount: row.cost, isCard: false)
        }
        let cards = homeBills.compactMap { bill -> UpcomingBill? in
            guard bill.kind == .card, span.contains(bill.dueDate) else { return nil }
            return UpcomingBill(id: bill.id, name: bill.name, date: bill.dueDate, amount: bill.amount, isCard: true)
        }
        return (recurring + cards).sorted { ($0.date, $0.name) < ($1.date, $1.name) }
    }
}

/// A bill due later this month; `amount` is nil for a card with no planned payment.
struct UpcomingBill: Identifiable, Hashable {
    let id: UUID
    let name: String
    let date: CalendarDate
    let amount: Money?
    let isCard: Bool
}
