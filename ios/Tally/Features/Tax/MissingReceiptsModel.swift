import Foundation
import Observation

/// What the missing-receipt list asks the server for.
struct MissingReceiptFilter: Equatable {
    var year = MonthRef().year
    var bucket = ReceiptBucket.business
}

@Observable
final class MissingReceiptsModel {
    /// Change it, then call `load`. Paging keeps using the filter of the last `load`.
    var bucket = ReceiptBucket.business

    private let list = FilteredPagedList<MissingReceiptFilter, Transaction>(filter: MissingReceiptFilter())
    /// Rows the server lists for the filter, not just the loaded ones.
    private(set) var count = 0
    /// Whether a load succeeded for the filter of the latest `load`. A failed refresh keeps it true.
    private(set) var hasData = false
    /// Rows whose receipt landed since the latest `load` began. A response the server built before the upload
    /// still lists them, so they are dropped from it.
    private var attached: Set<UUID> = []

    var items: [Transaction] { list.items }
    var error: APIError? { list.error }
    var isLoadingMore: Bool { list.isLoadingMore }
    var hasMore: Bool { list.hasMore }

    /// What `load(year:api:)` asks for, e.g. as a task id.
    func filter(year: Int) -> MissingReceiptFilter {
        MissingReceiptFilter(year: year, bucket: bucket)
    }

    /// Replaces the list with the first page for `year` and `bucket`. Reloading the same filter keeps the rows
    /// (and the loaded depth) until the response arrives; a different filter drops them first.
    /// A response superseded by a newer `load` is ignored.
    func load(year: Int, api: APIClient) async {
        let settled = attached
        let page = await list.load(
            filter(year: year),
            filterChanged: {
                count = 0
                hasData = false
                attached = []
            },
            fetch: { try await api.missingReceipts(year: $0.year, bucket: $0.bucket, limit: $1) }
        )
        guard let page else { return }
        attached.subtract(settled)
        count = page.count - dropAttachedRows()
        hasData = true
    }

    /// Appends the next page. Does nothing without a cursor or while another page is loading.
    func loadMore(api: APIClient) async {
        let attachedBefore = attached
        let page = await list.loadMore(fetch: {
            try await api.missingReceipts(year: $0.year, bucket: $0.bucket, cursor: $1)
        })
        guard let page else { return }
        let dropped = dropAttachedRows()
        // After an attach during the request, the page's count may still include it; the local count already doesn't.
        count = (attached == attachedBefore ? page.count : count) - dropped
    }

    /// A receipt is on `transactionId` now, so the server no longer lists it: drop the row and count it off without
    /// a reload, and keep it out of responses already on their way. A row that isn't listed changes nothing now.
    func receiptAttached(_ transactionId: UUID) {
        attached.insert(transactionId)
        if list.remove(id: transactionId) { count -= 1 }
    }

    /// Removes the listed rows in `attached`. Returns how many there were.
    private func dropAttachedRows() -> Int {
        var dropped = 0
        for id in attached where list.remove(id: id) {
            dropped += 1
        }
        return dropped
    }
}
