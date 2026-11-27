import Foundation

/// How often a subscription or bill charges; unknown server values decode to `.unrecognized`.
enum Cadence: PickableOpenEnum, Hashable {
    case weekly, monthly, quarterly, yearly, unrecognized(String)

    nonisolated static var allKnown: [Cadence] { [.weekly, .monthly, .quarterly, .yearly] }
    nonisolated static func unknown(_ raw: String) -> Cadence { .unrecognized(raw) }

    /// The wire string, for requests. The only place the cadence strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .weekly: "weekly"
        case .monthly: "monthly"
        case .quarterly: "quarterly"
        case .yearly: "yearly"
        case .unrecognized(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .weekly: "Weekly"
        case .monthly: "Monthly"
        case .quarterly: "Quarterly"
        case .yearly: "Yearly"
        case .unrecognized(let raw): raw.capitalized
        }
    }

    /// Charges per year, as the server costs it; an unknown cadence counts as monthly.
    var periodsPerYear: Int {
        switch self {
        case .weekly: 52
        case .quarterly: 4
        case .yearly: 1
        case .monthly, .unrecognized: 12
        }
    }
}

/// A subscription (streaming, software) or a bill (the car payment); unknown server values decode to `.unrecognized`.
enum SubscriptionKind: PickableOpenEnum, Hashable {
    case subscription, bill, unrecognized(String)

    nonisolated static var allKnown: [SubscriptionKind] { [.subscription, .bill] }
    nonisolated static func unknown(_ raw: String) -> SubscriptionKind { .unrecognized(raw) }

    /// The wire string, for requests. The only place the kind strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .subscription: "subscription"
        case .bill: "bill"
        case .unrecognized(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .subscription: "Subscription"
        case .bill: "Bill"
        case .unrecognized(let raw): raw.capitalized
        }
    }
}

/// Mirrors `PriceChange`: the stored amount against the latest linked charge.
struct PriceChange: Decodable, Hashable {
    let old: Money
    let new: Money
}

/// Mirrors `SubscriptionFlags`, computed by the server on read.
struct SubscriptionFlags: Decodable, Hashable {
    var priceChange: PriceChange?
    var missed = false
    var dormant = false
}

/// Mirrors `SubscriptionOut`. `amount` is negative, like `Transaction.amount`.
struct Subscription: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let amount: Money
    let cadence: Cadence
    let kind: SubscriptionKind
    let accountId: UUID?
    let categoryId: UUID?
    let bucket: Bucket
    let nextDue: CalendarDate?
    let active: Bool
    let startDate: CalendarDate?
    let endDate: CalendarDate?
    let businessPct: Int?
    let matchText: String?
    let createdAt: Date
    let updatedAt: Date
    let flags: SubscriptionFlags

    /// What it costs per charge, as a positive amount.
    var cost: Money { amount.negated }

    /// This row with the fields `patch` sets, as the server will return it.
    func applying(_ patch: SubscriptionPatch) -> Subscription {
        Subscription(
            id: id,
            name: patch.name ?? name,
            amount: patch.amount ?? amount,
            cadence: patch.cadence ?? cadence,
            kind: patch.kind ?? kind,
            accountId: patch.accountId ?? accountId,
            categoryId: patch.categoryId ?? categoryId,
            bucket: patch.bucket ?? bucket,
            nextDue: patch.nextDue ?? nextDue,
            active: patch.active ?? active,
            startDate: patch.startDate ?? startDate,
            endDate: patch.endDate ?? endDate,
            businessPct: patch.businessPct ?? businessPct,
            matchText: patch.matchText ?? matchText,
            createdAt: createdAt,
            updatedAt: updatedAt,
            flags: flags
        )
    }
}

/// Mirrors `SubscriptionTotals`: negative monthly and annual cost, normalized across cadences.
struct SubscriptionTotals: Decodable, Hashable {
    var monthly: Money
    var annual: Money

    static let zero = SubscriptionTotals(monthly: Money(0), annual: Money(0))

    static func + (lhs: SubscriptionTotals, rhs: SubscriptionTotals) -> SubscriptionTotals {
        SubscriptionTotals(monthly: lhs.monthly + rhs.monthly, annual: lhs.annual + rhs.annual)
    }
}

/// Mirrors `SubscriptionPage.totals`: active rows only, keyed by the kind's wire string. A kind the server
/// sent no totals for costs zero.
struct SubscriptionTotalsByKind: Decodable, Hashable {
    private var byKind: [String: SubscriptionTotals]

    static let empty = SubscriptionTotalsByKind(byKind: [:])

    private init(byKind: [String: SubscriptionTotals]) {
        self.byKind = byKind
    }

    init(from decoder: Decoder) throws {
        byKind = try decoder.singleValueContainer().decode([String: SubscriptionTotals].self)
    }

