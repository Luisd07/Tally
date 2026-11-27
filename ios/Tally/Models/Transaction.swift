import Foundation

/// Spending bucket; unknown server values decode to `.other` instead of failing the response.
enum Bucket: PickableOpenEnum, Hashable {
    case personal, business, reimbursable, transfer, income, other(String)

    nonisolated static var allKnown: [Bucket] { [.personal, .business, .reimbursable, .transfer, .income] }
    nonisolated static func unknown(_ raw: String) -> Bucket { .other(raw) }

    /// The wire string, for requests. The only place the bucket strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .personal: "personal"
        case .business: "business"
        case .reimbursable: "reimbursable"
        case .transfer: "transfer"
        case .income: "income"
        case .other(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .personal: "Personal"
        case .business: "Business"
        case .reimbursable: "Reimbursable"
        case .transfer: "Transfer"
        case .income: "Income"
        case .other(let raw): raw.capitalized
        }
    }
}

/// Where a transaction came from; unknown server values decode to `.other`.
enum Source: OpenEnum, Hashable {
    case simplefin, csv, manual, sheetImport, other(String)

    nonisolated static var known: [String: Source] {
        ["simplefin": .simplefin, "csv": .csv, "manual": .manual, "sheet_import": .sheetImport]
    }
    nonisolated static func unknown(_ raw: String) -> Source { .other(raw) }

    var label: String {
        switch self {
        case .simplefin: "SimpleFIN"
        case .csv: "CSV"
        case .manual: "Manual"
        case .sheetImport: "Sheet import"
        case .other(let raw): raw
        }
    }
}

/// Mirrors `TransactionOut`. `amount` keeps SimpleFIN's sign: spending is negative.
struct Transaction: Decodable, Identifiable, Hashable {
    let id: UUID
    let accountId: UUID
    let postedDate: CalendarDate
    let transactedAt: Date?
    let amount: Money
    let descriptionRaw: String
    let merchant: String?
    let categoryId: UUID?
    let bucket: Bucket
    let source: Source
    let externalId: String?
    let pending: Bool
    let reviewed: Bool
    let notes: String?
    let eventId: UUID?
    let parentId: UUID?
    let reimbursementId: UUID?
    let isSplit: Bool
    let createdAt: Date
    let updatedAt: Date

    /// What the merchant or, failing that, the bank's description says.
    var displayName: String { merchant ?? descriptionRaw }

    /// Whether it can be split: not a split's child and not pending.
    var canSplit: Bool { parentId == nil && !pending }

    /// This transaction with the fields `patch` sets, as the server will return it.
    func applying(_ patch: TransactionPatch) -> Transaction {
        Transaction(
            id: id,
            accountId: accountId,
            postedDate: postedDate,
            transactedAt: transactedAt,
            amount: amount,
            descriptionRaw: descriptionRaw,
            merchant: patch.merchant ?? merchant,
            categoryId: patch.categoryId ?? categoryId,
            bucket: patch.bucket ?? bucket,
            source: source,
            externalId: externalId,
            pending: pending,
            reviewed: patch.reviewed ?? reviewed,
            notes: patch.notes ?? notes,
            eventId: patch.eventId ?? eventId,
            parentId: parentId,
            reimbursementId: reimbursementId,
            isSplit: isSplit,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }
}

/// Mirrors `TransactionPage` from `GET /api/transactions`.
struct TransactionPage: Decodable {
    let items: [Transaction]
    let nextCursor: String?
}

/// Where an Inbox suggestion came from: the last reviewed row with the same merchant, or Claude (#169).
enum SuggestionSource: OpenEnum, Hashable {
    case history, ai, other(String)

