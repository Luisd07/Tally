import Foundation

/// A split being filled in. Parts are entered as positive magnitudes and sent with the parent's sign. The last
/// part shows whatever the others leave of the amount until the user types in it.
struct SplitDraft: Equatable {
    struct Part: Identifiable, Equatable {
        let id = UUID()
        var amountText: String
        var bucket: Bucket
        var categoryId: UUID?
        var hasTypedAmount = false
    }

    /// A rule the draft breaks, with the reason shown beside Save.
    enum Invalid: FormRule, Equatable {
        case tooFewParts, tooManyParts, zeroPart
        /// The parts add up to more than the original, by this much.
        case overAssigned(Money)
        /// The parts add up to less than the original, by this much.
        case unassigned(Money)

        var message: String {
            switch self {
            case .tooFewParts: "A split needs at least \(SplitDraft.minParts) parts."
            case .tooManyParts: "A split has at most \(SplitDraft.maxParts) parts."
            case .zeroPart: "Every part needs an amount above zero."
            case .overAssigned(let excess): "The parts exceed the original by \(excess.formatted())."
            case .unassigned(let left): "\(left.formatted()) left to assign."
            }
        }
    }

    static let minParts = 2
    static let maxParts = 20

    /// The parent's amount without its sign.
    let magnitude: Money
    /// Whether the parent is an outflow, so the parts are sent negative.
    private let isOutflow: Bool
    /// The bucket an added part starts in.
    private let addedBucket: Bucket
    private(set) var entries: [Part]

    /// A business or personal parent starts as Business and Personal; any other starts with both parts in its
    /// bucket. The second part takes the whole amount.
    init(parent: Transaction) {
        magnitude = parent.amount.amount < 0 ? parent.amount.negated : parent.amount
        isOutflow = parent.amount.amount < 0
        let buckets: [Bucket] = switch parent.bucket {
        case .personal, .business: [.business, .personal]
        default: [parent.bucket, parent.bucket]
        }
        addedBucket = buckets[1]
        entries = buckets.map { Part(amountText: "", bucket: $0) }
    }

    /// Whether a part can be added: there are fewer than the most, and some of the amount isn't typed into a part.
    var canAddPart: Bool { entries.count < Self.maxParts && unassigned.amount > 0 }
    var canRemovePart: Bool { entries.count > Self.minParts }

    /// What the parts leave of the original amount; negative when they exceed it.
    var remaining: Money {
        magnitude - entries.reduce(Money(0)) { $0 + (amount(of: $1) ?? Money(0)) }
    }

    var isBalanced: Bool { remaining.amount == 0 }

    /// The request parts, in order and each with the parent's sign, or the first rule the draft breaks.
    func validated() -> Result<[SplitPart], Invalid> {
        guard entries.count >= Self.minParts else { return .failure(.tooFewParts) }
        guard entries.count <= Self.maxParts else { return .failure(.tooManyParts) }
        guard remaining.amount >= 0 else { return .failure(.overAssigned(remaining.negated)) }
        guard entries.allSatisfy(isValid) else { return .failure(.zeroPart) }
        guard isBalanced else { return .failure(.unassigned(remaining)) }
        return .success(entries.compactMap { part in
            amount(of: part).map {
                SplitPart(amount: isOutflow ? $0.negated : $0, categoryId: part.categoryId, bucket: part.bucket)
            }
        })
    }

    /// The part's amount: the remainder for the automatic last part (nil when nothing is left), otherwise what
    /// was typed (nil when it isn't an amount).
    func amount(of part: Part) -> Money? {
        guard isAutomatic(part) else { return Money(userInput: part.amountText) }
        return unassigned.amount > 0 ? unassigned : nil
    }

    func isValid(_ part: Part) -> Bool {
        (amount(of: part)?.amount ?? 0) > 0
    }

    /// The text for the part's amount field.
    func amountText(of part: Part) -> String {
        isAutomatic(part) ? amount(of: part)?.inputText ?? "" : part.amountText
    }

    mutating func setAmountText(_ text: String, for id: UUID) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        entries[index].amountText = text
        entries[index].hasTypedAmount = true
    }

    mutating func setBucket(_ bucket: Bucket, for id: UUID) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        entries[index].bucket = bucket
    }

    /// `nil` leaves the category as the original's.
    mutating func setCategory(_ categoryId: UUID?, for id: UUID) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        entries[index].categoryId = categoryId
    }

    /// Adds a part that takes the remainder. The part that did so before keeps the amount it showed, until it is
    /// last again.
    mutating func addPart() {
        guard canAddPart else { return }
        if let last = entries.last, isAutomatic(last) {
            entries[entries.count - 1].amountText = amountText(of: last)
        }
        entries.append(Part(amountText: "", bucket: addedBucket))
    }

    /// Removes the parts at `offsets`, never going below two. A part left last takes the remainder unless the user
    /// typed its amount.
    mutating func removeParts(at offsets: IndexSet) {
        for index in offsets.sorted(by: >) where canRemovePart {
            entries.remove(at: index)
        }
    }

    /// What the parts other than an automatic last one leave of the amount; negative when they exceed it.
    private var unassigned: Money {
        let typed = entries.filter { !isAutomatic($0) }.map { Money(userInput: $0.amountText) ?? Money(0) }
        return magnitude - typed.reduce(Money(0), +)
    }

    private func isAutomatic(_ part: Part) -> Bool {
        part.id == entries.last?.id && !part.hasTypedAmount
    }
}
