import SwiftUI

/// One report: its expenses, and the next step for it (submit a draft, mark a submitted report paid). A failed
/// Remove shows as the Reimbursements screen's alert.
struct ReimbursementDetailView: View {
    let model: ReimbursementsModel
    let reference: ReferenceData
    let reportId: UUID

    @Environment(APIClient.self) private var api
    @State private var expenses = OneShotLoad<[Transaction]>()
    @State private var sheet: ReportSheet?

    private var report: Reimbursement? { model.report(id: reportId) }

    var body: some View {
        Group {
            if let report {
                list(report)
            } else {
                ContentUnavailableView("Report not found", systemImage: "doc")
            }
        }
        .navigationTitle(report?.displayName ?? "Report")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: report?.transactionCount) { await loadExpenses() }
        .refreshable {
            await model.load(api: api)
            await loadExpenses()
        }
        .sheet(item: $sheet) { sheet in
            switch sheet.step {
            case .submit: SubmitReportSheet(model: model, report: sheet.report)
            case .markPaid: MarkPaidSheet(model: model, report: sheet.report)
            }
        }
    }

    private func list(_ report: Reimbursement) -> some View {
        List {
            Section {
                ReimbursementRow(report: report, settledBy: model.settledBy(report))
                if let step = report.status.step {
                    Button(step.title) { sheet = ReportSheet(step: step, report: report) }
                }
            }
            Section("Expenses") {
                if let error = expenses.error {
                    RetrySection(title: "Couldn't load expenses", error: error) { Task { await loadExpenses() } }
                } else if expenses.value == nil {
                    ProgressView().frame(maxWidth: .infinity)
                } else if expenses.value?.isEmpty == true {
                    Text("No expenses").foregroundStyle(.secondary)
                }
                ForEach(expenses.value ?? []) { expense in expenseRow(expense, in: report) }
            }
        }
    }

    private func expenseRow(_ expense: Transaction, in report: Reimbursement) -> some View {
        TransactionRow(transaction: expense, categories: reference.categories)
            .swipeActions(edge: .trailing) {
                if report.isDraft {
                    Button("Remove", role: .destructive) { remove(expense, from: report) }
                }
            }
    }

    private func remove(_ expense: Transaction, from report: Reimbursement) {
        Task {
            if await model.remove([expense.id], from: report, api: api) {
                expenses.amend { $0.filter { $0.id != expense.id } }
            }
        }
    }

    private func loadExpenses() async {
        guard let report else { return }
        await expenses.load { try await model.expenses(of: report, api: api) }
    }
}

/// The sheet for a report's next step.
private struct ReportSheet: Identifiable {
    let step: ReimbursementStep
    let report: Reimbursement

    var id: String { "\(step)-\(report.id.uuidString)" }
}
