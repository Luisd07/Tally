import Foundation
import Observation

/// A list screen's rows: loads follow `LatestLoad`'s rules, edits `EditableRows`', and a saved edit or write drops
/// the loads sent before it, then loads again. The screen's model owns the fetches and anything derived from the
/// rows (order, totals).
/// - With an include-inactive filter (`init(isActive:)`), a write loads again only when it dropped a load in flight.
/// - Without one (`init()`), every row is listed and every accepted write loads again, for a screen whose other
///   data (a total from another endpoint) a write changes. `init(reloadsAfterEveryWrite: false)` lists every row
///   too but loads again like the filtered list, for a screen whose writes change nothing else it shows.
@Observable
final class LoadedList<Row: EditableRow> {
    /// Whether closed rows are listed too. Change it with `setIncludeInactive`.
    private(set) var includeInactive = false
    /// The latest load failure. It sits beside the rows: a failed reload keeps them on screen.
    var error: APIError? { loader.error }
    /// Whether a load has finished, with rows or an error.
    var hasLoaded: Bool { loader.hasLoaded }
    /// Whether the rows are the server's: a load succeeded. A failed refresh keeps it true.
    var hasData: Bool { loader.hasData }
    /// The latest failed edit, already rolled back. Cleared when the next edit starts.
    var editError: EditFailure? { rows.editError }
    var items: [Row] { rows.items }

    /// The rows, for the model's local changes (order, a created row, an amended one).
    let rows = EditableRows<Row>()
    private let loader = LatestLoad()
    /// Which rows the active-only list keeps; nil without a filter.
    private let isActive: ((Row) -> Bool)?
    /// Whether every accepted write loads again, not only one that dropped a load in flight.
    private let reloadsAfterEveryWrite: Bool

    /// A list with an include-inactive filter: `isActive` says which rows it keeps while closed ones are hidden.
    init(isActive: @escaping (Row) -> Bool) {
        self.isActive = isActive
        reloadsAfterEveryWrite = false
    }

    /// A list without a filter, which loads again after every accepted write unless `reloadsAfterEveryWrite` is
    /// false.
    init(reloadsAfterEveryWrite: Bool = true) {
        isActive = nil
        self.reloadsAfterEveryWrite = reloadsAfterEveryWrite
    }

    /// As in `EditableRows`: without an explicit deinit, Swift 6.3's optimizer crashes in the synthesized one for a
    /// generic class in Release.
    deinit {}

    /// Fetches with the current filter and lists `rows(of:)` the result. Returns it, or nil when the load failed
    /// (with `error` set), was cancelled or was superseded; the rows stay then.
    @discardableResult
    func load<Value>(
        _ fetch: (_ includeInactive: Bool) async throws -> Value,
        rows rowsOf: (Value) -> [Row]
    ) async -> Value? {
        let filter = includeInactive
        guard let value = await loader.load(fetch: { try await fetch(filter) }) else { return nil }
        rows.replace(with: rowsOf(value))
        return value
    }

    /// Switches between active-only and all rows, then `reload`s. Turning it off drops the closed rows at once.
    func setIncludeInactive(_ include: Bool, reload: () async -> Void) async {
        guard include != includeInactive else { return }
        includeInactive = include
        if !include { rows.replace(with: rows.items.filter { isActive?($0) ?? true }) }
        await reload()
    }

    /// Applies `patch` to row `id` at once, then `send`s it, by `EditableRows.edit`'s rules; a failure goes to
    /// `failed` instead of `editError` when it is given. A row that turns inactive leaves the list unless closed
    /// ones are shown. Once the server accepts it, a load sent before is dropped so it can't put the old row back,
    /// and `reload` sends one again: when one was dropped, or always without a filter. Returns whether it was
    /// accepted.
    @discardableResult
    func update(
        id: UUID,
        rowName: String,
        patch: Row.Patch,
        changed: (_ countChange: Int) -> Void = { _ in },
        failed: ((EditFailure) -> Void)? = nil,
        send: () async throws -> Row.Saved,
        reload: () async -> Void
    ) async -> Bool {
        let showsInactive = includeInactive
        let saved = await rows.edit(
            id: id,
            RowEdit(
                rowName: rowName,
                patch: patch,
                leaves: { [isActive] in !showsInactive && !(isActive?($0.applying(patch)) ?? true) }
            ),
            changed: changed,
            failed: failed,
            send: send
        )
        return await settle(saved, reload: reload)
    }

    /// Removes row `id` at once, then `send`s the delete, by `EditableRows.delete`'s rules, so a failure puts the row
    /// back where it stood. Once the server accepts it, loads follow `update`'s rules, so a load sent before can't
    /// put the row back. Returns whether it was accepted.
    @discardableResult
    func remove(id: UUID, rowName: String, send: () async throws -> Void, reload: () async -> Void) async -> Bool {
        await settle(await rows.delete(id: id, rowName: rowName, send: send), reload: reload)
    }

    /// After a write: when the server `accepted` it, drops the loads sent before and `reload`s when one was dropped,
    /// or after every write when the list says so. Returns `accepted`.
    private func settle(_ accepted: Bool, reload: () async -> Void) async -> Bool {
        guard accepted else { return false }
        if loader.supersede() || reloadsAfterEveryWrite { await reload() }
        return true
    }

    /// Sends a write and lands what the server returned, by `LatestLoad.land`'s rules; without a filter, `reload`
    /// runs after every write that succeeded. Returns whether it succeeded.
    func land<Value>(
        _ write: () async throws -> Value,
        apply: (Value) -> Void,
        reload: () async -> Void,
        failed: (APIError) -> Void
    ) async -> Bool {
        await loader.land(write, apply: apply, reload: reload, reloadsAlways: reloadsAfterEveryWrite, failed: failed)
    }

    func clearEditError() {
        rows.clearEditError()
    }
}

/// A screen model whose rows are a `LoadedList`. The screen reads the load and edit state through these, which
/// come from `list`.
protocol ListScreenModel: AnyObject {
    associatedtype Row: EditableRow
    /// The rows and their load and edit state.
    var list: LoadedList<Row> { get }
}

extension ListScreenModel {
    /// The latest load failure. It sits beside the data: a failed reload keeps it on screen.
    var error: APIError? { list.error }
    /// Whether a load has finished, with data or an error.
    var hasLoaded: Bool { list.hasLoaded }
    /// Whether the data is the server's: a load succeeded. A failed refresh keeps it true.
    var hasData: Bool { list.hasData }
    /// The latest failed edit, already rolled back. Cleared when the next edit starts.
    var editError: EditFailure? { list.editError }

    func clearEditError() {
        list.clearEditError()
    }
}
