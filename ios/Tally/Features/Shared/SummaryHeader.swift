import SwiftUI

/// The hero number at the top of a screen: a title, the amount in the theme's hero font, a caption and an
/// optional accessory (a chart, a breakdown) under it. The screen decides whether it sits in a card.
struct SummaryHeader<Caption: View, Accessory: View>: View {
    let title: String
    let amount: Money
    var role: AmountText.Role = .amount
    @ViewBuilder let caption: () -> Caption
    @ViewBuilder let accessory: () -> Accessory

    @Environment(\.theme) private var theme

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            AmountText(amount, role: role)
                .font(theme.heroFont)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            caption()
                .font(.subheadline)
                .foregroundStyle(.secondary)
            accessory()
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, theme.usesCards ? 4 : 0)
    }
}

extension SummaryHeader where Caption == Text, Accessory == EmptyView {
    init(_ title: String, amount: Money, role: AmountText.Role = .amount, caption: String) {
        self.init(title: title, amount: amount, role: role) { Text(caption) } accessory: { EmptyView() }
    }
}

extension SummaryHeader {
    init(
        _ title: String,
        amount: Money,
        role: AmountText.Role = .amount,
        @ViewBuilder caption: @escaping () -> Caption,
        @ViewBuilder accessory: @escaping () -> Accessory
    ) {
        self.init(title: title, amount: amount, role: role, caption: caption, accessory: accessory)
    }
}
