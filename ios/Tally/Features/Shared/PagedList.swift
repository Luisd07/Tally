import Foundation
import Observation

/// A page of rows from a cursor-paged endpoint.
protocol PageResponse {
    associatedtype Row
    var items: [Row] { get }
    var nextCursor: String? { get }
}

extension PageResponse {
    /// The most rows the server sends in one page.
    static var maxLimit: Int { 500 }
}

extension TransactionPage: PageResponse {}
extension InboxPage: PageResponse {}
extension MissingReceiptPage: PageResponse {}

/// The loaded pages of a cursor-paged list of editable rows: first-page replace, cursor paging with
/// dedupe, and optimistic PATCH edits (`EditableRows`' rules). Loads follow `LatestLoad`'s rules, and an edit the
/// server accepted drops the loads sent before it, then loads again, as in `LoadedList`. Each model keeps its own
/// policy (request, totals); `FilteredPagedList` adds the filter rules.
@Observable
final class PagedList<Row: EditableRow> {
    /// The loaded pages, in server order.
    var items: [Row] { rows.items }
    /// The latest load failure. It sits beside `items`: a failed reload keeps the rows on screen.
    var error: APIError? { loader.error }
    /// The latest failed edit and the row it was for; its message is the server's. Cleared when the next edit starts.
    var editError: EditFailure? { rows.editError }
    private(set) var isLoadingMore = false
    /// Whether a load has finished, with rows or an error. A cancelled or superseded load doesn't count.
    var hasLoaded: Bool { loader.hasLoaded }

    var hasMore: Bool { nextCursor != nil }

    /// Without an explicit deinit, Swift 6.3's optimizer crashes in the synthesized one for this generic class in Release.
    deinit {}

    private let rows = EditableRows<Row>()
    private let loader = LatestLoad()
    private var nextCursor: String?
    /// Bumped by every `load`, so a `loadMore` it overtook doesn't clear `isLoadingMore` for one sent after it.
    private var firstPages = 0

    /// The server's default page size.
    private static var defaultLimit: Int { 100 }

    /// Drops the rows and the load state, as for a different query.
    func reset() {
        rows.replace(with: [])
        nextCursor = nil
        loader.reset()
    }

    /// Replaces the list with a first page. `fetch` gets the `limit` to request: nil for a default page, or the
    /// loaded depth (capped at the server's maximum) so a refresh keeps the pages already loaded.
    /// Returns the page, or nil when it failed, was cancelled or was superseded by a newer `load` or an edit.
    @discardableResult
    func load<Page: PageResponse>(fetch: (_ limit: Int?) async throws -> Page) async -> Page? where Page.Row == Row {
        firstPages += 1
        isLoadingMore = false
        let limit = items.count > Self.defaultLimit ? Swift.min(items.count, Page.maxLimit) : nil
        guard let page = await loader.load(fetch: { try await fetch(limit) }) else { return nil }
        rows.replace(with: page.items)
        nextCursor = page.nextCursor
        return page
    }

    /// Appends the next page. Does nothing without a cursor or while another page is loading.
    /// Returns the page, or nil when nothing was appended.
    @discardableResult
    func loadMore<Page: PageResponse>(fetch: (_ cursor: String) async throws -> Page) async -> Page? where Page.Row == Row {
        guard let cursor = nextCursor, !isLoadingMore else { return nil }
        let current = firstPages
        isLoadingMore = true
        defer { if firstPages == current { isLoadingMore = false } }
        guard let page = await loader.loadMore(fetch: { try await fetch(cursor) }) else { return nil }
        rows.append(contentsOf: page.items)
        nextCursor = page.nextCursor
        return page
    }

    /// Removes row `id`, a change the server already has. Returns whether it was listed.
    @discardableResult
    func remove(id: UUID) -> Bool {
        rows.remove(id: id)
    }

    /// `EditableRows.splice`, for a change the server already has (a saved split): the loads sent before it are
    /// dropped so none can undo it, and `reload` sends one again when one was dropped.
    @discardableResult
    func splice(
        parent: Row,
        children: [Row],
        removing isStale: (Row) -> Bool,
        reload: () async -> Void
    ) async -> Bool {
        let listed = rows.splice(parent: parent, children: children, removing: isStale)
        if loader.supersede() { await reload() }
        return listed
    }

    func clearEditError() {
        rows.clearEditError()
    }

    /// Applies `change`'s patch to row `id` optimistically, then sends it. A row for which `change.leaves` is true
    /// is removed; any other is updated in place. `countChanged` gets -1 when a row leaves and +1 when a failed edit
    /// restores it. The server's reply is applied even after a reload; a rollback only to the rows the edit began on.
    /// Once the server accepts it, a load sent before is dropped so it can't put the old row back, and `reload`
    /// sends one again when one was dropped. Returns whether the server accepted the edit.
    @discardableResult
    func edit(
        id: UUID,
        _ change: RowEdit<Row>,
        countChanged: (Int) -> Void = { _ in },
        send: () async throws -> Row.Saved,
        reload: () async -> Void
    ) async -> Bool {
        await settle(await rows.edit(id: id, change, changed: countChanged, send: send), reload: reload)
    }

