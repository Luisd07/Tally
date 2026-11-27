import Foundation

/// Mirrors `HomeOut` from `GET /api/home`.
struct Home: Decodable {
    let month: String
    let spendingByBucket: SpendingByBucket
    let owedToYou: Money
    let income: IncomeSummary
    let billsDue: [BillDue]
    let lastUpdated: Date?
    /// Banks SimpleFIN hasn't refreshed in over two days. Optional so a server from before it still decodes.
    var staleBanks: [StaleBank]? = nil
    /// Which getting-started steps are done. Optional so a server from before it still decodes.
    var setup: SetupProgress? = nil
}

/// Which getting-started steps are done, from real data on the server.
struct SetupProgress: Decodable, Equatable {
    let bankConnected: Bool
    let inboxReviewed: Bool
    let categoriesReady: Bool
    let budgetsSet: Bool
    let notificationsOn: Bool
}

/// A bank whose data stopped refreshing: a sync still succeeds, but brings nothing new.
struct StaleBank: Decodable, Hashable {
    let institution: String
    let lastRefreshed: CalendarDate
}

struct SpendingByBucket: Decodable {
    let buckets: [BucketSpend]
    let total: Money
}

struct BucketSpend: Decodable {
    let bucket: Bucket
    let spent: Money
}

struct IncomeSummary: Decodable {
    let expected: Money
    let received: Money
}

/// A bill due within the next 7 days; `amount` is nil for a card with no planned payment.
struct BillDue: Decodable, Identifiable {
    let kind: BillKind
    let id: UUID
    let name: String
    let dueDate: CalendarDate
    let amount: Money?
}

/// Kind of bill; unknown server values decode to `.other`.
enum BillKind: OpenEnum, Hashable {
    case subscription, card, other(String)

    nonisolated static var known: [String: BillKind] { ["subscription": .subscription, "card": .card] }
    nonisolated static func unknown(_ raw: String) -> BillKind { .other(raw) }
}