    /// The totals the server computes for `rows`: per kind, the exact sum of the active rows' amount times
    /// charges per year, the monthly one twelfth of that, each rounded to cents once.
    init(costing rows: [Subscription]) {
        var annual: [String: Decimal] = [:]
        for row in rows where row.active {
            annual[row.kind.rawValue, default: 0] += row.amount.amount * Decimal(row.cadence.periodsPerYear)
        }
        self.init(byKind: annual.mapValues {
            SubscriptionTotals(monthly: Money($0 / 12).roundedToCents, annual: Money($0).roundedToCents)
        })
    }

    /// The totals for `kind`; zero when there are none.
    subscript(kind kind: SubscriptionKind) -> SubscriptionTotals {
        byKind[kind.rawValue] ?? .zero
    }

    /// Every other kind's totals summed, unrecognized kinds included.
    func excluding(_ kind: SubscriptionKind) -> SubscriptionTotals {
        byKind.filter { $0.key != kind.rawValue }.values.reduce(.zero, +)
    }
}

/// Mirrors `SubscriptionPage` from `GET /api/subscriptions`: every row at once, with totals per kind.
struct SubscriptionPage: Decodable {
    let items: [Subscription]
    let totals: SubscriptionTotalsByKind

    /// The totals for `kind`; zero when the server sent none.
    func totals(for kind: SubscriptionKind) -> SubscriptionTotals {
        totals[kind: kind]
    }
}

/// Body for `POST /api/subscriptions`. Unset optionals are left out. `id` is made on the phone, so retrying
/// the same draft never creates a second row. `amount` is negative.
struct SubscriptionDraft: Encodable {
    var id = UUID()
    var name: String
    var amount: Money
    var cadence: Cadence
    var kind: SubscriptionKind = .subscription
    var accountId: UUID?
    var categoryId: UUID?
    var bucket: Bucket = .personal
    var nextDue: CalendarDate?
    var active = true
    var startDate: CalendarDate?
    var endDate: CalendarDate?
    var businessPct: Int?
    var matchText: String?
}

extension SubscriptionDraft {
    /// A draft of what `form` holds, under `id` so a retry sends the same one.
    init(id: UUID, form: SubscriptionForm) {
        self.init(
            id: id, name: form.name, amount: form.amount, cadence: form.cadence, kind: form.kind,
            accountId: form.accountId, categoryId: form.categoryId, bucket: form.bucket,
            nextDue: form.nextDue, active: form.active, startDate: form.startDate, endDate: form.endDate,
            businessPct: form.businessPct, matchText: form.matchText
        )
    }
}

/// The subscription sheet's fields as entered, before validation. A new one holds an add's defaults.
struct SubscriptionFields: Equatable {
    var name = ""
    var costText = ""
    var cadence: Cadence = .monthly
    var kind: SubscriptionKind = .subscription
    var bucket: Bucket = .personal
    var accountId: UUID?
    var categoryId: UUID?
    /// Whether the next charge date is set; `nextDue` keeps the picker's day while it isn't.
    var hasNextDue = false
    var nextDue = CalendarDate(date: .now)
    var active = true
    var businessPctText = ""
    /// Not shown in the sheet; an edit keeps the row's.
    var startDate: CalendarDate?
    var endDate: CalendarDate?
    var matchText = ""
}

extension SubscriptionFields {
    /// The fields of `row`, for an edit.
    init(editing row: Subscription) {
        self.init(
            name: row.name, costText: row.cost.inputText, cadence: row.cadence, kind: row.kind, bucket: row.bucket,
            accountId: row.accountId, categoryId: row.categoryId, hasNextDue: row.nextDue != nil,
            nextDue: row.nextDue ?? CalendarDate(date: .now), active: row.active,
            businessPctText: row.businessPct.map(String.init) ?? "", startDate: row.startDate,
            endDate: row.endDate, matchText: row.matchText ?? ""
        )
    }

    /// The validated form, or the first rule the fields break.
    var validated: Result<SubscriptionForm, SubscriptionForm.Invalid> {
        guard let name = name.trimmedOrNil, name.count <= 100 else { return .failure(.name) }
        guard let cost = Money(userInput: costText), cost.amount > 0 else { return .failure(.cost) }
        guard let businessPct = businessPctText.parsedUnlessBlank({ Percent(userInput: $0, wholeOnly: true)?.wholeNumber })
        else { return .failure(.businessShare) }
        let matchText = matchText.trimmedOrNil
        if let matchText, matchText.count > 200 { return .failure(.matchText) }
        if let startDate, let endDate, endDate < startDate { return .failure(.endBeforeStart) }
        return .success(SubscriptionForm(
            name: name, cost: cost, cadence: cadence, kind: kind, bucket: bucket, accountId: accountId,
            categoryId: categoryId, nextDue: hasNextDue ? nextDue : nil, active: active, startDate: startDate,
            endDate: endDate, businessPct: businessPct, matchText: matchText
        ))
    }
}

