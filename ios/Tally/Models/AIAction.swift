import Foundation

/// Where an AI action is in its life; unknown server values decode to `.other`.
enum AIActionStatus: EncodableOpenEnum, Hashable {
    case proposed, applied, dismissed, undone, failed, other(String)

    nonisolated static var known: [String: AIActionStatus] {
        ["proposed": .proposed, "applied": .applied, "dismissed": .dismissed, "undone": .undone, "failed": .failed]
    }
    nonisolated static func unknown(_ raw: String) -> AIActionStatus { .other(raw) }

    nonisolated var rawValue: String {
        switch self {
        case .proposed: "proposed"
        case .applied: "applied"
        case .dismissed: "dismissed"
        case .undone: "undone"
        case .failed: "failed"
        case .other(let raw): raw
        }
    }
}

/// Where an AI action was proposed; unknown server values decode to `.other`.
enum AIActionSource: OpenEnum, Hashable {
    case chat, insight, other(String)

    nonisolated static var known: [String: AIActionSource] { ["chat": .chat, "insight": .insight] }
    nonisolated static func unknown(_ raw: String) -> AIActionSource { .other(raw) }
}

/// What one step of an action does; unknown server values decode to `.other`.
enum AIActionStepKind: OpenEnum, Hashable {
    case categorizeTransactions, markReviewed, createCategory, updateCategory, createRule, createSubscription
    case updateSubscription
    case other(String)

    nonisolated static var known: [String: AIActionStepKind] {
        [
            "categorize_transactions": .categorizeTransactions,
            "mark_reviewed": .markReviewed,
            "create_category": .createCategory,
            "update_category": .updateCategory,
            "create_rule": .createRule,
            "create_subscription": .createSubscription,
            "update_subscription": .updateSubscription,
        ]
    }
    nonisolated static func unknown(_ raw: String) -> AIActionStepKind { .other(raw) }
}

/// One step of an action, reduced to what a screen shows: its kind, how many transactions it touches, and the
/// name it creates or changes (a category or subscription name, or a rule's pattern) when it has one.
struct AIActionStep: Decodable, Hashable {
    let kind: AIActionStepKind
    let transactionCount: Int
    let name: String?
    /// The category a categorize step assigns, for the screen to resolve to a name.
    let categoryId: UUID?
    /// The row an update step changes.
    let id: UUID?
    /// The id a create_category step gives its new category, for later steps to resolve to a name.
    let createdCategoryId: UUID?
    /// The fields an update step's patch sets, as camelCase keys ("monthlyBudget").
    let patchFields: Set<String>
    /// The budget an update_category patch sets; nil with "monthlyBudget" in `patchFields` clears it.
    let monthlyBudget: Money?
    let archived: Bool?

    private enum CodingKeys: String, CodingKey {
        case kind, ids, category, rule, subscription, patch, id
    }

    private struct Patch: Decodable {
        let categoryId: UUID?
        let monthlyBudget: Money?
        let archived: Bool?
    }

    private struct AnyKey: CodingKey {
        let stringValue: String
        var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    private struct Identified: Decodable {
        let id: UUID?
    }

    private struct Named: Decodable {
        let name: String?
        let pattern: String?
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        kind = try container.decode(AIActionStepKind.self, forKey: .kind)
        transactionCount = try container.decodeIfPresent([UUID].self, forKey: .ids)?.count ?? 0
        let named = try [CodingKeys.category, .rule, .subscription, .patch].lazy
            .compactMap { try container.decodeIfPresent(Named.self, forKey: $0) }
            .first
        name = named?.name ?? named?.pattern
        let patch = try container.decodeIfPresent(Patch.self, forKey: .patch)
        categoryId = patch?.categoryId
        monthlyBudget = patch?.monthlyBudget
        archived = patch?.archived
        id = try container.decodeIfPresent(UUID.self, forKey: .id)
        createdCategoryId = try container.decodeIfPresent(Identified.self, forKey: .category)?.id
        patchFields = patch == nil
            ? []
            : Set(try container.nestedContainer(keyedBy: AnyKey.self, forKey: .patch).allKeys.map(\.stringValue))
    }

    /// Whether this is an update_category step that changes nothing but the monthly budget.
    var changesOnlyBudget: Bool {
        kind == .updateCategory && patchFields == ["monthlyBudget"]
    }
}

/// What an action really changes, counted by the server from its steps.
struct AIActionEffects: Decodable, Hashable {
    let transactions: Int
    let categoriesCreated: Int
    var categoriesUpdated = 0
    let rulesCreated: Int
    let subscriptionsCreated: Int
    let subscriptionsUpdated: Int

    /// What the action does, one line per kind of change that is not zero, e.g. "Updates 3 transactions".
    /// `budgetsOnly` says every category update changes only its budget, so those read as budget changes.
    nonisolated func lines(budgetsOnly: Bool = false) -> [String] {
        var lines: [String] = []
        if transactions > 0 { lines.append("Updates \(transactions) \(transactions == 1 ? "transaction" : "transactions")") }
        if categoriesCreated > 0 { lines.append("Creates \(categoriesCreated) \(categoriesCreated == 1 ? "category" : "categories")") }
        if categoriesUpdated > 0 {
            lines.append(budgetsOnly
                ? "Changes \(categoriesUpdated) \(categoriesUpdated == 1 ? "budget" : "budgets")"
                : "Updates \(categoriesUpdated) \(categoriesUpdated == 1 ? "category" : "categories")")
        }
        if rulesCreated > 0 { lines.append("Creates \(rulesCreated) \(rulesCreated == 1 ? "rule" : "rules")") }
        if subscriptionsCreated > 0 {
            lines.append("Adds \(subscriptionsCreated) \(subscriptionsCreated == 1 ? "subscription" : "subscriptions")")
        }
        if subscriptionsUpdated > 0 {
            lines.append("Changes \(subscriptionsUpdated) \(subscriptionsUpdated == 1 ? "subscription" : "subscriptions")")
        }
        return lines
    }
}

extension AIActionEffects {
    private enum CodingKeys: String, CodingKey {
        case transactions, categoriesCreated, categoriesUpdated, rulesCreated, subscriptionsCreated, subscriptionsUpdated
    }

    /// `categories_updated` is newer than the rest, so a server without it still decodes.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            transactions: try container.decode(Int.self, forKey: .transactions),
            categoriesCreated: try container.decode(Int.self, forKey: .categoriesCreated),
            categoriesUpdated: try container.decodeIfPresent(Int.self, forKey: .categoriesUpdated) ?? 0,
            rulesCreated: try container.decode(Int.self, forKey: .rulesCreated),
            subscriptionsCreated: try container.decode(Int.self, forKey: .subscriptionsCreated),
            subscriptionsUpdated: try container.decode(Int.self, forKey: .subscriptionsUpdated)
        )
    }
}

/// Mirrors `AIActionOut`: a change Claude proposed, which nothing applies until the user confirms it.
struct AIAction: Decodable, Identifiable, Hashable {
    let id: UUID
    let summary: String
    let status: AIActionStatus
    let source: AIActionSource
    let steps: [AIActionStep]
    let effects: AIActionEffects
    let createdAt: Date
    let appliedAt: Date?
    let error: String?

    /// The effects as lines, with category updates read as budget changes when that's all they change.
    var lines: [String] {
        effects.lines(budgetsOnly: steps.filter { $0.kind == .updateCategory }.allSatisfy(\.changesOnlyBudget))
    }
}
