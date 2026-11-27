import Foundation
import Observation

/// One month of income for the Income screen. Nothing is stored: rows live for the screen's lifetime.
/// Loading follows `MonthLoader`'s rules and edits `EditableRows`' rules; a write drops the month loads sent before
/// it and, when it dropped one, loads again.
@Observable
final class IncomeModel {
    /// Expected and received for the month: the server's on load, recomputed from `entries` after a local change.
    private(set) var totals = IncomeTotals.zero
    /// The latest failed create; the caller still holds the draft.
    private(set) var createError: APIError?

    /// The month's entries, newest date first. Cleared when a different month is requested.
    var entries: [IncomeEntry] { rows.items }
    /// The latest load failure. It sits beside `entries`: a failed reload keeps the rows on screen.
    var error: APIError? { loader.error }
    /// Whether a load of the month has finished, with rows or an error.
    var hasLoaded: Bool { loader.hasLoaded }
    /// Whether `entries` and `totals` are the month's: a load succeeded. A failed refresh keeps it true.
    var hasData: Bool { loader.hasData }
    /// The latest failed edit, already rolled back. Cleared when the next edit starts.
    var editError: EditFailure? { rows.editError }

    /// What is still to arrive this month, never below zero.
    var remaining: Money { Money(max(totals.expected.amount - totals.received.amount, 0)) }

    /// `entries` grouped by type, each group in the order its type first appears in the list.
    var groups: [(type: IncomeType, entries: [IncomeEntry])] {
        var result: [(type: IncomeType, entries: [IncomeEntry])] = []
        for entry in entries {
            if let index = result.firstIndex(where: { $0.type == entry.type }) {
                result[index].entries.append(entry)
            } else {
                result.append((entry.type, [entry]))
            }
        }
        return result
    }

    private let loader = MonthLoader()
    private let rows = EditableRows<IncomeEntry>()

    func load(month: MonthRef, api: APIClient) async {
        let page = await loader.load(
            month: month,
            reset: {
                rows.replace(with: [])
                totals = .zero
            },
            fetch: { try await api.income(month: month) }
        )
        guard let page else { return }
        rows.replace(with: page.items)
        totals = page.totals
    }

    /// Creates `draft` and, when it falls in the loaded month, inserts the server's row in date order and
    /// recomputes the totals locally (the list is the whole month, so the sums match the server's). Returns false
    /// on failure, with `createError` set unless it was cancelled; the draft keeps its id, so sending it again
    /// can't duplicate the row. A month load sent before the create is dropped so it can't take the new row off
    /// the list, then sent again.
    func create(_ draft: IncomeDraft, api: APIClient) async -> Bool {
        createError = nil
        return await loader.land(
            { try await api.createIncome(draft) },
            apply: { created in
                guard let month = loader.month, MonthRef(containing: created.date) == month else { return }
                rows.upsert(created)
                rowsChanged()
            },
            reload: { await reloadMonth(api: api) },
            failed: { createError = $0 }
        )
    }

    /// Marks `entry` received in full: `received` becomes its expected amount.
    func markReceived(_ entry: IncomeEntry, api: APIClient) async {
        guard let expected = entry.expected else { return }
        await update(entry, patch: IncomePatch(received: .some(expected)), api: api)
    }

    /// Applies `patch` to the row at once, then confirms it with the server. A date moved out of the month
    /// takes the row off the list. On failure the row goes back and `editError` says why. Once the server answers,
    /// a month load sent before the edit is dropped so it can't put the old row back, and sent again.
    func update(_ entry: IncomeEntry, patch: IncomePatch, api: APIClient) async {
        let month = loader.month
        _ = await loader.land(
            {
                await rows.edit(
                    id: entry.id,
                    RowEdit(
                        rowName: entry.displayName,
                        patch: patch,
                        leaves: { MonthRef(containing: $0.applying(patch).date) != month }
                    ),
                    changed: { _ in rowsChanged() },
                    send: { try await api.updateIncome(id: entry.id, patch: patch) }
                )
            },
            apply: { _ in },
            reload: { await reloadMonth(api: api) },
            failed: { _ in }
        )
    }

    func clearEditError() {
        rows.clearEditError()
    }

    /// Loads the month of the latest `load` again.
    private func reloadMonth(api: APIClient) async {
        if let month = loader.month { await load(month: month, api: api) }
    }

    /// Restores the server's order (date descending, then id descending) and recomputes the totals.
    private func rowsChanged() {
        rows.sort { lhs, rhs in lhs.date != rhs.date ? lhs.date > rhs.date : lhs.id.uuidString > rhs.id.uuidString }
        totals = IncomeTotals(
            expected: Money(entries.reduce(0) { $0 + ($1.expected?.amount ?? 0) }),
            received: Money(entries.reduce(0) { $0 + ($1.received?.amount ?? 0) })
        )
    }
}
