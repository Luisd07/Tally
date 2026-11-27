import SwiftUI

/// A loan's name and amount owed, planned payment, APR and when it is paid off.
struct LoanRow: View {
    let loan: LoanSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(loan.account.name)
                Spacer()
                Text(loan.balanceText).foregroundStyle(.secondary)
            }
            if !detail.isEmpty {
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            if let payoff = loan.payoffText {
                Text(payoff).font(.caption).foregroundStyle(.secondary)
            }
        }
        .foregroundStyle(.primary)
    }

    private var detail: String {
        var parts: [String] = []
        if let payment = loan.account.plannedPayment { parts.append("Pay \(payment.formatted())") }
        if let apr = loan.account.apr { parts.append("\(apr)% APR") }
        return parts.joined(separator: " · ")
    }
}
