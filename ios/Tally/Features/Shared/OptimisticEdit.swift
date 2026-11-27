import Foundation
import Observation

/// A list row that can be edited optimistically: `applying` shows a patch before the server answers,
/// `replacing` writes in what the server sent back.
protocol EditableRow: Identifiable where ID == UUID {
    /// The PATCH body for this row.
    associatedtype Patch
    /// What the server returns for an accepted PATCH.
    associatedtype Saved

    /// This row with the fields `patch` sets, as the server will return it.
    func applying(_ patch: Patch) -> Self
    /// This row carrying the server's `saved` version.
    func replacing(_ saved: Saved) -> Self
}

extension Transaction: @MainActor EditableRow {
    func replacing(_ saved: Transaction) -> Transaction { saved }
}

extension InboxItem: @MainActor EditableRow {
    func applying(_ patch: TransactionPatch) -> InboxItem {
        replacing(transaction.applying(patch))
    }

    func replacing(_ saved: Transaction) -> InboxItem {
        InboxItem(
            transaction: saved,
            needsResplit: needsResplit,
            unmatchedManual: unmatchedManual,
            suggestedCategoryId: suggestedCategoryId,
            suggestedBucket: suggestedBucket,
            suggestionSource: suggestionSource,
            suggestedMerchant: suggestedMerchant,
            ruleWorthy: ruleWorthy
        )
    }
}

extension IncomeEntry: @MainActor EditableRow {
    func replacing(_ saved: IncomeEntry) -> IncomeEntry { saved }
}

extension Subscription: @MainActor EditableRow {
    func replacing(_ saved: Subscription) -> Subscription { saved }
}

extension Reimbursement: @MainActor EditableRow {
    func replacing(_ saved: Reimbursement) -> Reimbursement { saved }
}

extension Rule: @MainActor EditableRow {
    func replacing(_ saved: Rule) -> Rule { saved }
}

extension CardSummary: @MainActor EditableRow {
    /// This card with the account fields `patch` sets; the balance stays.
    func applying(_ patch: AccountPatch) -> CardSummary {
        CardSummary(account: account.applying(patch), balance: balance, balanceDate: balanceDate, sourceHint: sourceHint, interest: interest)
    }

    /// This card carrying the server's `saved` account; the balance stays.
    func replacing(_ saved: Account) -> CardSummary {
        CardSummary(account: saved, balance: balance, balanceDate: balanceDate, sourceHint: sourceHint, interest: interest)
    }
}

/// One optimistic change to send for one or more rows.
struct RowEdit<Row: EditableRow> {
    /// What a failure is reported under: the row's name, or a count of rows.
    let rowName: String
    let patch: Row.Patch
    /// Whether a row leaves the list, given the row before the patch.
    let leaves: (Row) -> Bool
}

/// A failed edit and the row it was for, so an alert shown after a reload is still unambiguous.
struct EditFailure {
    let error: APIError
    let rowName: String

    var message: String { "\(rowName): \(error.localizedDescription)" }
}

/// Rows edited with optimistic PATCHes. The one place the edit rules live, for every list screen:
/// - `editError` is cleared when an edit starts and set when one fails, unless the edit hands its failure to
///   `failed`. A cancelled edit sets nothing, and a failure is reported even after the rows were replaced (a
///   reload or another month), so it is never lost.
/// - A row with several edits in flight settles on the server's last reply plus whatever is still in flight;
///   a failed edit drops only its own fields, and when the last one fails the row goes back to the server's version.
/// - A rollback applies only to the rows the edit began on: once `replace(with:)` swaps them, a failed edit
///   leaves the new rows alone. The server's reply is still written into a row that is listed.
/// - An edit in flight on a row `splice` replaces or removes is ignored when it lands or fails, so it can't
///   undo the splice.
@Observable
final class EditableRows<Row: EditableRow> {
    /// The rows, in the owner's order.
    private(set) var items: [Row] = []
    /// The latest failed edit and the row it was for; its message is the server's. Cleared when the next edit starts.
    private(set) var editError: EditFailure?