/// The fields of the subscription sheet, validated: the name trimmed and 1-100 characters, a positive cost,
/// a business share of 0-100, a match text of at most 200 characters (nil when blank), and no end before the start.
struct SubscriptionForm: Equatable {
    var name: String
    /// The cost per charge as entered, positive.
    var cost: Money
    var cadence: Cadence
    var kind: SubscriptionKind
    var bucket: Bucket
    var accountId: UUID?
    var categoryId: UUID?
    var nextDue: CalendarDate?
    var active: Bool
    var startDate: CalendarDate?
    var endDate: CalendarDate?
    var businessPct: Int?
    var matchText: String?

    /// What the server stores: the cost as an outflow.
    var amount: Money { cost.negated }

    enum Invalid: FormRule, Equatable {
        case name, cost, businessShare, matchText, endBeforeStart

        var message: String {
            switch self {
            case .name: "Enter a name of up to 100 characters."
            case .cost: "Enter an amount above zero, like 15.49."
            case .businessShare: "Business % must be a whole number from 0 to 100."
            case .matchText: "The match text can't be over 200 characters."
            case .endBeforeStart: "The end date can't be before the start date."
            }
        }
    }
}

/// Body for `PATCH /api/subscriptions/{id}`. Only fields that were set are sent.
/// The double-optional fields take `.some(nil)` to clear on the server; `name`, `amount`, `cadence`, `kind`,
/// `bucket` and `active` are single-optional (nil = omitted, never sent as null) because the server rejects
/// an explicit null for them.
struct SubscriptionPatch: Encodable, Equatable {
    var name: String?
    var amount: Money?
    var cadence: Cadence?
    var kind: SubscriptionKind?
    var accountId: UUID??
    var categoryId: UUID??
    var bucket: Bucket?
    var nextDue: CalendarDate??
    var active: Bool?
    var startDate: CalendarDate??
    var endDate: CalendarDate??
    var businessPct: Int??
    var matchText: String??

    private enum CodingKeys: String, CodingKey {
        case name, amount, cadence, kind, accountId, categoryId, bucket, nextDue, active
        case startDate, endDate, businessPct, matchText
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let name { try container.encode(name, forKey: .name) }
        if let amount { try container.encode(amount, forKey: .amount) }
        if let cadence { try container.encode(cadence, forKey: .cadence) }
        if let kind { try container.encode(kind, forKey: .kind) }
        try container.encodePatchField(accountId, forKey: .accountId)
        try container.encodePatchField(categoryId, forKey: .categoryId)
        if let bucket { try container.encode(bucket, forKey: .bucket) }
        try container.encodePatchField(nextDue, forKey: .nextDue)
        if let active { try container.encode(active, forKey: .active) }
        try container.encodePatchField(startDate, forKey: .startDate)
        try container.encodePatchField(endDate, forKey: .endDate)
        try container.encodePatchField(businessPct, forKey: .businessPct)
        try container.encodePatchField(matchText, forKey: .matchText)
    }
}

extension SubscriptionPatch {
    /// The fields where `form` differs from `original`; a field the form left blank clears it. Nil when nothing
    /// changed. The form trims the name and match text, so they're compared with the stored ones trimmed: an
    /// untouched value stored with surrounding spaces isn't a change.
    init?(from original: Subscription, to form: SubscriptionForm) {
        self.init(
            name: form.name == original.name.trimmedOrNil ? nil : form.name,
            amount: form.amount == original.amount ? nil : form.amount,
            cadence: form.cadence == original.cadence ? nil : form.cadence,
            kind: form.kind == original.kind ? nil : form.kind,
            accountId: form.accountId == original.accountId ? nil : .some(form.accountId),
            categoryId: form.categoryId == original.categoryId ? nil : .some(form.categoryId),
            bucket: form.bucket == original.bucket ? nil : form.bucket,
            nextDue: form.nextDue == original.nextDue ? nil : .some(form.nextDue),
            active: form.active == original.active ? nil : form.active,
            startDate: form.startDate == original.startDate ? nil : .some(form.startDate),
            endDate: form.endDate == original.endDate ? nil : .some(form.endDate),
            businessPct: form.businessPct == original.businessPct ? nil : .some(form.businessPct),
            matchText: form.matchText == original.matchText?.trimmedOrNil ? nil : .some(form.matchText)
        )
        if isEmpty { return nil }
    }

    /// Whether no field is set, so there is nothing to send.
    var isEmpty: Bool { self == SubscriptionPatch() }
}
