import Foundation

/// Mirrors `DashboardOut` from `GET /api/dashboard`.
struct Dashboard: Decodable {
    let month: MonthRef
    let byCategory: [CategorySpend]
    let totalSpent: Money
    let totalBudget: Money
    let trend: [TrendMonth]

    /// How many trend months the server sends when `months` is omitted.
    static let defaultTrendMonths = 6

    /// Whether any category has a budget, so the header shows Budget and Remaining.
    var hasBudget: Bool { totalBudget > Money(0) }

    /// What is left of the total budget; negative when over it.
    var remaining: Money { totalBudget - totalSpent }
}

/// One category's spending for the month; the row with no category rolls up uncategorized spending.
struct CategorySpend: Decodable, Identifiable, Hashable {
    let categoryId: UUID?
    let name: String?
    let budget: Money?
    let spent: Money
    let remaining: Money?

    var id: String { categoryId?.uuidString ?? "uncategorized" }
    var displayName: String { name ?? "Uncategorized" }

    /// Whether spending went past a set budget.
    var isOverBudget: Bool { remaining.map { $0 < Money(0) } ?? false }

    /// "of $X" under the spent amount; nil without a budget.
    var budgetText: String? { budget.map { "of \($0.formatted())" } }

    /// "$X left" or "over by $X"; nil without a budget.
    var remainingText: String? {
        remaining.map { $0 < Money(0) ? "over by \($0.negated.formatted())" : "\($0.formatted()) left" }
    }

    /// The row as VoiceOver reads it, with the budget and whether it is exceeded.
    var accessibilitySummary: String {
        [displayName + " " + spent.formatted(), budgetText, remainingText].compactMap { $0 }.joined(separator: ", ")
    }
}

/// One month of the spending trend.
struct TrendMonth: Decodable, Identifiable, Hashable {
    let month: MonthRef
    let spent: Money

    var id: MonthRef { month }
}
