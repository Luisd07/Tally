import SwiftUI

/// Claude's review of last week (#173): a short summary and the things worth a look, each opening its transaction.
struct WeeklyReviewSection: View {
    let review: WeeklyReview
    /// Called after an edit to a flagged transaction saved, so the Inbox badge can catch up.
    var onEdited: (() -> Void)?

    var body: some View {
        Section {
            Label {
                Text(review.summary)
            } icon: {
                Image(systemName: "sparkles")
            }
            ForEach(Array(review.findings.enumerated()), id: \.offset) { _, finding in
                if let transaction = finding.transaction {
                    NavigationLink {
                        FindingTransactionView(transaction: transaction, onEdited: onEdited)
                    } label: {
                        row(finding)
                    }
                } else {
                    row(finding)
                }
            }
        } header: {
            Text("Last week")
        }
    }

    private func row(_ finding: WeeklyReview.Finding) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Label(finding.kind.label, systemImage: finding.kind.systemImage)
                .font(.subheadline.weight(.semibold))
            Text(finding.note).font(.subheadline)
            if let transaction = finding.transaction {
                Text("\(transaction.displayName) · \(transaction.amount.formatted())")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// A flagged transaction's full detail. It loads the transaction's month, narrowed to its description, so the
/// usual detail screen (category, merchant, receipts) works on it.
struct FindingTransactionView: View {
    let transaction: WeeklyReview.FindingTransaction

    @Environment(APIClient.self) private var api
    @State private var model: TransactionsModel
    @State private var reference = ReferenceData()

    init(transaction: WeeklyReview.FindingTransaction, onEdited: (() -> Void)?) {
        self.transaction = transaction
        _model = State(initialValue: TransactionsModel(onEdited: onEdited))
    }

    var body: some View {
        Group {
            if model.hasLoaded {
                TransactionDetailView(
                    id: transaction.id, model: model, categories: reference.categories, accounts: reference.accounts
                )
            } else {
                ProgressView()
            }
        }
        .task {
            model.filter.month = MonthRef(containing: transaction.postedDate)
            model.filter.search = transaction.descriptionRaw
            await model.load(api: api)
        }
        .task { await reference.load(api: api) }
        .editFailureAlert(model.editError, clear: model.clearEditError)
    }
}
