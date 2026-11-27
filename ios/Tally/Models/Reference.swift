import Foundation

/// Kind of category; unknown server values decode to `.other`.
enum CategoryKind: PickableOpenEnum, Hashable {
    case expense, income, other(String)

    nonisolated static var allKnown: [CategoryKind] { [.expense, .income] }
    nonisolated static func unknown(_ raw: String) -> CategoryKind { .other(raw) }

    nonisolated var rawValue: String {
        switch self {
        case .expense: "expense"
        case .income: "income"
        case .other(let raw): raw
        }
    }
}

/// Kind of account; unknown server values decode to `.other`.
enum AccountKind: EncodableOpenEnum, Hashable {
    case checking, creditCard, cash, loan, other(String)

    nonisolated static var known: [String: AccountKind] {
        ["checking": .checking, "credit_card": .creditCard, "cash": .cash, "loan": .loan]
    }
    nonisolated static func unknown(_ raw: String) -> AccountKind { .other(raw) }

    nonisolated var rawValue: String {
        switch self {
        case .checking: "checking"
        case .creditCard: "credit_card"
        case .cash: "cash"
        case .loan: "loan"
        case .other(let raw): raw
        }
    }
}

/// Mirrors `CategoryOut` from `GET /api/categories`. `archived` is absent from an older server's reply.
struct Category: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let kind: CategoryKind
    let taxLine: String?
    let deductiblePct: Int
    let monthlyBudget: Money?
    let archived: Bool
    let createdAt: Date
    let updatedAt: Date

    private enum CodingKeys: String, CodingKey {
        case id, name, kind, taxLine, deductiblePct, monthlyBudget, archived, createdAt, updatedAt
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        kind = try container.decode(CategoryKind.self, forKey: .kind)
        taxLine = try container.decodeIfPresent(String.self, forKey: .taxLine)
        deductiblePct = try container.decode(Int.self, forKey: .deductiblePct)
        monthlyBudget = try container.decodeIfPresent(Money.self, forKey: .monthlyBudget)
        archived = try container.decodeIfPresent(Bool.self, forKey: .archived) ?? false
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        updatedAt = try container.decode(Date.self, forKey: .updatedAt)
    }
}

/// Body for `POST /api/categories`. The phone makes the id, so a retry can't create a second category.
struct CategoryDraft: Encodable, Equatable {
    var id = UUID()
    var name: String
    var kind: CategoryKind
    var monthlyBudget: Money?
}

/// Body for `PATCH /api/categories/{id}`. Only fields that were set are sent; a budget set to `.some(nil)` clears it.
struct CategoryPatch: Encodable, Equatable {
    var name: String?
    var kind: CategoryKind?
    var monthlyBudget: Money??
    var archived: Bool?

    private enum CodingKeys: String, CodingKey {
        case name, kind, monthlyBudget, archived
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let name { try container.encode(name, forKey: .name) }
        if let kind { try container.encode(kind, forKey: .kind) }
        try container.encodePatchField(monthlyBudget, forKey: .monthlyBudget)
        if let archived { try container.encode(archived, forKey: .archived) }
    }
}

extension [Category] {
    /// The categories a picker offers: archived ones are kept for old transactions but not chosen again.
    var pickable: [Category] { filter { !$0.archived } }

    /// `pickable`, plus the category already selected even when archived, so a picker never loses its selection.
    func pickable(keeping selectedId: UUID?) -> [Category] { filter { !$0.archived || $0.id == selectedId } }
}

/// Mirrors `AccountOut` from `GET /api/accounts`. `apr` is the server's decimal string, kept as text.
struct Account: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let kind: AccountKind
    let institution: String?
    let simplefinAccountId: String?
    let last4: String?
    let dueDay: Int?
    let plannedPayment: Money?
    let apr: String?
    let promoAprEnd: CalendarDate?
    let active: Bool
    let createdAt: Date
    let updatedAt: Date
}

extension Account {
    /// This account with the fields `patch` sets, as the server will return it.
    func applying(_ patch: AccountPatch) -> Account {
        Account(
            id: id,
            name: patch.name ?? name,
            kind: patch.kind ?? kind,
            institution: patch.institution ?? institution,
            simplefinAccountId: simplefinAccountId,
            last4: patch.last4 ?? last4,
            dueDay: patch.dueDay ?? dueDay,
            plannedPayment: patch.plannedPayment ?? plannedPayment,
            apr: patch.apr ?? apr,
            promoAprEnd: patch.promoAprEnd ?? promoAprEnd,
            active: patch.active ?? active,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }
}
