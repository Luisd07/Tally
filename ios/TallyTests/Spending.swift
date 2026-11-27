import Foundation
@testable import Tally

/// The spending rule the Dashboard and Home totals use on the server, applied to a list of transactions.
/// Test support: it must track `api/app/services/dashboard.py::spending`, and the paired-fixture test in SpendingTests
/// fails when the two drift apart.
@MainActor
enum Spending {
    /// What `transactions` cost: personal and business, posted (not pending), split parents left out,
    /// summed as `-amount` so a refund nets against spend.
    static func total(of transactions: [Transaction]) -> Money {
        transactions
            .filter { counts($0) }
            .reduce(Money(0)) { $0 - $1.amount }
    }

    private static func counts(_ transaction: Transaction) -> Bool {
        switch transaction.bucket {
        case .personal, .business: !transaction.pending && !transaction.isSplit
        default: false
        }
    }
}
