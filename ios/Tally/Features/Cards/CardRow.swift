import SwiftUI

/// A card's name and amount owed, balance date, source badge, next due date, planned payment, APR and promo end.
struct CardRow: View {
    let card: CardSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                Spacer()
                Text(card.balanceText).foregroundStyle(.secondary)
            }
            Text(balanceDetail).font(.caption).foregroundStyle(.secondary)
            Text(dueDetail).font(.caption).foregroundStyle(.secondary)
            if let promo = card.promoText() {
                Text(promo).font(.caption).foregroundStyle(.secondary)
            }
            if let estimate = card.estimateText {
                Text(estimate).font(.caption).foregroundStyle(.secondary)
            }
            if card.hasAprMismatch {
                Text(CardSummary.aprMismatchNote).font(.caption).foregroundStyle(.orange)
            }
            badge
        }
        .foregroundStyle(.primary)
    }

    private var title: String {
        let account = card.account
        return account.last4.map { "\(account.name) •• \($0)" } ?? account.name
    }

    private var balanceDetail: String {
        var parts = [card.balanceDate.map { "as of \($0.formatted())" } ?? "No balance date"]
        if !card.account.active { parts.append("Inactive") }
        return parts.joined(separator: " · ")
    }

    private var dueDetail: String {
        let account = card.account
        var parts = [card.nextDue().map { "Due \($0.formatted())" } ?? "No due day"]
        if let payment = account.plannedPayment { parts.append("Pay \(payment.formatted())") }
        if let apr = account.apr { parts.append("\(apr)% APR") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var badge: some View {
        switch card.sourceHint {
        case .synced: badge("Synced", .green)
        case .manual: badge("Manual", .orange)
        case .other(let raw): badge(raw.capitalized, .gray)
        }
    }

    private func badge(_ text: String, _ color: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(color.opacity(0.2), in: Capsule())
            .foregroundStyle(color)
    }
}
