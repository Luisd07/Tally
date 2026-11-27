import Foundation

/// What an insight noticed; unknown server values decode to `.other`.
enum InsightKind: OpenEnum, Hashable {
    case pace, unusualCharge, newSubscription, priceChange, promoPace, other(String)

    nonisolated static var known: [String: InsightKind] {
        [
            "pace": .pace, "unusual_charge": .unusualCharge,
            "new_subscription": .newSubscription, "price_change": .priceChange,
            "promo_pace": .promoPace,
        ]
    }
    nonisolated static func unknown(_ raw: String) -> InsightKind { .other(raw) }

    var systemImage: String {
        switch self {
        case .pace: "speedometer"
        case .unusualCharge: "exclamationmark.circle"
        case .newSubscription: "arrow.triangle.2.circlepath"
        case .priceChange: "arrow.up.right"
        case .promoPace: "percent"
        case .other: "lightbulb"
        }
    }
}

/// Mirrors `InsightOut` (#225): something worth a heads-up, written in plain words. `actionId` is set when Claude
/// proposed a fix alongside it, and `actionStatus` is where that action stands.
struct Insight: Decodable, Identifiable, Hashable {
    let id: UUID
    let kind: InsightKind
    let title: String
    let message: String
    let day: CalendarDate
    let categoryId: UUID?
    let transactionId: UUID?
    let subscriptionId: UUID?
    let actionId: UUID?
    let actionStatus: AIActionStatus?
}
