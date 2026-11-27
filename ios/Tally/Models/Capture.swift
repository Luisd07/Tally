import Foundation

/// A manually captured expense, stored in the outbox until the server has it.
/// The phone mints `id` (and `receiptId`) so a retry never creates a second row.
struct Capture: Codable, Hashable, Identifiable {
    let id: UUID
    var accountId: UUID
    var postedDate: CalendarDate
    /// Expenses are negative.
    var amount: Money
    var descriptionRaw: String
    var merchant: String?
    var categoryId: UUID?
    var bucket: Bucket
    var notes: String?
    var transactedAt: Date?
    /// Set when a receipt photo is queued; its bytes live in a sibling file, and it is not part of the create request.
    var receiptId: UUID?
    /// The event to link once the row exists; `TransactionCreate` has no `event_id`, so the outbox sends it as a PATCH.
    var eventId: UUID?

    init(
        id: UUID = UUID(),
        accountId: UUID,
        postedDate: CalendarDate,
        amount: Money,
        descriptionRaw: String,
        merchant: String? = nil,
        categoryId: UUID? = nil,
        bucket: Bucket = .personal,
        notes: String? = nil,
        transactedAt: Date? = nil,
        receiptId: UUID? = nil,
        eventId: UUID? = nil
    ) {
        self.id = id
        self.accountId = accountId
        self.postedDate = postedDate
        self.amount = amount
        self.descriptionRaw = descriptionRaw
        self.merchant = merchant
        self.categoryId = categoryId
        self.bucket = bucket
        self.notes = notes
        self.transactedAt = transactedAt
        self.receiptId = receiptId
        self.eventId = eventId
    }

    /// The `POST /api/transactions` body: exactly `TransactionCreate`'s keys, nil optionals omitted.
    var createBody: CreateBody { CreateBody(capture: self) }

    /// The create request's view of a capture: everything except `receiptId` and `eventId`, with `transactedAt` as ISO 8601.
    struct CreateBody: Encodable {
        let capture: Capture

        private enum CodingKeys: String, CodingKey {
            case id, accountId, postedDate, amount, descriptionRaw, merchant, categoryId, bucket, notes, transactedAt
        }

        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(capture.id, forKey: .id)
            try container.encode(capture.accountId, forKey: .accountId)
            try container.encode(capture.postedDate, forKey: .postedDate)
            try container.encode(capture.amount, forKey: .amount)
            try container.encode(capture.descriptionRaw, forKey: .descriptionRaw)
            try container.encodeIfPresent(capture.merchant, forKey: .merchant)
            try container.encodeIfPresent(capture.categoryId, forKey: .categoryId)
            try container.encode(capture.bucket, forKey: .bucket)
            try container.encodeIfPresent(capture.notes, forKey: .notes)
            let isoStyle = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
            let transactedAt = capture.transactedAt?.formatted(isoStyle)
            try container.encodeIfPresent(transactedAt, forKey: .transactedAt)
        }
    }
}
