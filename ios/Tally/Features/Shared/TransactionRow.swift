import SwiftUI

extension Array where Element == Category {
    /// The name of the category with `id`, or nil when `id` is nil or isn't in this list.
    func name(for id: UUID?) -> String? {
        guard let id else { return nil }
        return first { $0.id == id }?.name
    }

    /// The category shown for `transaction`: "Uncategorized" only when it has no category, nil when its
    /// category isn't in this list (not loaded yet).
    func label(for transaction: Transaction) -> String? {
        transaction.categoryId == nil ? "Uncategorized" : name(for: transaction.categoryId)
    }
}

/// One transaction as a list row. A category that isn't in `categories` (not loaded yet) shows no
/// name rather than "Uncategorized", which is reserved for a transaction with no category.
struct TransactionRow: View {
    let transaction: Transaction
    let categories: [Category]
    var statuses: [TransactionStatus] = []
    /// Inbox suggestion text.
    var suggestion: String?
    /// Shows the sparkle for a suggestion Claude made (#169) instead of the history wand.
    var suggestionIsAI = false

    @Environment(\.theme) private var theme
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(spacing: 12) {
            if theme.showsGlyphs {
                CategoryGlyph(category: category)
            }
            VStack(alignment: .leading, spacing: theme.isDense ? 1 : 3) {
                topLine
                Text(details)
                    .font(theme.captionFont)
                    .foregroundStyle(.secondary)
                if !statuses.isEmpty {
                    HStack(spacing: 10) {
                        ForEach(statuses, id: \.self) { StatusBadge($0) }
                    }
                }
                if let suggestion {
                    HStack(spacing: 4) {
                        Image(systemName: suggestionIsAI ? "sparkles" : "wand.and.stars")
                        Text(suggestion)
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, theme.rowPadding)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    /// The name and amount side by side, or stacked at accessibility text sizes so the amount isn't squeezed.
    private var topLine: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading))
            : AnyLayout(HStackLayout(alignment: .firstTextBaseline))
        return layout {
            Text(transaction.displayName)
                .lineLimit(2)
            Spacer(minLength: 8)
            AmountText(transaction.amount, role: transaction.amountRole)
        }
    }

    private var category: String? { categories.label(for: transaction) }

    private var details: String {
        [transaction.postedDate.formatted(), category, transaction.bucket.label]
            .compactMap { $0 }
            .joined(separator: " · ")
    }

    private var accessibilityText: String {
        let amount = AmountText.Presentation(transaction.amount, role: transaction.amountRole).text
        return ([transaction.displayName, amount, details] + statuses.map(\.label) + [suggestion].compactMap { $0 })
            .joined(separator: ", ")
    }
}
