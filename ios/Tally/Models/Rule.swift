import Foundation

/// What a rule's pattern is matched against; unknown server values decode to `.other`.
enum MatchField: PickableOpenEnum, Hashable {
    case description, other(String)

    nonisolated static var allKnown: [MatchField] { [.description] }
    nonisolated static func unknown(_ raw: String) -> MatchField { .other(raw) }

    /// The wire string, for requests. The only place the match field strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .description: "description"
        case .other(let raw): raw
        }
    }
}

/// How a rule's pattern is matched; unknown server values decode to `.other`.
enum MatchType: PickableOpenEnum, Hashable {
    case contains, equals, startsWith, other(String)

    nonisolated static var allKnown: [MatchType] { [.contains, .equals, .startsWith] }
    nonisolated static func unknown(_ raw: String) -> MatchType { .other(raw) }

    /// The wire string, for requests. The only place the match type strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .contains: "contains"
        case .equals: "equals"
        case .startsWith: "starts_with"
        case .other(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .contains: "Contains"
        case .equals: "Equals"
        case .startsWith: "Starts with"
        case .other(let raw): raw.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// How the rule sentence reads: "transactions containing …".
    var phrase: String {
        switch self {
        case .contains: "containing"
        case .equals: "matching"
        case .startsWith: "starting with"
        case .other(let raw): raw.replacingOccurrences(of: "_", with: " ")
        }
    }
}

/// Mirrors `RuleOut`. Rules run on new sync rows in priority order and the first match wins.
struct Rule: Decodable, Identifiable, Hashable {
    let id: UUID
    let priority: Int
    let matchField: MatchField
    let matchType: MatchType
    let pattern: String
    let setMerchant: String?
    let setCategoryId: UUID?
    let setBucket: Bucket?
    let active: Bool
    let createdAt: Date
    let updatedAt: Date

    /// This rule with the fields `patch` sets, as the server will return it.
    func applying(_ patch: RulePatch) -> Rule {
        Rule(
            id: id,
            priority: priority,
            matchField: matchField,
            matchType: patch.matchType ?? matchType,
            pattern: patch.pattern ?? pattern,
            setMerchant: setMerchant,
            setCategoryId: setCategoryId,
            setBucket: setBucket,
            active: patch.active ?? active,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }

    /// What a rule files a transaction as: its category's name and its bucket. The name is "…" before the
    /// categories have loaded, and "Deleted category" once they have and it isn't among them.
    static func targetLabels(categoryId: UUID?, bucket: Bucket?, categories: [Category]) -> [String] {
        let category = categoryId.map { categories.name(for: $0) ?? (categories.isEmpty ? "…" : "Deleted category") }
        return [category, bucket?.label].compactMap { $0 }
    }
}

extension String {
    /// The text as rules compare it: runs of whitespace collapsed to one space, trimmed and uppercased. Mirrors the
    /// server's `normalize_text` (api/app/services/normalize.py), so the phone predicts which rules overlap.
    var ruleNormalized: String {
        split(whereSeparator: \.isWhitespace).joined(separator: " ").uppercased()
    }
}

/// Where a rule saved after a correction runs among the active rules. The server runs rules by priority, then oldest
/// first, and the first match wins, so a new rule runs ahead of every broader rule that also matches: one whose
/// pattern is part of the new one's.
struct RulePlacement: Equatable {
    let priority: Int
    /// The broader rules that still run first: they are at priority 0, so the new rule ties with them and, being
    /// newer, loses. In server order.
    let stillAhead: [String]
    /// Whether the other rules were checked; when they couldn't load, the priority is the usual one.
    let checked: Bool

    /// The placement when the other rules couldn't load: the usual priority.
    static let unchecked = RulePlacement(priority: RuleDraft.correctionPriority, stillAhead: [], checked: false)

    /// The placement for a rule with `pattern` among `rules` (in server order): one ahead of the broader active
    /// rules (never below 0), or the usual priority when none overlaps.
    init(pattern: String, rules: [Rule]) {
        let normalized = pattern.ruleNormalized
        let broader = rules.filter { rule in
            let other = rule.pattern.ruleNormalized
            return rule.active && !other.isEmpty && normalized.contains(other)
        }
        let priority = broader.map(\.priority).min().map { max(0, $0 - 1) } ?? RuleDraft.correctionPriority
        let stillAhead = broader.filter { $0.priority <= priority }.map(\.pattern)
        self.init(priority: priority, stillAhead: stillAhead, checked: true)
    }

