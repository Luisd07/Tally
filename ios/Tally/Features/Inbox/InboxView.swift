import SwiftUI

struct InboxView: View {
    let model: InboxModel

    @Environment(APIClient.self) private var api
    @Environment(\.theme) private var theme
    @State private var categories: [Category] = []
    @State private var recategorizing: InboxItem?
    @State private var attachTarget: Transaction?
    @State private var categoriesError: APIError?
    @State private var uploads = ReceiptUploads()
    /// A rule to offer after confirming a suggestion that makes three for its merchant.
    @State private var ruleOffer: RuleSuggestion?

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Inbox")
                .environment(\.onCategoryCreated) { category in
                    if !categories.contains(where: { $0.id == category.id }) { categories.append(category) }
                }
                .task { await refresh() }
                .refreshable { await refresh() }
                .attachesReceipts(to: $attachTarget, uploads: uploads)
                .offersRuleAfterRecategorize(
                    $recategorizing, transaction: \.transaction, categories: categories, offer: $ruleOffer
                ) {
                    await model.recategorize($0, categoryId: $1, bucket: $2, api: api)
                }
                .editFailureAlert(model.editError, clear: model.clearEditError)
        }
    }

    @ViewBuilder
    private var content: some View {
        if !model.items.isEmpty {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Inbox", error: error) { Task { await reload() } }
        } else if model.hasLoaded {
            EmptyState(
                title: "All caught up",
                systemImage: "checkmark.circle",
                description: "New transactions land here after each sync for you to confirm."
            )
        } else {
            ProgressView()
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await reload() } }
            }
            if let categoriesError {
                RetrySection(title: "Couldn't load categories", error: categoriesError) { Task { await loadCategories() } }
            }
            ForEach(model.items) { item in
                TransactionRow(
                    transaction: item.transaction,
                    categories: categories,
                    statuses: item.statuses,
                    suggestion: item.suggestion(categories: categories),
                    suggestionIsAI: item.isAISuggestion
                )
                .swipeActions(edge: .leading) { leadingActions(item) }
                .swipeActions(edge: .trailing) { trailingActions(item) }
                .contextMenu {
                    leadingActions(item)
                    trailingActions(item)
                }
                .loadsMore(whenLast: item.id == model.items.last?.id, hasMore: model.hasMore) {
                    Task { await model.loadMore(api: api) }
                }
            }
            if model.isLoadingMore { LoadingMoreRow() }
        }
        .themedList(theme)
    }

    @ViewBuilder
    private func leadingActions(_ item: InboxItem) -> some View {
        if !(item.unmatchedManual && item.transaction.reviewed) {
            Button("Confirm", systemImage: "checkmark") {
                Task { await model.confirm(item, api: api) }
            }
            .tint(.accentColor)
        }
        if item.suggestedCategoryId != nil || item.suggestedBucket != nil {
            Button("Apply suggestion", systemImage: item.isAISuggestion ? "sparkles" : "wand.and.stars") {
                Task {
                    if let rule = await model.applySuggestion(item, api: api) { ruleOffer = rule }
                }
            }
            .tint(.gray)
        }
    }

    @ViewBuilder
    private func trailingActions(_ item: InboxItem) -> some View {
        Button("Recategorize", systemImage: "tag") { recategorizing = item }
            .tint(.accentColor)
        Button("Attach receipt", systemImage: "paperclip") { attachTarget = item.transaction }
            .tint(.gray)
    }

    private func reload() async {
        await model.load(api: api)
    }

    private func refresh() async {
        async let refreshed: Void = reload()
        async let named: Void = loadCategories()
        _ = await (refreshed, named)
    }

    private func loadCategories() async {
        do {
            categories = try await api.categories()
            categoriesError = nil
        } catch is CancellationError {
            return
        } catch {
            categoriesError = APIError(wrapping: error)
        }
    }
}

private extension InboxItem {
    var statuses: [TransactionStatus] {
        TransactionStatus.of(transaction, needsResplit: needsResplit, awaitingBankMatch: unmatchedManual, in: .inbox)
    }

    /// "Suggested: Dining · Business", with the AI's merchant name first when it has one.
    func suggestion(categories: [Category]) -> String? {
        let parts = [suggestedMerchant, categories.name(for: suggestedCategoryId), suggestedBucket?.label].compactMap { $0 }
        guard !parts.isEmpty else { return nil }
        return (isAISuggestion ? "AI suggests: " : "Suggested: ") + parts.joined(separator: " · ")
    }
}