    /// `EditableRows.editMany`: one all-or-nothing edit of every listed row in `ids`. `countChanged` gets minus the
    /// rows that left, and the number put back when it fails. Loads follow `edit`'s rules once it is accepted.
    @discardableResult
    func editMany(
        ids: [UUID],
        _ change: RowEdit<Row>,
        countChanged: (Int) -> Void = { _ in },
        send: (_ ids: [UUID]) async throws -> [Row.Saved],
        reload: () async -> Void
    ) async -> Bool where Row.Saved: Identifiable, Row.Saved.ID == UUID {
        await settle(await rows.editMany(ids: ids, change, changed: countChanged, send: send), reload: reload)
    }

    /// After a write: when the server `accepted` it, drops the loads sent before and `reload`s when one was
    /// dropped. Returns `accepted`.
    private func settle(_ accepted: Bool, reload: () async -> Void) async -> Bool {
        guard accepted else { return false }
        if loader.supersede() { await reload() }
        return true
    }
}

/// A `PagedList` for a filtered query. A `load` for a different filter than the latest one drops the old rows
/// before its response arrives; paging and edits keep using the filter of the last successful `load`.
@Observable
final class FilteredPagedList<Filter: Equatable, Row: EditableRow> {
    /// The filter `items` and the paging cursor belong to.
    private(set) var loadedFilter: Filter
    /// The filter of the latest `load` request.
    private(set) var lastRequested: Filter?

    var items: [Row] { list.items }
    var error: APIError? { list.error }
    var editError: EditFailure? { list.editError }
    var isLoadingMore: Bool { list.isLoadingMore }
    var hasLoaded: Bool { list.hasLoaded }
    var hasMore: Bool { list.hasMore }

    private let list = PagedList<Row>()

    /// `filter` stands in for `loadedFilter` until the first load succeeds.
    init(filter: Filter) {
        loadedFilter = filter
    }

    /// Without an explicit deinit, Swift 6.3's optimizer crashes in the synthesized one for this generic class in Release.
    deinit {}

    /// Replaces the list with the first page for `filter`. Reloading the same filter keeps the rows (and the loaded
    /// depth) until the response arrives; a different one drops them first and calls `filterChanged`, so the caller
    /// can drop what it keeps beside them. Returns the page, or nil when it failed, was cancelled or was superseded.
    @discardableResult
    func load<Page: PageResponse>(
        _ filter: Filter,
        filterChanged: () -> Void = {},
        fetch: (_ filter: Filter, _ limit: Int?) async throws -> Page
    ) async -> Page? where Page.Row == Row {
        if filter != lastRequested {
            lastRequested = filter
            list.reset()
            filterChanged()
        }
        let page = await list.load(fetch: { try await fetch(filter, $0) })
        if page != nil { loadedFilter = filter }
        return page
    }

    /// Appends the next page of `loadedFilter`. Returns the page, or nil when nothing was appended.
    @discardableResult
    func loadMore<Page: PageResponse>(
        fetch: (_ filter: Filter, _ cursor: String) async throws -> Page
    ) async -> Page? where Page.Row == Row {
        let loaded = loadedFilter
        return await list.loadMore(fetch: { try await fetch(loaded, $0) })
    }

    /// Removes row `id`, a change the server already has. Returns whether it was listed.
    @discardableResult
    func remove(id: UUID) -> Bool {
        list.remove(id: id)
    }

    /// `PagedList.splice`.
    @discardableResult
    func splice(
        parent: Row,
        children: [Row],
        removing isStale: (Row) -> Bool,
        reload: () async -> Void
    ) async -> Bool {
        await list.splice(parent: parent, children: children, removing: isStale, reload: reload)
    }

    func clearEditError() {
        list.clearEditError()
    }

    /// `PagedList.edit`.
    @discardableResult
    func edit(
        id: UUID,
        _ change: RowEdit<Row>,
        countChanged: (Int) -> Void = { _ in },
        send: () async throws -> Row.Saved,
        reload: () async -> Void
    ) async -> Bool {
        await list.edit(id: id, change, countChanged: countChanged, send: send, reload: reload)
    }

    /// `PagedList.editMany`.
    @discardableResult
    func editMany(
        ids: [UUID],
        _ change: RowEdit<Row>,
        countChanged: (Int) -> Void = { _ in },
        send: (_ ids: [UUID]) async throws -> [Row.Saved],
        reload: () async -> Void
    ) async -> Bool where Row.Saved: Identifiable, Row.Saved.ID == UUID {
        await list.editMany(ids: ids, change, countChanged: countChanged, send: send, reload: reload)
    }
}
