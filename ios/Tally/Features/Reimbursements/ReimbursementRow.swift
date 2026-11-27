import SwiftUI

/// A report's name and total, its expense count, status badge and dates.
struct ReimbursementRow: View {
    let report: Reimbursement
    /// The income entry that settled the report, once loaded.
    var settledBy: IncomeEntry?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(report.displayName).font(.headline)
                Spacer()
                Text(report.total.formatted()).monospacedDigit()
            }
            HStack(spacing: 6) {
                ReimbursementStatusBadge(status: report.status)
                Text(detail)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if let settledBy {
                Text(paidBy(settledBy))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .foregroundStyle(.primary)
    }

    private var detail: String {
        var parts = [report.transactionCount == 1 ? "1 expense" : "\(report.transactionCount) expenses"]
        if let date = report.submittedDate { parts.append("Submitted \(date.formatted())") }
        if let date = report.paidDate { parts.append("Paid \(date.formatted())") }
        return parts.joined(separator: " · ")
    }

    /// The settling entry's name and amount: what was received, or else what was expected.
    private func paidBy(_ entry: IncomeEntry) -> String {
        let amount = (entry.received ?? entry.expected)?.formatted() ?? ""
        return "Paid by \(entry.displayName) \(amount)"
    }
}

struct ReimbursementStatusBadge: View {
    let status: ReimbursementStatus

    var body: some View {
        Text(status.label)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(status.color.opacity(0.2), in: Capsule())
            .foregroundStyle(status.color)
    }
}

extension ReimbursementStatus {
    /// The badge color for this status.
    var color: Color {
        switch self {
        case .draft: .gray
        case .submitted: .blue
        case .paid: .green
        case .unrecognized: .orange
        }
    }
}
