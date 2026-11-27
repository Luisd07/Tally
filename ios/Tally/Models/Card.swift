import Foundation

/// Where a card's balance comes from; unknown server values decode to `.other`.
enum CardSource: OpenEnum, Hashable {
    case synced, manual, other(String)

    nonisolated static var known: [String: CardSource] { ["synced": .synced, "manual": .manual] }
    nonisolated static func unknown(_ raw: String) -> CardSource { .other(raw) }
}

/// Mirrors `CardOut` from `GET /api/accounts/cards`. `balance` is the latest snapshot and is negative for debt.
struct CardSummary: Decodable, Identifiable, Hashable {
    let account: Account
    let balance: Money?
    let balanceDate: CalendarDate?
    let sourceHint: CardSource
    /// Absent from servers that predate interest tracking.
    var interest: CardInterest?

    var id: UUID { account.id }

    /// What the card owes, as a positive amount; negative for a credit balance. Nil without a balance.
    var owed: Money? { balance?.negated }
}

/// Mirrors `CardInterestOut`: what a card's interest and fees cost, oldest month first. `estimate` is this
/// month's expected interest, nil without an APR or balance; `mismatch` says last month's interest didn't match
/// the APR on file.
struct CardInterest: Decodable, Hashable {
    struct Month: Decodable, Hashable, Identifiable {
        let month: MonthRef
        let amount: Money

        var id: MonthRef { month }
    }

    let months: [Month]
    let yearToDate: Money
    let estimate: Money?
    let mismatch: Bool
}

extension CardSummary {
    /// "~$25.00 interest this month"; nil when there is no estimate.
    var estimateText: String? {
        interest?.estimate.map { "~\($0.formatted()) interest this month" }
    }

    /// Shown when last month's interest didn't match the APR on file.
    static let aprMismatchNote = "Interest doesn't match the APR on file — check the APR"

    var hasAprMismatch: Bool { interest?.mismatch ?? false }
}

/// Mirrors `InterestSummaryOut` from `GET /api/accounts/interest`: `yearToDate` is interest and fees across every
/// card, closed ones too.
struct InterestSummary: Decodable, Hashable {
    let yearToDate: Money

    /// "You've paid $X in interest and fees this year"; nil when nothing was paid.
    var text: String? {
        yearToDate.amount > 0 ? "You've paid \(yearToDate.formatted()) in interest and fees this year" : nil
    }
}

/// Mirrors `LoanOut` from `GET /api/accounts/loans`: an active loan, what it owes on its latest balance (nil
/// without one) and when paying its monthly payment clears it. Nil months and interest mean it never pays off.
struct LoanSummary: Decodable, Identifiable, Hashable {
    let account: Account
    let owed: Money?
    let balanceDate: CalendarDate?
    let monthsToPayoff: Int?
    let payoffMonth: MonthRef?
    let totalInterest: Money?

    var id: UUID { account.id }

    /// The amount owed as shown, "No balance yet" without one.
    var balanceText: String { owed?.formatted() ?? "No balance yet" }

    /// "Paid off by Mar 2028", or "Never at its payment" when it doesn't clear; nil without a balance.
    var payoffText: String? {
        guard owed != nil else { return nil }
        return payoffMonth.map { "Paid off by \($0.title())" } ?? "Never at its payment"
    }

    /// This loan as a card row, so the card sheet edits it.
    var asCard: CardSummary {
        CardSummary(
            account: account, balance: owed?.negated, balanceDate: balanceDate,
            sourceHint: account.simplefinAccountId == nil ? .manual : .synced
        )
    }
}

/// Mirrors `BalanceSnapshotOut` from `PUT /api/accounts/{id}/balances/{date}`.
struct BalanceSnapshot: Decodable, Identifiable, Hashable {
    let id: UUID
    let accountId: UUID
    let date: CalendarDate
    let balance: Money
    let createdAt: Date
    let updatedAt: Date
}

extension CardSummary {
    /// This card showing `snapshot`'s balance, unless it already shows one from a later day (the newest wins).
    func recording(_ snapshot: BalanceSnapshot) -> CardSummary {
        if let balanceDate, snapshot.date < balanceDate { return self }
        return CardSummary(account: account, balance: snapshot.balance, balanceDate: snapshot.date, sourceHint: sourceHint, interest: interest)
    }
}

