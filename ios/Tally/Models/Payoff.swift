import Foundation

/// Mirrors `PayoffPlanOut` from `GET /api/accounts/payoff`: each card owing a balance paid on its own at its
/// monthly payment, when that makes the user debt-free and what it costs, and the avalanche and snowball
/// strategies for `budget`. Nil months and interest mean it never pays off at those payments. `clamped` says the
/// `requestedBudget` was less than the monthly payments and `budget` was raised to cover them.
struct PayoffPlan: Decodable, Hashable {
    let budget: Money
    let requestedBudget: Money?
    let clamped: Bool
    let plannedBudget: Money
    let monthsToDebtFree: Int?
    let debtFreeMonth: MonthRef?
    let totalInterest: Money?
    let cards: [PayoffCard]
    let avalanche: PayoffStrategy
    let snowball: PayoffStrategy
}

/// Mirrors `PayoffCardOut`: one card paid on its own, with its 0% promo pacing when a promo is active.
struct PayoffCard: Decodable, Hashable, Identifiable {
    let accountId: UUID
    let name: String
    let owed: Money
    let apr: String?
    let promoAprEnd: CalendarDate?
    let plannedPayment: Money?
    let minimumPayment: Money
    let monthsToPayoff: Int?
    let payoffMonth: MonthRef?
    let totalInterest: Money?
    let promo: PromoPace?

    var id: UUID { accountId }
}

/// Mirrors `PromoPaceOut`: the monthly payment that clears the card before its promo ends, and whether its
/// monthly payment falls short of that.
struct PromoPace: Decodable, Hashable {
    let ends: CalendarDate
    let monthsLeft: Int
    let monthlyPayment: Money
    let short: Bool
}

/// Which strategy a `PayoffStrategy` is; unknown server values decode to `.other`.
enum PayoffStrategyKind: OpenEnum, Hashable {
    case avalanche, snowball, other(String)

    nonisolated static var known: [String: PayoffStrategyKind] { ["avalanche": .avalanche, "snowball": .snowball] }
    nonisolated static func unknown(_ raw: String) -> PayoffStrategyKind { .other(raw) }
}

/// Mirrors `PayoffStrategyOut`: the cards in the order to pay them, when that makes the user debt-free, and the
/// interest it costs and saves against paying only the monthly payments.
struct PayoffStrategy: Decodable, Hashable {
    struct Step: Decodable, Hashable, Identifiable {
        let accountId: UUID
        let name: String
        let payoffMonth: MonthRef?

        var id: UUID { accountId }
    }

    let strategy: PayoffStrategyKind
    let order: [Step]
    let monthsToDebtFree: Int?
    let debtFreeMonth: MonthRef?
    let totalInterest: Money?
    let interestSaved: Money?
}

extension PayoffPlan {
    /// The budget slider moves in steps of this much.
    static let budgetStep = Money(10)

    /// The most the slider offers: three times the planned payments.
    var maxBudget: Money { Money(plannedBudget.amount * 3) }

    /// How many steps the slider has above the planned payments; 0 when there's nothing planned to scale.
    var budgetSteps: Int {
        let span = (maxBudget - plannedBudget).amount / Self.budgetStep.amount
        return NSDecimalNumber(decimal: span).intValue
    }

    /// The budget `step` steps above the planned payments, never past `maxBudget`.
    func budget(atStep step: Int) -> Money {
        min(plannedBudget + Money(Decimal(step) * Self.budgetStep.amount), maxBudget)
    }

    /// The slider step closest to `budget`.
    func step(for budget: Money) -> Int {
        let steps = (budget - plannedBudget).amount / Self.budgetStep.amount
        return min(max(Int(NSDecimalNumber(decimal: steps).doubleValue.rounded()), 0), budgetSteps)
    }

    /// "Raised to cover minimums" when the budget asked for was less than the monthly payments.
    var clampedText: String? {
        clamped ? "Raised to cover minimums" : nil
    }
}

extension Optional where Wrapped == MonthRef {
    /// "Debt-free by October 2030", or "Never at these payments" when it doesn't pay off.
    var debtFreeText: String {
        map { "Debt-free by \($0.title())" } ?? "Never at these payments"
    }
}

extension PromoPace {
    /// "Pay $240.00/mo to clear by Apr 1, 2027".
    var payText: String {
        "Pay \(monthlyPayment.formatted())/mo to clear by \(ends.formatted(.dateTime.month(.abbreviated).day().year()))"
    }
}

extension PayoffStrategy {
    var title: String {
        switch strategy {
        case .avalanche: "Avalanche"
        case .snowball: "Snowball"
        case .other(let raw): raw.capitalized
        }
    }

    /// What it orders by.
    var subtitle: String {
        switch strategy {
        case .avalanche: "Highest APR first"
        case .snowball: "Smallest balance first"
        case .other: ""
        }
    }

    /// "Saves $1,303.02", or nil when it saves nothing or never pays off.
    var savedText: String? {
        guard let interestSaved, interestSaved.amount > 0 else { return nil }
        return "Saves \(interestSaved.formatted())"
    }
}

extension PayoffCard {
    /// "Paid off by November 2027", or "Never at its payment" when it doesn't pay off on its own.
    var payoffText: String {
        payoffMonth.map { "Paid off by \($0.title())" } ?? "Never at its payment"
    }
}