    private init(priority: Int, stillAhead: [String], checked: Bool) {
        self.priority = priority
        self.stillAhead = stillAhead
        self.checked = checked
    }

    /// What the sheet says about where the rule runs, when it may not win.
    var note: String? {
        if !checked {
            return "Couldn't check your other rules, so this one gets the usual priority and an older rule that also "
                + "matches may still win."
        }
        guard let first = stillAhead.first else { return nil }
        return "“\(first)” also matches and was saved first, so it still wins."
    }
}

/// Body for `POST /api/rules`. Unset optionals are left out. `id` is made on the phone, so retrying the same
/// draft never creates a second rule.
struct RuleDraft: Encodable, Equatable {
    /// The priority of a rule saved after a correction: after hand-curated low-number rules.
    static let correctionPriority = 100

    var id = UUID()
    var priority = RuleDraft.correctionPriority
    var matchField: MatchField = .description
    var matchType: MatchType = .contains
    var pattern: String
    var setCategoryId: UUID?
    var setBucket: Bucket?
    var active = true
}

/// Body for `PATCH /api/rules/{id}`. Only fields that were set are sent; none may be null on the server.
struct RulePatch: Encodable, Equatable {
    var active: Bool?
    var pattern: String?
    var matchType: MatchType?
}

extension RulePatch {
    /// What brings `saved` to `draft`'s pattern and match type: a create replaying a draft's id returns the rule as
    /// first saved, so an edit made before the retry isn't on it. Sets nothing when they agree.
    init(bringing saved: Rule, to draft: RuleDraft) {
        self.init(
            pattern: saved.pattern != draft.pattern ? draft.pattern : nil,
            matchType: saved.matchType != draft.matchType ? draft.matchType : nil
        )
    }
}

/// The rule offered after a transaction's category or bucket was corrected: file every transaction whose
/// description matches the pattern the way this one was filed. It sets only what the correction changed. A
/// category cleared to none isn't offered, since a rule can't clear one, and neither is a split part or a split
/// parent, whose description is the whole purchase's. `id` is minted once, so a retried Save sends the same draft.
struct RuleSuggestion: Identifiable, Equatable {
    let id: UUID
    var pattern: String
    var matchType: MatchType = .contains
    let setCategoryId: UUID?
    let setBucket: Bucket?

    /// The longest pattern the server takes, in Unicode scalars as Python counts a string.
    static let maxPatternLength = 200

    /// The rule for correcting `before` to `categoryId` and `bucket`, or nil when neither changed or `before` is
    /// part of a split.
    init?(before: Transaction, categoryId: UUID?, bucket: Bucket, id: UUID = UUID()) {
        guard before.parentId == nil, !before.isSplit else { return nil }
        let category = categoryId != before.categoryId ? categoryId : nil
        let newBucket = bucket != before.bucket ? bucket : nil
        guard category != nil || newBucket != nil else { return nil }
        self.id = id
        pattern = before.descriptionRaw.trimmingCharacters(in: .whitespacesAndNewlines)
        setCategoryId = category
        setBucket = newBucket
    }

    /// The draft to send, or the first rule the pattern breaks.
    var validated: Result<RuleDraft, Invalid> {
        guard let pattern = pattern.trimmedOrNil else { return .failure(.blank) }
        guard pattern.unicodeScalars.count <= Self.maxPatternLength else { return .failure(.tooLong) }
        return .success(RuleDraft(
            id: id, matchType: matchType, pattern: pattern, setCategoryId: setCategoryId, setBucket: setBucket
        ))
    }

    /// "Always file transactions containing “HERTZ” as Travel · Reimbursable", naming only what the rule sets.
    func sentence(categories: [Category]) -> String {
        let targets = Rule.targetLabels(categoryId: setCategoryId, bucket: setBucket, categories: categories)
        return "Always file transactions \(matchType.phrase) “\(pattern.trimmedOrNil ?? "…")” as "
            + targets.joined(separator: " · ")
    }

    enum Invalid: FormRule, Equatable {
        case blank, tooLong

        var message: String {
            switch self {
            case .blank: "Enter the text to match."
            case .tooLong: "The pattern can't be over \(RuleSuggestion.maxPatternLength) characters."
            }
        }
    }
}
