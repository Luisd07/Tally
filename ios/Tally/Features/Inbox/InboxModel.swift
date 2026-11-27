import Foundation
import Observation

@Observable
final class InboxModel {
    private let list = PagedList<InboxItem>()
    /// Unreviewed items on the server, not just the loaded ones.
    private(set) var total = 0

    var items: [InboxItem] { list.items }
    var error: APIError? { list.error }
    var editError: EditFailure? { list.editError }
    var isLoadingMore: Bool { list.isLoadingMore }
    var hasLoaded: Bool { list.hasLoaded }
    var hasMore: Bool { list.hasMore }

    /// Replaces the list with the first page, keeping the loaded depth. A response superseded by a newer `load` is ignored.
    func load(api: APIClient) async {
        if let page = await list.load(fetch: { try await api.inbox(limit: $0) }) { total = page.total }
    }

    /// Appends the next page. Does nothing without a cursor or while another page is loading.
    func loadMore(api: APIClient) async {
        if let page = await list.loadMore(fetch: { try await api.inbox(cursor: $0) }) { total = page.total }
    }

    func confirm(_ item: InboxItem, api: APIClient) async {
        await edit(item, patch: TransactionPatch(reviewed: true), api: api)
    }

    /// Accepts the suggestion: sends only the suggested fields that exist, plus `reviewed`.
    /// Confirms the suggestion, with the AI's clean merchant name when it has one. Returns the rule to offer when
    /// the server saved it and `ruleWorthy` says this makes three for the merchant.
    @discardableResult
    func applySuggestion(_ item: InboxItem, api: APIClient) async -> RuleSuggestion? {
        let patch = TransactionPatch(
            categoryId: item.suggestedCategoryId.map { Optional($0) },
            bucket: item.suggestedBucket,
            reviewed: true,
            merchant: item.suggestedMerchant.map { Optional($0) }
        )
        guard await edit(item, patch: patch, api: api), item.ruleWorthy else { return nil }
        return RuleSuggestion(
            before: item.transaction,
            categoryId: item.suggestedCategoryId ?? item.transaction.categoryId,
            bucket: item.suggestedBucket ?? item.transaction.bucket
        )
    }

    /// Sets `bucket` and the category (`nil` clears it) and marks the item reviewed. Returns the rule to suggest
    /// once the server saved it, or nil when it failed or `RuleSuggestion` offers none.
    @discardableResult
    func recategorize(_ item: InboxItem, categoryId: UUID?, bucket: Bucket, api: APIClient) async -> RuleSuggestion? {
        let patch = TransactionPatch(categoryId: Optional(categoryId), bucket: bucket, reviewed: true)
        guard await edit(item, patch: patch, api: api) else { return nil }
        return RuleSuggestion(before: item.transaction, categoryId: categoryId, bucket: bucket)
    }

    func clearEditError() {
        list.clearEditError()
    }

    /// Applies `patch` optimistically. The server keeps `unmatchedManual` rows listed, so those are
    /// updated in place; every other row leaves the list. A failure restores the row and total. Once the server
    /// accepts it, a load sent before is dropped so it can't put the row back or reset the total, then sent again.
    /// Returns whether the server accepted it.
    @discardableResult
    private func edit(_ item: InboxItem, patch: TransactionPatch, api: APIClient) async -> Bool {
        await list.edit(
            id: item.id,
            RowEdit(rowName: item.transaction.displayName, patch: patch, leaves: { !$0.unmatchedManual }),
            countChanged: { total += $0 },
            send: { try await api.updateTransaction(id: item.id, patch: patch) },
            reload: { await load(api: api) }
        )
    }
}
