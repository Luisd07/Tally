import SwiftUI

struct SubscriptionsView: View {
    @Environment(APIClient.self) private var api
    @State private var model = SubscriptionsModel()
    @State private var sheet: SubscriptionSheet?
    @State private var reference = ReferenceData()
    @Environment(\.theme) private var theme

    var body: some View {
        content
            .navigationTitle("Subscriptions")
            .navigationBarTitleDisplayMode(.inline)
            .task {
                async let referenceLoad: Void = reference.load(api: api)
                await load()
                await referenceLoad
            }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .sheet(item: $sheet) { sheet in
                SubscriptionFormSheet(model: model, row: sheet.row, reference: reference)
            }
            .editFailureAlert(model.editError, clear: model.clearEditError)
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Subscriptions", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            ReferenceDataRetrySection(reference: reference)
            if model.subscriptions.isEmpty, model.bills.isEmpty {
                EmptyState(
                    title: model.includeInactive ? "No subscriptions or bills" : "No active subscriptions or bills",
                    systemImage: "arrow.triangle.2.circlepath",
                    description: "Add the streaming, software and bills you pay regularly to see what they cost a year.",
                    actionTitle: "Add subscription",
                    action: { sheet = .add }
                )
                .listRowBackground(Color.clear)
            } else {
                summarySection
                Section {
                    Button("Add subscription", systemImage: "plus.circle.fill") { sheet = .add }
                }
                // With one group, its total is the summary above, so the header doesn't repeat it.
                let showsTotals = !model.subscriptions.isEmpty && !model.bills.isEmpty
                if !model.subscriptions.isEmpty {
                    rowsSection("Subscriptions", rows: model.subscriptions,
                                totals: showsTotals ? model.subscriptionTotals : nil)
                }
                if !model.bills.isEmpty {
                    rowsSection("Bills", rows: model.bills, totals: showsTotals ? model.billTotals : nil)
                }
            }
            Section {
                Toggle("Show inactive", isOn: includeInactive)
            }
        }
        .themedList(theme)
    }

    /// What subscriptions and bills cost together, per month and per year.
    private var summarySection: some View {
        let totals = model.subscriptionTotals + model.billTotals
        return Section {
            SummaryHeader("Recurring per month", amount: totals.monthly.negated) {
                HStack(spacing: 4) {
                    AmountText(totals.annual.negated)
                    Text("a year")
                }
            } accessory: {
                EmptyView()
            }
        }
        .listRowBackground(theme.usesCards ? nil : Color.clear)
    }

    private var includeInactive: Binding<Bool> {
        Binding(
            get: { model.includeInactive },
            set: { value in Task { await model.setIncludeInactive(value, api: api) } }
        )
    }

    private func rowsSection(_ title: String, rows: [Subscription], totals: SubscriptionTotals?) -> some View {
        Section {
            ForEach(rows) { row in rowButton(row) }
        } header: {
            HStack {
                Text(title)
                Spacer()
                if let totals {
                    HStack(spacing: 2) {
                        AmountText(totals.monthly.negated, role: .neutral)
                        Text("/ mo")
                    }
                    .textCase(nil)
                }
            }
        }
    }

    private func rowButton(_ row: Subscription) -> some View {
        EditRowButton { sheet = .edit(row) } label: {
            SubscriptionRow(
                row: row,
                accountName: reference.accountName(for: row.accountId),
                categoryName: reference.categories.name(for: row.categoryId)
            )
        }
        .swipeActions(edge: .trailing) { activeToggle(row) }
        .contextMenu {
            Button("Edit", systemImage: "pencil") { sheet = .edit(row) }
            activeToggle(row)
        }
    }

    @ViewBuilder
    private func activeToggle(_ row: Subscription) -> some View {
        if row.active {
            Button("Deactivate", systemImage: "pause.circle") {
                Task { await model.setActive(row, to: false, api: api) }
            }
            .tint(.gray)
        } else {
            Button("Reactivate", systemImage: "play.circle") {
                Task { await model.setActive(row, to: true, api: api) }
            }
            .tint(.accentColor)
        }
    }

    private func load() async {
        await model.load(api: api)
    }
}

private enum SubscriptionSheet: Identifiable {
    case add
    case edit(Subscription)

    var id: String {
        switch self {
        case .add: "add"
        case .edit(let row): row.id.uuidString
        }
    }

    var row: Subscription? {
        if case .edit(let row) = self { row } else { nil }
    }
}