    nonisolated static var known: [String: SuggestionSource] { ["history": .history, "ai": .ai] }
    nonisolated static func unknown(_ raw: String) -> SuggestionSource { .other(raw) }
}

/// Mirrors `InboxItemOut`: a transaction plus the flags and suggestions that put it in the Inbox.
struct InboxItem: Decodable, Identifiable, Hashable {
    let transaction: Transaction
    let needsResplit: Bool
    let unmatchedManual: Bool
    let suggestedCategoryId: UUID?
    let suggestedBucket: Bucket?
    let suggestionSource: SuggestionSource?
    /// A clean merchant name to set with the suggestion; only AI suggestions carry one.
    let suggestedMerchant: String?
    /// Confirming this suggestion would make three for its merchant, so it's worth offering as a rule.
    let ruleWorthy: Bool

    var id: UUID { transaction.id }
    var isAISuggestion: Bool { suggestionSource == .ai }

    init(
        transaction: Transaction,
        needsResplit: Bool,
        unmatchedManual: Bool,
        suggestedCategoryId: UUID?,
        suggestedBucket: Bucket?,
        suggestionSource: SuggestionSource? = nil,
        suggestedMerchant: String? = nil,
        ruleWorthy: Bool = false
    ) {
        self.transaction = transaction
        self.needsResplit = needsResplit
        self.unmatchedManual = unmatchedManual
        self.suggestedCategoryId = suggestedCategoryId
        self.suggestedBucket = suggestedBucket
        self.suggestionSource = suggestionSource
        self.suggestedMerchant = suggestedMerchant
        self.ruleWorthy = ruleWorthy
    }

    private enum CodingKeys: String, CodingKey {
        case needsResplit, unmatchedManual, suggestedCategoryId, suggestedBucket, suggestionSource, suggestedMerchant
        case ruleWorthy
    }

    init(from decoder: Decoder) throws {
        transaction = try Transaction(from: decoder)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        needsResplit = try container.decode(Bool.self, forKey: .needsResplit)
        unmatchedManual = try container.decode(Bool.self, forKey: .unmatchedManual)
        suggestedCategoryId = try container.decodeIfPresent(UUID.self, forKey: .suggestedCategoryId)
        suggestedBucket = try container.decodeIfPresent(Bucket.self, forKey: .suggestedBucket)
        suggestionSource = try container.decodeIfPresent(SuggestionSource.self, forKey: .suggestionSource)
        suggestedMerchant = try container.decodeIfPresent(String.self, forKey: .suggestedMerchant)
        ruleWorthy = try container.decodeIfPresent(Bool.self, forKey: .ruleWorthy) ?? false
    }
}

/// Mirrors `InboxPage` from `GET /api/inbox`; `total` counts every unreviewed item, not just this page.
struct InboxPage: Decodable {
    let items: [InboxItem]
    let nextCursor: String?
    let total: Int
}

/// Body for `PATCH /api/transactions/{id}`. Only fields that were set are sent.
/// The double-optional fields take `.some(nil)` to clear on the server; `bucket` and `reviewed`
/// are single-optional (nil = omitted, never sent as null) because the server rejects an explicit null.
struct TransactionPatch: Encodable {
    var categoryId: UUID??
    var bucket: Bucket?
    var notes: String??
    var reviewed: Bool?
    var merchant: String??
    var eventId: UUID??

    private enum CodingKeys: String, CodingKey {
        case categoryId, bucket, notes, reviewed, merchant, eventId
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodePatchField(categoryId, forKey: .categoryId)
        if let bucket { try container.encode(bucket, forKey: .bucket) }
        try container.encodePatchField(notes, forKey: .notes)
        if let reviewed { try container.encode(reviewed, forKey: .reviewed) }
        try container.encodePatchField(merchant, forKey: .merchant)
        try container.encodePatchField(eventId, forKey: .eventId)
    }
}

/// Body for `POST /api/transactions/bulk`: one `patch` for every row in `ids` (1 to `maxIds`, unique).
struct TransactionBulkUpdate: Encodable {
    /// The most ids the server takes in one request.
    static let maxIds = 500

    let ids: [UUID]
    let patch: TransactionPatch
}

/// Mirrors `TransactionBulkResult`: how many rows changed and each as saved, in the order of the request's ids.
struct BulkResult: Decodable {
    let updated: Int
    let items: [Transaction]
}
