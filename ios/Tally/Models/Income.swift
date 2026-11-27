import Foundation

/// What an income entry is for; unknown server values decode to `.unrecognized`.
enum IncomeType: PickableOpenEnum, Hashable {
    case eventPay, remoteHours, reimbursement, other, unrecognized(String)

    nonisolated static var allKnown: [IncomeType] { [.eventPay, .remoteHours, .reimbursement, .other] }
    nonisolated static func unknown(_ raw: String) -> IncomeType { .unrecognized(raw) }

    /// The wire string, for requests. The only place the type strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .eventPay: "event_pay"
        case .remoteHours: "remote_hours"
        case .reimbursement: "reimbursement"
        case .other: "other"
        case .unrecognized(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .eventPay: "Event pay"
        case .remoteHours: "Remote hours"
        case .reimbursement: "Reimbursement"
        case .other: "Other"
        case .unrecognized(let raw): raw.capitalized
        }
    }
}

/// Mirrors `IncomeOut`. An entry has an expected amount, a received amount, or both.
struct IncomeEntry: Decodable, Identifiable, Hashable {
    let id: UUID
    let date: CalendarDate
    let type: IncomeType
    let description: String?
    let expected: Money?
    let received: Money?
    let transactionId: UUID?
    let eventId: UUID?
    let createdAt: Date
    let updatedAt: Date

    /// What to call this entry in a list or an error message.
    var displayName: String { description ?? type.label }

    /// Whether the money has all arrived: received at least the expected amount. An entry with only a
    /// received amount (nothing was expected) counts as fully received.
    var isFullyReceived: Bool {
        guard let received else { return false }
        guard let expected else { return true }
        return received >= expected
    }

    /// This entry with the fields `patch` sets, as the server will return it.
    func applying(_ patch: IncomePatch) -> IncomeEntry {
        IncomeEntry(
            id: id,
            date: patch.date ?? date,
            type: patch.type ?? type,
            description: patch.description ?? description,
            expected: patch.expected ?? expected,
            received: patch.received ?? received,
            transactionId: patch.transactionId ?? transactionId,
            eventId: patch.eventId ?? eventId,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }
}

/// Mirrors `IncomeTotals`: the month's expected and received sums.
struct IncomeTotals: Decodable, Hashable {
    var expected: Money
    var received: Money

    static let zero = IncomeTotals(expected: Money(0), received: Money(0))
}

/// Mirrors `IncomeListOut` from `GET /api/income`: the whole month at once. Unlike the `*Page` types of
/// cursor-paged lists it has no cursor; the server never splits a month.
struct IncomePage: Decodable {
    let items: [IncomeEntry]
    let totals: IncomeTotals
}

/// Body for `POST /api/income`. Unset optionals are left out; the server needs `expected` or `received`.
/// `id` is made on the phone, so retrying the same draft never creates a second row.
struct IncomeDraft: Encodable {
    var id = UUID()
    var date: CalendarDate
    var type: IncomeType
    var description: String?
    var expected: Money?
    var received: Money?
    var transactionId: UUID?
    var eventId: UUID?
}

extension IncomeDraft {
    /// A draft of what `form` holds, under `id` so a retry sends the same one.
    init(id: UUID, form: IncomeForm) {
        self.init(
            id: id, date: form.date, type: form.type, description: form.description,
            expected: form.expected, received: form.received
        )
    }
}

/// The fields of the income sheet, validated: the description trimmed (nil when blank) and at least one amount.
struct IncomeForm: Equatable {
    var date: CalendarDate
    var type: IncomeType
    var description: String?
    var expected: Money?
    var received: Money?
}

extension IncomeForm {
    /// Nil when an amount isn't one `Money(userInput:)` reads, or both are blank.
    init?(date: CalendarDate, type: IncomeType, description: String, expectedText: String, receivedText: String) {
        guard let expected = Self.amount(expectedText), let received = Self.amount(receivedText),
              expected != nil || received != nil
        else { return nil }
        self.init(date: date, type: type, description: description.trimmedOrNil, expected: expected, received: received)
    }

    /// `.some(nil)` when blank, nil when invalid.
    private static func amount(_ text: String) -> Money?? {
        guard text.trimmedOrNil != nil else { return .some(nil) }
        return Money(userInput: text).map { .some($0) }
    }
}

/// Body for `PATCH /api/income/{id}`. Only fields that were set are sent.
/// The double-optional fields take `.some(nil)` to clear on the server; `date` and `type`
/// are single-optional (nil = omitted, never sent as null) because the server rejects an explicit null.
struct IncomePatch: Encodable {
    var date: CalendarDate?
    var type: IncomeType?
    var description: String??
    var expected: Money??
    var received: Money??
    var transactionId: UUID??
    var eventId: UUID??

    private enum CodingKeys: String, CodingKey {
        case date, type, description, expected, received, transactionId, eventId
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let date { try container.encode(date, forKey: .date) }
        if let type { try container.encode(type, forKey: .type) }
        try container.encodePatchField(description, forKey: .description)
        try container.encodePatchField(expected, forKey: .expected)
        try container.encodePatchField(received, forKey: .received)
        try container.encodePatchField(transactionId, forKey: .transactionId)
        try container.encodePatchField(eventId, forKey: .eventId)
    }
}

extension IncomePatch {
    /// The fields where `form` differs from `original`; a field the form left blank clears it. Nil when nothing changed.
    init?(from original: IncomeEntry, to form: IncomeForm) {
        self.init(
            date: form.date == original.date ? nil : form.date,
            type: form.type == original.type ? nil : form.type,
            description: form.description == original.description ? nil : .some(form.description),
            expected: form.expected == original.expected ? nil : .some(form.expected),
            received: form.received == original.received ? nil : .some(form.received)
        )
        if date == nil, type == nil, description == nil, expected == nil, received == nil { return nil }
    }
}
