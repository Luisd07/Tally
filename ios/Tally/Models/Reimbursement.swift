import Foundation

/// Where an expense report stands; unknown server values decode to `.unrecognized`.
enum ReimbursementStatus: PickableOpenEnum, Hashable {
    case draft, submitted, paid, unrecognized(String)

    nonisolated static var allKnown: [ReimbursementStatus] { [.draft, .submitted, .paid] }
    nonisolated static func unknown(_ raw: String) -> ReimbursementStatus { .unrecognized(raw) }

    /// The wire string, for requests. The only place the status strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .draft: "draft"
        case .submitted: "submitted"
        case .paid: "paid"
        case .unrecognized(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .draft: "Draft"
        case .submitted: "Submitted"
        case .paid: "Paid"
        case .unrecognized(let raw): raw.capitalized
        }
    }

    /// The one step the server accepts from here; nil for a paid or unknown report.
    var next: ReimbursementStatus? {
        switch self {
        case .draft: .submitted
        case .submitted: .paid
        case .paid, .unrecognized: nil
        }
    }

    /// What the user can do with a report from here: the move to `next`. Nil for a paid or unknown report.
    var step: ReimbursementStep? {
        switch self {
        case .draft: .submit
        case .submitted: .markPaid
        case .paid, .unrecognized: nil
        }
    }
}

/// A move the user makes on a report: submit a draft, or mark a submitted report paid.
enum ReimbursementStep: Hashable {
    case submit, markPaid

    var title: String {
        switch self {
        case .submit: "Submit"
        case .markPaid: "Mark paid"
        }
    }
}

/// Mirrors `ReimbursementOut`. `total` is a positive amount owed back: the sum of the counted expenses' magnitudes.
struct Reimbursement: Decodable, Identifiable, Hashable {
    let id: UUID
    let status: ReimbursementStatus
    let submittedDate: CalendarDate?
    let paidDate: CalendarDate?
    let incomeEntryId: UUID?
    let note: String?
    let total: Money
    let transactionCount: Int
    let createdAt: Date
    let updatedAt: Date

    /// What to call this report in a list or an error message.
    var displayName: String { note ?? "Untitled report" }

    /// Whether expenses can be added to or removed from it: the server only allows that while it is a draft.
    var isDraft: Bool { status == .draft }

    /// This report with the fields `patch` sets, as the server will return it. The total and count stay: they
    /// depend on expenses the patch only names.
    func applying(_ patch: ReimbursementPatch) -> Reimbursement {
        Reimbursement(
            id: id,
            status: patch.status ?? status,
            submittedDate: patch.submittedDate ?? submittedDate,
            paidDate: patch.paidDate ?? paidDate,
            incomeEntryId: patch.incomeEntryId ?? incomeEntryId,
            note: patch.note ?? note,
            total: total,
            transactionCount: transactionCount,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }
}

/// Mirrors `ReimbursementPage` from `GET /api/reimbursements`: every report at once, newest first.
struct ReimbursementPage: Decodable {
    let items: [Reimbursement]
}

/// Mirrors `OwedOut` from `GET /api/reimbursements/owed`: what is still owed back to you, as Home shows it.
struct Owed: Decodable, Hashable {
    let owed: Money
}

/// The new-report sheet's note, validated: trimmed, nil when blank, and at most `maxNoteLength` long.
struct ReimbursementForm: Equatable {
    var note: String?

    /// The server's limit for a note, in Unicode scalars (Python counts a `str` in code points).
    static let maxNoteLength = 500

    /// `noteText` as a form, or the rule it breaks.
    static func validated(noteText: String) -> Result<ReimbursementForm, Invalid> {
        let note = noteText.trimmedOrNil
        if let note, note.unicodeScalars.count > maxNoteLength { return .failure(.noteTooLong) }
        return .success(ReimbursementForm(note: note))
    }

    enum Invalid: FormRule, Equatable {
        case noteTooLong

        var message: String { "The note can be at most \(ReimbursementForm.maxNoteLength) characters." }
    }
}

/// Body for `POST /api/reimbursements`. Unset optionals are left out. `id` is made on the phone, so retrying
/// the same draft never creates a second report; the expenses are only attached when the report is created.
struct ReimbursementDraft: Encodable {
    var id = UUID()
    var note: String?
    var transactionIds: [UUID]?
}

/// Body for `PATCH /api/reimbursements/{id}`. Only fields that were set are sent.
/// The double-optional fields take `.some(nil)` to clear on the server; `status` is single-optional (nil =
/// omitted, never sent as null) because the server rejects an explicit null for it. The expense lists are
/// omitted when nil; the server honors them only while the report is a draft.
struct ReimbursementPatch: Encodable, Equatable {
    var status: ReimbursementStatus?
    var submittedDate: CalendarDate??
    var paidDate: CalendarDate??
    var incomeEntryId: UUID??
    var note: String??
    var addTransactionIds: [UUID]?
    var removeTransactionIds: [UUID]?

    private enum CodingKeys: String, CodingKey {
        case status, submittedDate, paidDate, incomeEntryId, note, addTransactionIds, removeTransactionIds
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let status { try container.encode(status, forKey: .status) }
        try container.encodePatchField(submittedDate, forKey: .submittedDate)
        try container.encodePatchField(paidDate, forKey: .paidDate)
        try container.encodePatchField(incomeEntryId, forKey: .incomeEntryId)
        try container.encodePatchField(note, forKey: .note)
        if let addTransactionIds { try container.encode(addTransactionIds, forKey: .addTransactionIds) }
        if let removeTransactionIds { try container.encode(removeTransactionIds, forKey: .removeTransactionIds) }
    }
}
