import SwiftUI

/// A transaction's status, with the one label, symbol and tone every list shows for it.
enum TransactionStatus: CaseIterable, Hashable {
    case needsResplit, awaitingBankMatch, unreviewed, pending, split

    /// Where the statuses are shown. The Inbox lists only unreviewed items, so it doesn't repeat that one.
    enum Context {
        case inbox, transactions
    }

    /// `transaction`'s statuses in display order. `needsResplit` and `awaitingBankMatch` come from the Inbox;
    /// a transaction-list row doesn't carry them.
    static func of(
        _ transaction: Transaction,
        needsResplit: Bool = false,
        awaitingBankMatch: Bool = false,
        in context: Context
    ) -> [TransactionStatus] {
        var result: [TransactionStatus] = []
        if needsResplit { result.append(.needsResplit) }
        if awaitingBankMatch { result.append(.awaitingBankMatch) }
        if !transaction.reviewed, context == .transactions { result.append(.unreviewed) }
        if transaction.pending { result.append(.pending) }
        if transaction.isSplit { result.append(.split) }
        return result
    }

    var label: String {
        switch self {
        case .needsResplit: "Needs re-split"
        case .awaitingBankMatch: "Waiting for bank match"
        case .unreviewed: "Unreviewed"
        case .pending: "Pending"
        case .split: "Split"
        }
    }

    var symbol: String {
        switch self {
        case .needsResplit: "exclamationmark.triangle"
        case .awaitingBankMatch: "hourglass"
        case .unreviewed: "tray"
        case .pending: "clock"
        case .split: "arrow.triangle.branch"
        }
    }

    /// Only a status the user has to act on draws attention.
    var tone: StatusBadge.Tone {
        self == .needsResplit ? .attention : .neutral
    }
}

/// A status as a small label: the symbol in `Attention` orange when it needs action, otherwise secondary; the
/// text always secondary so it stays readable.
struct StatusBadge: View {
    enum Tone: Equatable {
        case neutral, attention
    }

    let label: String
    let symbol: String
    let tone: Tone

    init(_ label: String, systemImage symbol: String, tone: Tone = .neutral) {
        self.label = label
        self.symbol = symbol
        self.tone = tone
    }

    init(_ status: TransactionStatus) {
        self.init(status.label, systemImage: status.symbol, tone: status.tone)
    }

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: symbol)
                .foregroundStyle(tone == .attention ? AnyShapeStyle(Color(.attention)) : AnyShapeStyle(.secondary))
            Text(label).foregroundStyle(.secondary)
        }
        .font(.caption)
        .accessibilityElement(children: .combine)
    }
}
