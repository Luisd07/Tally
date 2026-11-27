import Foundation
import Observation

/// The Rates screen's data: the per-mile rates, oldest first. Nothing is stored: rows live for the screen's
/// lifetime. Loading and edits follow `LoadedList`'s rules; a write changes nothing else on screen, so it loads
/// again only when it dropped a load. A second rate on the same date is the server's 409, shown as its message.
@Observable
final class MileageRatesModel: @MainActor ListScreenModel {
    /// The latest failed create; the caller still holds the draft.
    private(set) var createError: APIError?

    var rates: [MileageRate] { list.items }

    let list = LoadedList<MileageRate>(reloadsAfterEveryWrite: false)

    func load(api: APIClient) async {
        await list.load({ _ in try await api.mileageRates() }, rows: { $0 })
    }

    /// Creates `draft` and lists the server's row in date order. Returns false on failure, with `createError` set
    /// unless it was cancelled; the draft keeps its id, so sending it again can't duplicate the row.
    func create(_ draft: MileageRateDraft, api: APIClient) async -> Bool {
        createError = nil
        return await list.land(
            { try await api.createMileageRate(draft) },
            apply: { created in
                list.rows.upsert(created)
                sortRows()
            },
            reload: { await load(api: api) },
            failed: { createError = $0 }
        )
    }

    /// Applies `patch` to the row at once, then confirms it with the server. On failure the row goes back and
    /// `editError` says why.
    func update(_ rate: MileageRate, patch: MileageRatePatch, api: APIClient) async {
        await list.update(
            id: rate.id,
            rowName: rate.label,
            patch: patch,
            changed: { _ in sortRows() },
            send: { try await api.updateMileageRate(id: rate.id, patch: patch) },
            reload: { await load(api: api) }
        )
    }

    /// Takes `rate` off the list at once, then deletes it on the server. On failure it goes back where it stood
    /// and `editError` says why.
    func delete(_ rate: MileageRate, api: APIClient) async {
        await list.remove(
            id: rate.id,
            rowName: rate.label,
            send: { try await api.deleteMileageRate(id: rate.id) },
            reload: { await load(api: api) }
        )
    }

    /// Effective date ascending, the server's order.
    private func sortRows() {
        list.rows.sort { $0.effectiveFrom < $1.effectiveFrom }
    }
}

extension MileageRate: @MainActor EditableRow {
    /// This rate with the fields `patch` sets, as the server will return it.
    func applying(_ patch: MileageRatePatch) -> MileageRate {
        MileageRate(
            id: id,
            effectiveFrom: patch.effectiveFrom ?? effectiveFrom,
            rate: patch.rate ?? rate,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }

    func replacing(_ saved: MileageRate) -> MileageRate { saved }

    /// "From Jul 1, 2026 · 76¢/mi".
    var label: String {
        "From \(effectiveFrom.formatted(.dateTime.month(.abbreviated).day().year())) · \(rate.centsText)/mi"
    }
}
