import SwiftUI

/// Home's "Heads up" card (#225): up to three insights, each dismissable and, when Claude proposed a fix, with
/// what it would change above Confirm / Not now.
struct HeadsUpSection: View {
    let model: HeadsUpModel
    var onEdited: (() -> Void)?

    private static let limit = 3

    var body: some View {
        Section {
            ForEach(model.insights.prefix(Self.limit)) { insight in
                HeadsUpRow(insight: insight, model: model, onEdited: onEdited)
            }
            if model.insights.count > Self.limit {
                NavigationLink("See all (\(model.insights.count))") {
                    HeadsUpListView(model: model, onEdited: onEdited)
                }
            }
        } header: {
            Text("Heads up")
        }
    }
}

/// Every insight, for when the card shows only the first few.
struct HeadsUpListView: View {
    let model: HeadsUpModel
    var onEdited: (() -> Void)?

    var body: some View {
        List {
            ForEach(model.insights) { insight in
                HeadsUpRow(insight: insight, model: model, onEdited: onEdited)
            }
        }
        .navigationTitle("Heads up")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct HeadsUpRow: View {
    let insight: Insight
    let model: HeadsUpModel
    var onEdited: (() -> Void)?

    @Environment(APIClient.self) private var api
    /// How long "Done" stays up, with Undo, before the insight leaves the card.
    private static let undoWindow: Duration = .seconds(5)

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.done[insight.id] != nil {
                doneRow
            } else {
                content
            }
            if let failure = model.failures[insight.id] {
                Text(failure).font(.caption).foregroundStyle(.red)
            }
        }
        .padding(.vertical, 2)
        .swipeActions(edge: .trailing) {
            if model.done[insight.id] == nil {
                Button("Dismiss", role: .destructive) { Task { await model.dismiss(insight, api: api) } }
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let transactionId = insight.transactionId {
            NavigationLink {
                InsightTransactionView(insight: insight, transactionId: transactionId, onEdited: onEdited)
            } label: {
                text
            }
        } else {
            text
        }
        if model.canConfirm(insight), let action = model.actions[insight.id] {
            proposal(action)
        } else {
            Button("Dismiss") { Task { await model.dismiss(insight, api: api) } }
                .font(.subheadline)
                .buttonStyle(.borderless)
        }
    }

    private var text: some View {
        Label {
            VStack(alignment: .leading, spacing: 2) {
                Text(insight.title).font(.subheadline.weight(.semibold))
                Text(insight.message).font(.subheadline).foregroundStyle(.secondary)
            }
        } icon: {
            Image(systemName: insight.kind.systemImage)
        }
    }

    /// What confirming changes comes first, so the buttons below it say what they do.
    private func proposal(_ action: AIAction) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(action.summary).font(.subheadline.weight(.medium))
                ForEach(action.lines, id: \.self) { line in
                    Text(line).font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 12) {
                Button("Confirm") { Task { await model.confirm(insight, api: api) } }
                    .buttonStyle(.borderedProminent)
                Button("Not now") { Task { await model.notNow(insight, api: api) } }
                    .buttonStyle(.bordered)
            }
            .controlSize(.small)
        }
    }

    private var doneRow: some View {
        HStack {
            Label("Done", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            Spacer()
            Button("Undo") { Task { await model.undo(insight, api: api) } }
                .buttonStyle(.borderless)
        }
        .task {
            do { try await Task.sleep(for: Self.undoWindow) } catch { return }
            await model.settle(insight, api: api)
        }
    }
}

/// The transaction an insight is about. The server has no lookup by id, so this pages through the insight's month
/// (then the one before it, in case the charge posted just before the insight's day) until it finds the row.
private struct InsightTransactionView: View {
    let insight: Insight
    let transactionId: UUID

    @Environment(APIClient.self) private var api
    @State private var model: TransactionsModel
    @State private var reference = ReferenceData()
    @State private var searched = false

    private static let maxPages = 10

    init(insight: Insight, transactionId: UUID, onEdited: (() -> Void)?) {
        self.insight = insight
        self.transactionId = transactionId
        _model = State(initialValue: TransactionsModel(onEdited: onEdited))
    }

    var body: some View {
        Group {
            if model.transaction(id: transactionId) != nil {
                TransactionDetailView(
                    id: transactionId, model: model, categories: reference.categories, accounts: reference.accounts
                )
            } else if searched {
                ContentUnavailableView(
                    "Transaction not found", systemImage: "questionmark.circle",
                    description: Text("It may have been deleted or moved.")
                )
            } else {
                ProgressView()
            }
        }
        .task { await find() }
        .task { await reference.load(api: api) }
        .editFailureAlert(model.editError, clear: model.clearEditError)
    }

    private func find() async {
        let month = MonthRef(containing: insight.day)
        for candidate in [month, month.previous] {
            model.filter.month = candidate
            await model.load(api: api)
            var pages = 1
            while model.transaction(id: transactionId) == nil, model.hasMore, pages < Self.maxPages {
                await model.loadMore(api: api)
                pages += 1
            }
            if model.transaction(id: transactionId) != nil { return }
        }
        searched = true
    }
}
