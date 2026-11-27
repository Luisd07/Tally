import SwiftUI

/// List sections for `year`'s expenses in the chosen bucket that have no receipt, each with an Attach receipt
/// button. The screen loads `model` for `year`; these sections page it and retry it.
struct MissingReceiptsSection: View {
    let year: Int
    @Bindable var model: MissingReceiptsModel
    let categories: [Category]
    /// Called with the row whose Attach receipt button was tapped.
    let attach: (Transaction) -> Void

    @Environment(APIClient.self) private var api

    var body: some View {
        Section {
            Picker("Bucket", selection: $model.bucket) {
                ForEach(ReceiptBucket.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
        }
        if model.hasData {
            loaded
        } else if let error = model.error {
            RetrySection(title: "Couldn't load missing receipts", error: error) { reload() }
        } else {
            Section { ProgressView().frame(maxWidth: .infinity) }
        }
    }

    @ViewBuilder
    private var loaded: some View {
        if let error = model.error {
            RetrySection(title: "Couldn't refresh", error: error) { reload() }
        }
        if !model.items.isEmpty || model.hasMore {
            Section(model.count == 1 ? "1 missing receipt" : "\(model.count) missing receipts") {
                ForEach(model.items) { row($0) }
                if model.items.isEmpty {
                    // Every loaded row got its receipt; fetch the next page instead of waiting for a last row.
                    LoadingMoreRow().loadsMore(whenLast: true, hasMore: model.hasMore) { loadMore() }
                } else if model.isLoadingMore {
                    LoadingMoreRow()
                }
            }
        } else if model.error == nil {
            Section {
                ContentUnavailableView(
                    "Every \(model.bucket.label.lowercased()) expense in \(String(year)) has a receipt",
                    systemImage: "checkmark.circle"
                )
            }
        }
    }

    private func row(_ transaction: Transaction) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            TransactionRow(transaction: transaction, categories: categories)
            Button("Attach receipt", systemImage: "paperclip") { attach(transaction) }
                .buttonStyle(.bordered)
        }
        .loadsMore(whenLast: transaction.id == model.items.last?.id, hasMore: model.hasMore) { loadMore() }
    }

    private func reload() {
        Task { await model.load(year: year, api: api) }
    }

    private func loadMore() {
        Task { await model.loadMore(api: api) }
    }
}
