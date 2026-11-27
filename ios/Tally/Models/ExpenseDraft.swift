import Foundation

/// `POST /api/ai/parse-expense` (#171): what the Add screen fills in from a plain-language note.
/// Nothing is saved on the server; any field may be empty.
struct ExpenseDraft: Decodable, Equatable {
    enum Direction: OpenEnum, Hashable {
        case expense, income, other(String)

        nonisolated static var known: [String: Direction] { ["expense": .expense, "income": .income] }
        nonisolated static func unknown(_ raw: String) -> Direction { .other(raw) }
    }

    let direction: Direction
    /// Always positive; `direction` decides the sign.
    let amount: Money?
    let merchant: String?
    let description: String?
    let date: CalendarDate
    let categoryId: UUID?
    let bucket: Bucket?
}

struct ParseExpenseBody: Encodable {
    let text: String
}