    /// Without an explicit deinit, Swift 6.3's optimizer crashes in the synthesized one for a generic class in Release.
    deinit {}

    /// Bumped by `replace(with:)`; edits from an earlier generation don't roll back.
    private var generation = 0
    /// Rows with edits in flight in this generation: the server's latest version and the patches still unanswered.
    private var inFlight: [UUID: InFlight] = [:]
    /// Edits in flight on rows a `splice` replaced or removed; their reply and their failure change nothing.
    private var ignored: Set<UUID> = []

    private struct InFlight {
        var confirmed: Row
        /// The patches still unanswered; nil for a delete, which changes nothing on the row.
        var patches: [(edit: UUID, patch: Row.Patch?)]

        var shown: Row { patches.reduce(confirmed) { row, entry in entry.patch.map(row.applying) ?? row } }
    }

    /// Where a row an edit removed stood, so a failed edit puts it back.
    private struct Removal {
        let index: Int
        /// The row that followed the removed one.
        let followingId: UUID?
    }

    /// A new set of rows (a load, or a different query). Edits still in flight won't roll back into it.
    func replace(with rows: [Row]) {
        items = rows
        generation += 1
        inFlight = [:]
    }

    /// Appends the rows not already listed.
    func append(contentsOf rows: [Row]) {
        let known = Set(items.map(\.id))
        items += rows.filter { !known.contains($0.id) }
    }

    /// Removes row `id`, a change the server already has. Returns whether it was listed.
    @discardableResult
    func remove(id: UUID) -> Bool {
        guard let index = items.firstIndex(where: { $0.id == id }) else { return false }
        items.remove(at: index)
        return true
    }

    /// Replaces the listed row with `row`'s id, or appends `row`.
    func upsert(_ row: Row) {
        if let index = items.firstIndex(where: { $0.id == row.id }) {
            items[index] = row
        } else {
            items.append(row)
        }
    }

    /// Changes row `id` with `transform`, a change the server already has (a recorded value beside the row's
    /// edits). An edit in flight on the row keeps it when it lands or rolls back.
    func amend(id: UUID, _ transform: (Row) -> Row) {
        if let index = items.firstIndex(where: { $0.id == id }) { items[index] = transform(items[index]) }
        if let confirmed = inFlight[id]?.confirmed { inFlight[id]?.confirmed = transform(confirmed) }
    }

    /// Puts `parent` in place of the listed row with its id and `children` right after it, dropping the rows
    /// `isStale` names (children of an earlier split), all changes the server already has. Edits in flight on
    /// `parent` or on a dropped row are ignored from here on: a landing edit doesn't write its reply over `parent`,
    /// and a failing one doesn't put a dropped row back. Returns whether `parent`'s row was listed.
    @discardableResult
    func splice(parent: Row, children: [Row], removing isStale: (Row) -> Bool) -> Bool {
        guard items.contains(where: { $0.id == parent.id }) else { return false }
        func isDropped(_ row: Row) -> Bool { row.id != parent.id && isStale(row) }
        let superseded = [parent.id] + inFlight.filter { isDropped($0.value.confirmed) }.map(\.key)
            + items.filter(isDropped).map(\.id)
        for id in superseded {
            ignored.formUnion(inFlight.removeValue(forKey: id)?.patches.map(\.edit) ?? [])
        }
        let kept = Set(items.filter { !isDropped($0) }.map(\.id))
        let added = children.filter { !kept.contains($0.id) }
        items = items.flatMap { row in
            row.id == parent.id ? [parent] + added : isDropped(row) ? [] : [row]
        }
        return true
    }

    func sort(by areInIncreasingOrder: (Row, Row) -> Bool) {
        items.sort(by: areInIncreasingOrder)
    }

    func clearEditError() {
        editError = nil
    }

