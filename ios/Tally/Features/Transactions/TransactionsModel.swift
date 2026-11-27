import Foundation
import Observation

/// A category's month to show on the Transactions tab, handed over from another tab.
struct TransactionFocus: Equatable {
    let categoryId: UUID
    let month: MonthRef
}

/// What the Transactions tab asks the server for.
struct TransactionFilter: Equatable {
    var month = MonthRef()
    var search = ""
    var bucket: Bucket?
    var accountId: UUID?
    var categoryId: UUID?

    /// Whether the bucket, account or category is narrowing the list.
    var hasRefinements: Bool { bucket != nil || accountId != nil || categoryId != nil }

    mutating func clearRefinements() {
        bucket = nil
        accountId = nil
        categoryId = nil
    }

    /// Narrows the list to just `focus`'s category in its month.
    mutating func show(_ focus: TransactionFocus) {
        self = TransactionFilter(month: focus.month, categoryId: focus.categoryId)
    }

    /// Whether `transaction` belongs in a list filtered by the bucket and category. Month and account are
    /// the server's call and edits never change them. Search is left to the server's next load: merchant
    /// and notes edits can change whether a row matches it, and the list keeps the row until then.
    func admits(_ transaction: Transaction) -> Bool {
        (bucket == nil || transaction.bucket == bucket) && (categoryId == nil || transaction.categoryId == categoryId)
    }

    func query(limit: Int? = nil, cursor: String? = nil) -> TransactionQuery {
        TransactionQuery(
            month: month,
            search: search.trimmedOrNil,
            bucket: bucket,
            accountId: accountId,
            categoryId: categoryId,
            limit: limit,
            cursor: cursor
        )
    }
}

@Observable
final class TransactionsModel {
    /// Change it, then call `load`. Paging and edits keep using the filter of the last `load`.
    var filter = TransactionFilter()

    private let list = FilteredPagedList<TransactionFilter, Transaction>(filter: TransactionFilter())
    /// Called once after each edit the server accepted, so other screens can refresh what the edit may have changed.
    private let onEdited: (() -> Void)?

    init(onEdited: (() -> Void)? = nil) {
        self.onEdited = onEdited
    }

    var items: [Transaction] { list.items }
    var error: APIError? { list.error }
    var editError: EditFailure? { list.editError }
    var isLoadingMore: Bool { list.isLoadingMore }
    var hasLoaded: Bool { list.hasLoaded }
    var hasMore: Bool { list.hasMore }
    /// The latest failed split, shown in the sheet that sent it. Cleared when the next one starts.
    private(set) var splitError: APIError?
    /// The children of the splits saved this session, for a detail whose filter hides some or all of them.
    private var splitChildren: [UUID: [Transaction]] = [:]

    /// Whether rows are being picked for a bulk edit rather than opened. Ends by itself when a load or a bulk edit
    /// leaves the list empty, since the controls go with the rows.
    private(set) var isSelecting = false
    /// The rows picked for a bulk edit: at most `TransactionBulkUpdate.maxIds`, the most one request takes.
    /// It keeps only listed rows: a filter change drops it with the rows, a load drops the rows it no longer
    /// lists, and a bulk edit that saved drops the rows it sent.
    private(set) var selection: Set<UUID> = []

    /// Whether the selection holds as many rows as one bulk edit takes, so no more can be picked.
    var isSelectionFull: Bool { selection.count >= TransactionBulkUpdate.maxIds }

    /// Whether "Select all" would add a row: the selection has room and a listed row isn't in it.
    var canSelectAll: Bool { !isSelectionFull && items.contains { !selection.contains($0.id) } }

    /// The category every selected row has, or nil when they differ or have none.
    var sharedCategoryId: UUID? {
        let categories = Set(items.filter { selection.contains($0.id) }.map(\.categoryId))
        return categories.count == 1 ? categories.first ?? nil : nil
    }

    func transaction(id: UUID) -> Transaction? {
        items.first { $0.id == id }
    }

    /// The children of split row `id`: every child its last split this session returned, as listed when it is,
    /// or else the listed rows that name it as their parent.
    func children(of id: UUID) -> [Transaction] {
        guard let split = splitChildren[id] else { return items.filter { $0.parentId == id } }
        return split.map { child in transaction(id: child.id) ?? child }
    }

    /// Loads when nothing is loaded yet or the filter differs from the last load's. Returning to the
    /// list (from a detail, say) with the same filter keeps what is on screen.
    func loadIfNeeded(api: APIClient) async {
        guard !hasLoaded || filter != list.lastRequested else { return }
        await load(api: api)
    }

    /// Replaces the list with the first page for the current filter. Reloading the same filter keeps the rows
    /// (and the loaded depth) until the response arrives; a different filter drops them first.
    /// A response superseded by a newer `load` is ignored.
    func load(api: APIClient) async {
        let page = await list.load(
            filter,
            filterChanged: { selection = [] },
            fetch: { try await api.transactions($0.query(limit: $1)) }
        )
        if page != nil { keepSelectionListed() }
    }

    /// Appends the next page. Does nothing without a cursor or while another page is loading.
    func loadMore(api: APIClient) async {
        await list.loadMore(fetch: { try await api.transactions($0.query(cursor: $1)) })
    }

    /// Sets `bucket` and the category (`nil` clears it). Returns the rule to suggest once the server saved it, or
    /// nil when it failed or `RuleSuggestion` offers none.
    @discardableResult
    func recategorize(
        _ transaction: Transaction, categoryId: UUID?, bucket: Bucket, api: APIClient
    ) async -> RuleSuggestion? {
        let patch = TransactionPatch(categoryId: Optional(categoryId), bucket: bucket)
        guard await edit(transaction, patch: patch, api: api) else { return nil }
        return RuleSuggestion(before: transaction, categoryId: categoryId, bucket: bucket)
    }

