import SwiftUI

/// A row's name and cost per charge, its next charge date, account, inactive mark and flag badges.
struct SubscriptionRow: View {
    let row: Subscription
    let accountName: String?
    let categoryName: String?

    @Environment(\.theme) private var theme
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        HStack(spacing: 12) {
            if theme.showsGlyphs {
                if row.categoryId == nil {
                    CategoryGlyph(systemImage: row.kind == .bill ? "doc.text" : "arrow.triangle.2.circlepath")
                } else {
                    CategoryGlyph(category: categoryName)
                }
            }
            VStack(alignment: .leading, spacing: theme.isDense ? 1 : 3) {
                topLine
                Text(detail)
                    .font(theme.captionFont)
                    .foregroundStyle(.secondary)
                badges
            }
        }
        .padding(.vertical, theme.rowPadding)
    }

    /// The name and cost side by side, or stacked at accessibility text sizes.
    private var topLine: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading))
            : AnyLayout(HStackLayout(alignment: .firstTextBaseline))
        return layout {
            Text(row.name).lineLimit(2)
            Spacer(minLength: 8)
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                AmountText(row.cost)
                Text(row.cadence.shortLabel)
                    .font(theme.captionFont)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var detail: String {
        var parts = [row.nextDue.map { "Next \($0.formatted())" } ?? "No date"]
        if let accountName { parts.append(accountName) }
        if !row.active { parts.append("Inactive") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var badges: some View {
        let flags = row.flags
        if flags.priceChange != nil || flags.missed || flags.dormant {
            HStack(spacing: 10) {
                if let change = flags.priceChange {
                    let old = AmountText.Presentation(change.old, role: .amount).text
                    let new = AmountText.Presentation(change.new, role: .amount).text
                    // Amounts are negative: a lower one costs more.
                    let symbol = change.new < change.old ? "arrow.up.right" : "arrow.down.right"
                    StatusBadge("\(old) → \(new)", systemImage: symbol, tone: .attention)
                }
                if flags.missed { StatusBadge("Missed", systemImage: "exclamationmark.circle", tone: .attention) }
                if flags.dormant { StatusBadge("Dormant", systemImage: "moon.zzz") }
            }
        }
    }
}

private extension Cadence {
    /// "/ mo" after a price.
    var shortLabel: String {
        switch self {
        case .weekly: "/ wk"
        case .monthly: "/ mo"
        case .quarterly: "/ qtr"
        case .yearly: "/ yr"
        case .unrecognized(let raw): "/ \(raw)"
        }
    }
}
