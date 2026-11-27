import Foundation

/// `GET /api/ai/usage`: this month's Claude spend against the cap (#168). `enabled` is false
/// when the server has no API key, and the More screen then shows nothing.
struct AIUsage: Decodable, Equatable {
    let month: String
    let spentUsd: Money
    let budgetUsd: Money
    let enabled: Bool
}