    /// Blank text clears the notes.
    func setNotes(_ transaction: Transaction, _ text: String, api: APIClient) async {
        await edit(transaction, patch: TransactionPatch(notes: Optional(text.trimmedOrNil)), api: api)
    }

    /// Blank text clears the merchant override.
    func setMerchant(_ transaction: Transaction, _ text: String, api: APIClient) async {
        await edit(transaction, patch: TransactionPatch(merchant: Optional(text.trimmedOrNil)), api: api)
    }

    /// Sets the merchant and, when it has one, the category a receipt reading suggests (#170).
    func useReceiptReading(_ transaction: Transaction, _ reading: ReceiptReading, api: APIClient) async {
        let patch = TransactionPatch(
            categoryId: reading.categoryId.map { Optional($0) },
            merchant: reading.merchant.map { Optional($0) }
        )
        await edit(transaction, patch: patch, api: api)
    }

    func setReviewed(_ transaction: Transaction, _ reviewed: Bool, api: APIClient) async {
        await edit(transaction, patch: TransactionPatch(reviewed: reviewed), api: api)
    }

    /// Splits `transaction` into `parts`. On success the list shows the server's split parent with its
    /// children right after it (those the bucket and category filters admit) in place of any earlier children,
    /// without reloading; a load sent before it is dropped and sent again. An edit still in flight on the parent or
    /// an earlier child changes nothing when it settles, so it can't undo the split. Returns false on failure, with `splitError` set unless it was
    /// cancelled; the caller keeps its draft, and sending it again replaces the children, so a retry can't
    /// duplicate them.
    func split(_ transaction: Transaction, parts: [SplitPart], api: APIClient) async -> Bool {
        splitError = nil
        do {
            let result = try await api.splitTransaction(id: transaction.id, parts: parts)
            let filter = list.loadedFilter
            splitChildren[transaction.id] = result.children
            await list.splice(
                parent: result.parent,
                children: result.children.filter(filter.admits),
                removing: { $0.parentId == result.parent.id },
                reload: { await load(api: api) }
            )
            onEdited?()
            return true
        } catch is CancellationError {
            return false
        } catch {
            splitError = APIError(wrapping: error)
            return false
        }
    }

    func clearEditError() {
        list.clearEditError()
    }

    func isInSelection(_ id: UUID) -> Bool {
        selection.contains(id)
    }

    func beginSelecting() {
        isSelecting = true
    }

    /// Stops picking rows and drops the selection.
    func endSelecting() {
        isSelecting = false
        clearSelection()
    }

    /// Adds row `id` unless the selection is full.
    func select(_ id: UUID) {
        guard !isSelectionFull else { return }
        selection.insert(id)
    }

    func deselect(_ id: UUID) {
        selection.remove(id)
    }

    func toggleSelection(_ id: UUID) {
        if isInSelection(id) { deselect(id) } else { select(id) }
    }

    /// Adds the loaded rows, in list order, until the selection is full.
    func selectAllLoaded() {
        for transaction in items where !isSelectionFull {
            selection.insert(transaction.id)
        }
    }

    func clearSelection() {
        selection = []
    }

    /// Sets the category of every selected row (`nil` clears it).
    func bulkSetCategory(_ categoryId: UUID?, api: APIClient) async {
        await bulkEdit(patch: TransactionPatch(categoryId: Optional(categoryId)), api: api)
    }

    func bulkSetBucket(_ bucket: Bucket, api: APIClient) async {
        await bulkEdit(patch: TransactionPatch(bucket: bucket), api: api)
    }

    func bulkSetReviewed(_ reviewed: Bool, api: APIClient) async {
        await bulkEdit(patch: TransactionPatch(reviewed: reviewed), api: api)
    }

    /// Applies `patch` optimistically. A row the patch moves out of the active bucket or category filter
    /// leaves the list; any other is updated in place. A failure restores the row. Once the server accepts it, a
    /// load sent before is dropped so it can't undo the edit, then sent again. Returns whether the server accepted it.
    @discardableResult
    private func edit(_ transaction: Transaction, patch: TransactionPatch, api: APIClient) async -> Bool {
        let filter = list.loadedFilter
        let saved = await list.edit(
            id: transaction.id,
            RowEdit(rowName: transaction.displayName, patch: patch, leaves: { !filter.admits($0.applying(patch)) }),
            send: { try await api.updateTransaction(id: transaction.id, patch: patch) },
            reload: { await load(api: api) }
        )
        if saved { onEdited?() }
        return saved
    }

    /// Applies `patch` to every selected listed row optimistically in one request, all-or-nothing, with
    /// `edit`'s filter rule. Success drops the sent rows, and any no longer listed, from the selection; a failure
    /// (a reply missing a sent row too) restores every row and keeps the selection, so the edit can be retried.
    private func bulkEdit(patch: TransactionPatch, api: APIClient) async {
        let ids = items.map(\.id).filter(selection.contains)
        let filter = list.loadedFilter
        let rowName = ids.count == 1
            ? transaction(id: ids[0])?.displayName ?? "1 transaction"
            : "\(ids.count) transactions"
        let saved = await list.editMany(
            ids: ids,
            RowEdit(rowName: rowName, patch: patch, leaves: { !filter.admits($0.applying(patch)) }),
            send: { try await api.bulkUpdate(ids: $0, patch: patch).items },
            reload: { await load(api: api) }
        )
        guard saved else { return }
        selection.subtract(ids)
        keepSelectionListed()
        onEdited?()
    }

    /// Drops the selected rows that aren't listed, and ends selecting when no row is.
    private func keepSelectionListed() {
        selection.formIntersection(items.map(\.id))
        if items.isEmpty { isSelecting = false }
    }
}