/// Body for `PATCH /api/accounts/{id}`. Only fields that were set are sent.
/// The double-optional fields take `.some(nil)` to clear on the server; `name` and `active` are
/// single-optional (nil = omitted, never sent as null) because the server rejects an explicit null for them.
/// `apr` is a decimal string with two decimals, as the server stores it.
struct AccountPatch: Encodable, Equatable {
    var name: String?
    var kind: AccountKind?
    var institution: String??
    var last4: String??
    var dueDay: Int??
    var plannedPayment: Money??
    var apr: String??
    var promoAprEnd: CalendarDate??
    var active: Bool?

    private enum CodingKeys: String, CodingKey {
        case name, kind, institution, last4, dueDay, plannedPayment, apr, promoAprEnd, active
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let name { try container.encode(name, forKey: .name) }
        if let kind { try container.encode(kind, forKey: .kind) }
        try container.encodePatchField(institution, forKey: .institution)
        try container.encodePatchField(last4, forKey: .last4)
        try container.encodePatchField(dueDay, forKey: .dueDay)
        try container.encodePatchField(plannedPayment, forKey: .plannedPayment)
        try container.encodePatchField(apr, forKey: .apr)
        try container.encodePatchField(promoAprEnd, forKey: .promoAprEnd)
        if let active { try container.encode(active, forKey: .active) }
    }

    /// Whether no field is set, so there is nothing to send.
    var isEmpty: Bool { self == AccountPatch() }
}

/// The card sheet's fields as entered, before validation.
struct CardFields: Equatable {
    var dueDayText = ""
    var plannedPaymentText = ""
    var aprText = ""
    /// Whether the promo end is set; `promoAprEnd` keeps the picker's day while it isn't.
    var hasPromoEnd = false
    var promoAprEnd = CalendarDate(date: .now)
    var isLoan = false
}

extension CardFields {
    /// The fields of `card`, for an edit.
    init(editing card: CardSummary) {
        let account = card.account
        self.init(
            dueDayText: account.dueDay.map(String.init) ?? "",
            plannedPaymentText: account.plannedPayment?.inputText ?? "",
            aprText: account.apr.flatMap(Percent.init(apiString:))?.inputText ?? "",
            hasPromoEnd: account.promoAprEnd != nil,
            promoAprEnd: account.promoAprEnd ?? CalendarDate(date: .now),
            isLoan: account.kind == .loan
        )
    }

    /// The validated form, or the first rule the fields break.
    var validated: Result<CardForm, CardForm.Invalid> {
        guard let dueDay = dueDayText.parsedUnlessBlank(Self.dueDay) else { return .failure(.dueDay) }
        guard let plannedPayment = plannedPaymentText.parsedUnlessBlank(Self.payment) else { return .failure(.plannedPayment) }
        guard let apr = aprText.parsedUnlessBlank({ Percent(userInput: $0) }) else { return .failure(.apr) }
        return .success(CardForm(
            dueDay: dueDay, plannedPayment: plannedPayment, apr: apr, promoAprEnd: hasPromoEnd ? promoAprEnd : nil,
            isLoan: isLoan
        ))
    }

    /// A whole number from 1 to 31.
    private static func dueDay(_ text: String) -> Int? {
        guard text.wholeMatch(of: /\d{1,2}/) != nil, let value = Int(text), (1...31).contains(value) else { return nil }
        return value
    }

    /// An amount of zero or more.
    private static func payment(_ text: String) -> Money? {
        Money(userInput: text).flatMap { $0.amount >= 0 ? $0 : nil }
    }
}

/// The card sheet's fields, validated: a due day of 1-31, a planned payment of zero or more, and an APR of
/// 0-100 with at most two decimals, each nil when blank.
struct CardForm: Equatable {
    var dueDay: Int?
    var plannedPayment: Money?
    var apr: Percent?
    var promoAprEnd: CalendarDate?
    var isLoan = false

    enum Invalid: FormRule, Equatable {
        case dueDay, plannedPayment, apr

