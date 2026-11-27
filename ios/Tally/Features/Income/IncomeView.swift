import SwiftUI

struct IncomeView: View {
    @Environment(APIClient.self) private var api
    @State private var model = IncomeModel()
    @State private var month = MonthRef()
    @State private var sheet: IncomeSheet?

    private let currentMonth = MonthRef()

    var body: some View {
        content
            .navigationTitle("Income")
            .navigationBarTitleDisplayMode(.inline)
            // Expected pay is often dated next month, so Next goes one month past the current one.
            .monthScreen(month: $month, latest: currentMonth.next) { await load() }
            .sheet(item: $sheet) { sheet in
                IncomeFormSheet(model: model, entry: sheet.entry, month: month)
            }
            .editFailureAlert(model.editError, clear: model.clearEditError)
    }

    private var content: some View {
        MonthContent(
            hasData: model.hasData,
            error: model.error,
            failedTitle: "Couldn't load Income",
            retry: { Task { await load() } }
        ) { list }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            totalsSection
            Section {
                Button("Add income", systemImage: "plus.circle.fill") { sheet = .add }
            }
            if model.entries.isEmpty {
                Section {
                    Text("No income this month").foregroundStyle(.secondary)
                }
            }
            ForEach(model.groups, id: \.type) { group in
                Section(group.type.label) {
                    ForEach(group.entries) { entry in row(entry) }
                }
            }
        }
    }

    private var totalsSection: some View {
        Section("Totals") {
            LabeledContent("Expected", value: model.totals.expected.formatted())
            LabeledContent("Received", value: model.totals.received.formatted())
            if model.remaining > Money(0) {
                LabeledContent("Remaining", value: model.remaining.formatted())
            }
        }
    }

    private func row(_ entry: IncomeEntry) -> some View {
        Button { sheet = .edit(entry) } label: {
            IncomeRow(entry: entry)
        }
        .swipeActions(edge: .trailing) {
            if !entry.isFullyReceived, entry.expected != nil {
                Button("Mark received") { Task { await model.markReceived(entry, api: api) } }
                    .tint(.green)
            }
        }
    }

    private func load() async {
        await model.load(month: month, api: api)
    }
}

/// An entry's name and date, its link and received marks, and its amounts.
private struct IncomeRow: View {
    let entry: IncomeEntry

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.displayName)
                Text(entry.date.formatted()).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if entry.transactionId != nil {
                Image(systemName: "link").foregroundStyle(.secondary)
                    .accessibilityLabel("Linked to a transaction")
            }
            if entry.isFullyReceived {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    .accessibilityLabel("Received")
            }
            amounts
        }
        .foregroundStyle(.primary)
    }

    private var amounts: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text("Expected \(entry.expected?.formatted() ?? "—")")
            Text("Received \(entry.received?.formatted() ?? "—")")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

private enum IncomeSheet: Identifiable {
    case add
    case edit(IncomeEntry)

    var id: String {
        switch self {
        case .add: "add"
        case .edit(let entry): entry.id.uuidString
        }
    }

    var entry: IncomeEntry? {
        if case .edit(let entry) = self { entry } else { nil }
    }
}
