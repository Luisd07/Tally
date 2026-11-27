import Foundation

/// Mirrors `CardLogOut` from `GET /api/accounts/{id}/card-log`. Every raw amount keeps SimpleFIN's sign:
/// charges and fees are negative, payments and refunds positive, a balance negative while the card is in debt.
/// The balances, `computedEnding` and `difference` are nil when no snapshot qualifies for the month.
struct CardLog: Decodable, Hashable {
    let month: String
    let startingBalance: Money?
    let startingBalanceDate: CalendarDate?
    let newCharges: Money
    let payments: Money
    let interestAndFees: Money
    let other: Money
    let endingBalance: Money?
    let endingBalanceDate: CalendarDate?
    let computedEnding: Money?
    let difference: Money?

    /// Whether the month's activity accounts for the ending balance exactly.
    var isReconciled: Bool { difference?.amount == 0 }

    /// What was charged, as a positive outflow.
    var charged: Money { newCharges.negated }
    // Payments and other are credits, already positive, so they keep the server sign.
    /// Interest and fees, as a positive outflow.
    var feesCharged: Money { interestAndFees.negated }
    var startingOwed: Money? { startingBalance?.negated }
    var endingOwed: Money? { endingBalance?.negated }
    var computedEndingOwed: Money? { computedEnding?.negated }
    /// How much more the card owes than the activity explains: the ending owed minus the computed ending owed.
    var differenceOwed: Money? { difference?.negated }

    /// The ending balance's date when it falls before `month`: the snapshot is reused, so there is no difference to show.
    func staleEndingDate(in month: MonthRef) -> CalendarDate? {
        guard let date = endingBalanceDate, MonthRef(containing: date) < month else { return nil }
        return date
    }

    /// Where the month's reconciliation stands.
    enum Reconciliation: Hashable {
        case reconciled
        /// The card owes this much more (negative: less) than the activity explains.
        case off(by: Money)
        /// The ending balance is from before the month, so there is no difference to show.
        case staleEnding(CalendarDate)
        /// A balance is absent. A stale ending counts as a missing end once the start is missing too.
        case missingBalances(missingStart: Bool, missingEnd: Bool)
    }

    /// Why the month does or doesn't reconcile. A missing starting balance is reported before a stale ending.
    func reconciliation(in month: MonthRef) -> Reconciliation {
        if let differenceOwed {
            return differenceOwed.amount == 0 ? .reconciled : .off(by: differenceOwed)
        }
        let stale = staleEndingDate(in: month)
        if startingBalance == nil || endingBalance == nil {
            return .missingBalances(missingStart: startingBalance == nil, missingEnd: endingBalance == nil || stale != nil)
        }
        if let stale { return .staleEnding(stale) }
        return .missingBalances(missingStart: false, missingEnd: false)
    }
}
