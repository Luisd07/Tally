import Foundation

/// `GET /api/ai/weekly-review` (#173): Claude's short review of last week, for Home.
struct WeeklyReview: Decodable, Equatable {
    enum Kind: OpenEnum, Hashable {
        case doubleCharge, priceIncrease, missingReceipt, unusual, other(String)

        nonisolated static var known: [String: Kind] {
            [
                "double_charge": .doubleCharge, "price_increase": .priceIncrease,
                "missing_receipt": .missingReceipt, "unusual": .unusual,
            ]
        }
        nonisolated static func unknown(_ raw: String) -> Kind { .other(raw) }

        var label: String {
            switch self {
            case .doubleCharge: "Possible double charge"
            case .priceIncrease: "Price went up"
            case .missingReceipt: "No receipt"
            case .unusual: "Unusual"
            case .other: "Worth a look"
            }
        }

        var systemImage: String {
            switch self {
            case .doubleCharge: "doc.on.doc"
            case .priceIncrease: "arrow.up.right"
            case .missingReceipt: "doc.badge.ellipsis"
            case .unusual, .other: "exclamationmark.circle"
            }
        }
    }

    /// Enough of the flagged transaction to show it and open it.
    struct FindingTransaction: Decodable, Equatable, Hashable {
        let id: UUID
        let postedDate: CalendarDate
        let descriptionRaw: String
        let merchant: String?
        let amount: Money

        var displayName: String { merchant ?? descriptionRaw }
    }

    struct Finding: Decodable, Equatable {
        let kind: Kind
        let note: String
        /// Nil when the transaction has since been deleted.
        let transaction: FindingTransaction?
    }

    let weekStart: CalendarDate
    let weekEnd: CalendarDate
    let summary: String
    let findings: [Finding]
}
