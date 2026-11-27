import SwiftUI

struct ReimbursementsView: View {
    @Environment(APIClient.self) private var api
    @State private var model = ReimbursementsModel()
    @State private var reference = ReferenceData()
    @State private var selected: Set<UUID> = []
    @State private var showNewReport = false

    var body: some View {
        content
            .navigationTitle("Reimbursements")
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .sheet(isPresented: $showNewReport) {
                NewReportSheet(model: model, transactionIds: selectedIds) { selected = [] }
            }
            // The one alert for this stack: the pushed report detail has none of its own.
            .editFailureAlert(model.editError, clear: model.clearEditError)
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Reimbursements", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            Section {
                LabeledContent("Owed to you", value: model.owed.formatted())
            }
            unsubmittedSection
            reportsSection("Open", reports: model.openReports, empty: "No open reports")
            reportsSection("Paid", reports: model.paidReports, empty: "No paid reports")
        }
    }

    private var selectedIds: [UUID] {
        model.unsubmitted.map(\.id).filter(selected.contains)
    }

    private var unsubmittedSection: some View {
        Section("Unsubmitted") {
            if model.unsubmitted.isEmpty {
                Text("Nothing waiting to be reported").foregroundStyle(.secondary)
            }
            ForEach(model.unsubmitted) { transaction in selectableRow(transaction) }
            if !selectedIds.isEmpty { selectionActions }
        }
    }

    private func selectableRow(_ transaction: Transaction) -> some View {
        Button {
            if !selected.insert(transaction.id).inserted { selected.remove(transaction.id) }
        } label: {
            HStack {
                Image(systemName: selected.contains(transaction.id) ? "checkmark.circle.fill" : "circle")
                TransactionRow(transaction: transaction, categories: reference.categories)
            }
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var selectionActions: some View {
        let ids = selectedIds
        Button("New report from \(ids.count) selected") { showNewReport = true }
        ForEach(model.reportsAvailableToAdd) { draft in
            Button("Add \(ids.count) to \(draft.displayName)") {
                Task {
                    if await model.add(ids, to: draft, api: api) { selected = [] }
                }
            }
        }
    }

    private func reportsSection(_ title: String, reports: [Reimbursement], empty: String) -> some View {
        Section(title) {
            if reports.isEmpty {
                Text(empty).foregroundStyle(.secondary)
            }
            ForEach(reports) { report in
                NavigationLink {
                    ReimbursementDetailView(model: model, reference: reference, reportId: report.id)
                } label: {
                    ReimbursementRow(report: report, settledBy: model.settledBy(report))
                }
            }
        }
    }

    private func load() async {
        async let named: Void = reference.load(api: api)
        async let reports: Void = model.load(api: api)
        _ = await (named, reports)
    }
}
