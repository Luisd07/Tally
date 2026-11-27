import Foundation

/// Mirrors `ReceiptOut` from `POST /api/transactions/{id}/receipts`.
struct Receipt: Decodable, Identifiable, Hashable {
    let id: UUID
    let transactionId: UUID
    let mime: String
    let sha256: String
    let uploadedAt: Date
}

/// One receipt from `GET /api/transactions/{id}/receipts`, with what Claude read off it (#170).
struct ReceiptWithReading: Decodable, Identifiable, Hashable {
    let id: UUID
    let transactionId: UUID
    let uploadedAt: Date
    /// Nil until the server has read it.
    let reading: ReceiptReading?
}

/// What Claude read off a receipt, for the user to confirm; nothing is applied on its own.
struct ReceiptReading: Decodable, Hashable {
    enum Status: OpenEnum, Hashable {
        case ready, retry, failed, other(String)

        nonisolated static var known: [String: Status] { ["ready": .ready, "retry": .retry, "failed": .failed] }
        nonisolated static func unknown(_ raw: String) -> Status { .other(raw) }
    }

    struct LineItem: Decodable, Hashable {
        let description: String
        let amount: Money
    }

    let status: Status
    let merchant: String?
    let receiptDate: CalendarDate?
    let subtotal: Money?
    let tax: Money?
    let tip: Money?
    let total: Money?
    let lineItems: [LineItem]
    let categoryId: UUID?
    /// False when the line items, tax and tip don't make the total; nil with nothing to check.
    let addsUp: Bool?
    /// False when the total isn't the charge (a tip added later does this).
    let matchesTransaction: Bool?
}