    /// Applies `change`'s patch to row `id` at once, then sends it. A row for which `change.leaves` is true is
    /// removed; any other is updated in place. `changed` is called after each local change with the change in row
    /// count: -1 when the row leaves, +1 when a failed edit puts it back, otherwise 0. A failure goes to `failed`
    /// instead of `editError` when it is given, for a sheet that shows it inline.
    /// Returns whether the server accepted the edit; false also when no row has `id`.
    @discardableResult
    func edit(
        id: UUID,
        _ change: RowEdit<Row>,
        changed: (_ countChange: Int) -> Void = { _ in },
        failed: ((EditFailure) -> Void)? = nil,
        send: () async throws -> Row.Saved
    ) async -> Bool {
        await run([id], change, changed: changed, failed: failed) { _ in [try await send()] }
    }

    /// Removes row `id` at once, then `send`s its delete, by `edit`'s rules: a failure puts the row back where it
    /// stood, with whatever edits are still in flight on it. Returns whether the server accepted the delete; false
    /// also when no row has `id`.
    @discardableResult
    func delete(id: UUID, rowName: String, send: () async throws -> Void) async -> Bool {
        editError = nil
        guard let index = items.firstIndex(where: { $0.id == id }) else { return false }
        let editId = UUID()
        let removal = begin(editId, at: index, patch: nil, leaves: { _ in true })
        let started = generation
        do {
            try await send()
            if ignored.remove(editId) == nil { forget(editId, id: id) }
            return true
        } catch {
            report(error, rowName: rowName, failed: nil)
            guard ignored.remove(editId) == nil, generation == started else { return false }
            rollback(editId, id: id, removal: removal)
            return false
        }
    }

    /// Applies `change`'s patch to every listed row in `ids` at once, then sends it once for all of them,
    /// all-or-nothing. `send` gets the ids of the listed rows, in list order, and returns their saved versions.
    /// Each row follows `edit`'s rules, and `changed` gets the sum of their count changes. On failure every row goes
    /// back to where and what it was (a removed row reappears in its place), with one failure named
    /// `change.rowName`. A reply missing a sent row is a failure too, so no row is left showing a change the
    /// server didn't confirm. A row a `splice` superseded meanwhile is left alone, the others still settle.
    /// Returns whether the server accepted the edit; false also when none of `ids` is listed.
    @discardableResult
    func editMany(
        ids: [UUID],
        _ change: RowEdit<Row>,
        changed: (_ countChange: Int) -> Void = { _ in },
        send: (_ ids: [UUID]) async throws -> [Row.Saved]
    ) async -> Bool where Row.Saved: Identifiable, Row.Saved.ID == UUID {
        await run(ids, change, changed: changed, failed: nil) { listed in
            let saved = Dictionary(try await send(listed).map { ($0.id, $0) }, uniquingKeysWith: { $1 })
            return try listed.map { id in
                guard let row = saved[id] else { throw APIError.decoding(Self.missingRow) }
                return row
            }
        }
    }

    /// The failure for a reply that leaves out a row it was sent.
    private static var missingRow: DecodingError {
        .dataCorrupted(.init(codingPath: [], debugDescription: "The reply left out a row it was sent."))
    }

    /// An edit begun on one row: the row, the edit's id, and where the row stood when the edit removed it.
    private struct BegunEdit {
        let id: UUID
        let editId: UUID
        let removal: Removal?
    }