        var message: String {
            switch self {
            case .dueDay: "Due day must be a whole number from 1 to 31."
            case .plannedPayment: "Enter a payment of zero or more, like 100.00."
            case .apr: "APR must be from 0 to 100 with at most two decimals."
            }
        }
    }
}

extension AccountPatch {
    /// The fields where `form` differs from `card`'s account; a field the form left blank clears it. Nil when
    /// nothing changed. An APR is compared as a number, so "24.9" and "24.90" are the same.
    init?(from card: CardSummary, to form: CardForm) {
        let account = card.account
        self.init(
            kind: form.isLoan == (account.kind == .loan) ? nil : (form.isLoan ? .loan : .creditCard),
            dueDay: form.dueDay == account.dueDay ? nil : .some(form.dueDay),
            plannedPayment: form.plannedPayment == account.plannedPayment ? nil : .some(form.plannedPayment),
            apr: form.apr == account.apr.flatMap(Percent.init(apiString:)) ? nil : .some(form.apr?.inputText),
            promoAprEnd: form.promoAprEnd == account.promoAprEnd ? nil : .some(form.promoAprEnd)
        )
        if isEmpty { return nil }
    }
}

/// The balance sheet's fields as entered, before validation. The amount is what the card owes, entered
/// positive; `isCredit` marks the rare balance in the cardholder's favor.
struct BalanceFields: Equatable {
    var amountText = ""
    var isCredit = false
    var date = BalanceFields.latestDate()
}

extension BalanceFields {
    /// The earliest date the server takes for a balance.
    static let earliestDate = CalendarDate(year: 2000, month: 1, day: 1)

    /// The latest date the server takes for a balance: today, but no later than today in UTC, where the server
    /// checks it. East of UTC, local today runs a day ahead in the hours after midnight.
    static func latestDate(now: Date = .now, calendar: Calendar = .current) -> CalendarDate {
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = .gmt
        return min(CalendarDate(date: now, calendar: calendar), CalendarDate(date: now, calendar: utc))
    }

    /// The validated entry, or the first rule the fields break. `today` is the latest date allowed.
    func validated(today: CalendarDate = BalanceFields.latestDate()) -> Result<BalanceEntry, BalanceEntry.Invalid> {
        guard let owed = Money(userInput: amountText), owed.amount >= 0 else { return .failure(.amount) }
        guard (Self.earliestDate...today).contains(date) else { return .failure(.date) }
        return .success(BalanceEntry(owed: owed, isCredit: isCredit, date: date))
    }
}

/// A month-end balance to record: what the card owes (positive), whether that is a credit, and the day.
struct BalanceEntry: Equatable {
    var owed: Money
    var isCredit = false
    var date: CalendarDate

    /// What the server stores: negative for debt, positive for a credit.
    var balance: Money { isCredit ? owed : owed.negated }

    enum Invalid: FormRule, Equatable {
        case amount, date

        var message: String {
            switch self {
            case .amount: "Enter the amount owed, like 250.00."
            case .date: "Pick a date from 2000-01-01 through today."
            }
        }
    }
}

/// Body for `PUT /api/accounts/{id}/balances/{date}`.
struct BalanceUpsert: Encodable {
    var balance: Money
}

extension CardSummary {
    /// The amount owed as shown: "No balance yet", "Credit $X" for a balance in the cardholder's favor, else the debt.
    var balanceText: String {
        guard let owed else { return "No balance yet" }
        return owed.amount < 0 ? "Credit \(owed.negated.formatted())" : owed.formatted()
    }

    /// The next payment due date from the due day, or nil without one.
    func nextDue(today: CalendarDate = CalendarDate(date: .now)) -> CalendarDate? {
        account.dueDay.map { CalendarDate.nextOccurrence(ofDay: $0, onOrAfter: today) }
    }

    /// When the promo APR ends and how long is left, "Promo ended" once past, or nil without a promo end.
    func promoText(today: CalendarDate = CalendarDate(date: .now), calendar: Calendar = .current) -> String? {
        guard let end = account.promoAprEnd else { return nil }
        guard let days = today.days(until: end, calendar: calendar), days >= 0 else { return "Promo ended" }
        let left = days == 0 ? "ends today" : days == 1 ? "1 day left" : "\(days) days left"
        return "Promo ends \(end.formatted()) · \(left)"
    }
}
