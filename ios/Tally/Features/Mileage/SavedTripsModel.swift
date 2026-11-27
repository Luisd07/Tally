import Foundation
import Observation

/// The saved trips on the Mileage screen, by name. Nothing is stored: rows live for the screen's lifetime. Loading
/// and edits follow `LoadedList`'s rules; a write changes nothing else on screen, so it loads again only when it
/// dropped a load.
@Observable
final class SavedTripsModel: @MainActor ListScreenModel {
    /// The latest failed create; the caller still holds the draft.
    private(set) var createError: APIError?

    var savedTrips: [SavedTrip] { list.items }

    let list = LoadedList<SavedTrip>(reloadsAfterEveryWrite: false)

    func load(api: APIClient) async {
        await list.load({ _ in try await api.savedTrips() }, rows: { $0 })
    }

    /// Creates `draft` and lists the server's row by name. Returns false on failure, with `createError` set unless
    /// it was cancelled; the draft keeps its id, so sending it again can't duplicate the row.
    func create(_ draft: SavedTripDraft, api: APIClient) async -> Bool {
        createError = nil
        return await list.land(
            { try await api.createSavedTrip(draft) },
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
    func update(_ saved: SavedTrip, patch: SavedTripPatch, api: APIClient) async {
        await list.update(
            id: saved.id,
            rowName: saved.name,
            patch: patch,
            changed: { _ in sortRows() },
            send: { try await api.updateSavedTrip(id: saved.id, patch: patch) },
            reload: { await load(api: api) }
        )
    }

    /// Takes `saved` off the list at once, then deletes it on the server; trips it logged stay. On failure it goes
    /// back where it stood and `editError` says why.
    func delete(_ saved: SavedTrip, api: APIClient) async {
        await list.remove(
            id: saved.id,
            rowName: saved.name,
            send: { try await api.deleteSavedTrip(id: saved.id) },
            reload: { await load(api: api) }
        )
    }

    /// Name ascending, then id.
    private func sortRows() {
        list.rows.sort { lhs, rhs in
            let order = lhs.name.localizedCaseInsensitiveCompare(rhs.name)
            return order == .orderedSame ? lhs.id.uuidString < rhs.id.uuidString : order == .orderedAscending
        }
    }
}

extension SavedTrip: @MainActor EditableRow {
    /// This saved trip with the fields `patch` sets, as the server will return it.
    func applying(_ patch: SavedTripPatch) -> SavedTrip {
        SavedTrip(
            id: id,
            name: patch.name ?? name,
            miles: patch.details.miles ?? miles,
            purpose: patch.details.purpose ?? purpose,
            startLocation: patch.details.startLocation ?? startLocation,
            endLocation: patch.details.endLocation ?? endLocation,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }

    func replacing(_ saved: SavedTrip) -> SavedTrip { saved }
}