    /// `edit` and `editMany`: begins `change` on the listed rows in `ids`, sends their ids, and settles them with
    /// `send`'s reply (one saved row per sent id, in order) or rolls them back when it throws.
    private func run(
        _ ids: [UUID],
        _ change: RowEdit<Row>,
        changed: (_ countChange: Int) -> Void,
        failed: ((EditFailure) -> Void)?,
        send: (_ ids: [UUID]) async throws -> [Row.Saved]
    ) async -> Bool {
        editError = nil
        let edits = beginAll(ids, change)
        guard !edits.isEmpty else { return false }
        let started = generation
        changed(-edits.count { $0.removal != nil })
        do {
            let saved = try await send(edits.map(\.id))
            for (edit, row) in zip(edits, saved) where ignored.remove(edit.editId) == nil {
                commit(edit.editId, id: edit.id, saved: row, removed: edit.removal != nil)
            }
            changed(0)
            return true
        } catch {
            report(error, rowName: change.rowName, failed: failed)
            let live = edits.filter { ignored.remove($0.editId) == nil }
            guard generation == started else { return false }
            changed(rollBack(live))
            return false
        }
    }

    /// Begins `change` on each listed row in `ids`, in list order.
    private func beginAll(_ ids: [UUID], _ change: RowEdit<Row>) -> [BegunEdit] {
        let wanted = Set(ids)
        var edits: [BegunEdit] = []
        var index = 0
        while index < items.count {
            let id = items[index].id
            guard wanted.contains(id) else {
                index += 1
                continue
            }
            let editId = UUID()
            let removal = begin(editId, at: index, patch: change.patch, leaves: change.leaves)
            edits.append(BegunEdit(id: id, editId: editId, removal: removal))
            if removal == nil { index += 1 }
        }
        return edits
    }

    /// Sets `editError`, or hands the failure to `failed`; a cancelled edit reports nothing.
    private func report(_ error: any Error, rowName: String, failed: ((EditFailure) -> Void)?) {
        guard !(error is CancellationError) else { return }
        let failure = EditFailure(error: APIError(wrapping: error), rowName: rowName)
        if let failed { failed(failure) } else { editError = failure }
    }

    /// Rolls `edits` back in reverse begin order, so removed rows go back in place. Returns the change in row count.
    private func rollBack(_ edits: [BegunEdit]) -> Int {
        let before = items.count
        for edit in edits.reversed() {
            rollback(edit.editId, id: edit.id, removal: edit.removal)
        }
        return items.count - before
    }

    /// Shows `patch` on the row at `index`. Returns where the row stood when it was removed.
    private func begin(_ editId: UUID, at index: Int, patch: Row.Patch?, leaves: (Row) -> Bool) -> Removal? {
        let row = items[index]
        inFlight[row.id, default: InFlight(confirmed: row, patches: [])].patches.append((edit: editId, patch: patch))
        guard leaves(row) else {
            items[index] = patch.map(row.applying) ?? row
            return nil
        }
        let removal = Removal(index: index, followingId: index + 1 < items.count ? items[index + 1].id : nil)
        items.remove(at: index)
        return removal
    }

    private func commit(_ editId: UUID, id: UUID, saved: Row.Saved, removed: Bool) {
        let position = items.firstIndex { $0.id == id }
        guard var state = inFlight[id] else {
            if !removed, let position { items[position] = items[position].replacing(saved) }
            return
        }
        state.patches.removeAll { $0.edit == editId }
        state.confirmed = state.confirmed.replacing(saved)
        inFlight[id] = state.patches.isEmpty ? nil : state
        if let position { items[position] = state.shown }
    }

    /// Drops edit `editId` from row `id`'s edits in flight. Returns the row's state without it.
    @discardableResult
    private func forget(_ editId: UUID, id: UUID) -> InFlight? {
        guard var state = inFlight[id] else { return nil }
        state.patches.removeAll { $0.edit == editId }
        inFlight[id] = state.patches.isEmpty ? nil : state
        return state
    }

    private func rollback(_ editId: UUID, id: UUID, removal: Removal?) {
        guard let state = forget(editId, id: id) else { return }
        if let position = items.firstIndex(where: { $0.id == id }) {
            if removal == nil { items[position] = state.shown }
        } else if let removal {
            let following = removal.followingId.flatMap { id in items.firstIndex { $0.id == id } }
            items.insert(state.shown, at: following ?? Swift.min(removal.index, items.count))
        }
    }
}
