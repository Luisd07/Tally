import Foundation
import Observation

/// One year of the mileage log for the Mileage screen, and the saved-trip taps that add to it. Nothing is stored:
/// rows live for the screen's lifetime. Loading follows `KeyedLoader`'s rules and edits `EditableRows`' rules; a
/// write drops the year loads sent before it and, when it dropped one, loads again.
@Observable
final class MileageModel {
    /// The latest failed create; the caller still holds the draft.
    private(set) var createError: APIError?
    /// The trip the latest saved-trip tap logged, while it can still be undone.
    private(set) var lastLogged: LoggedTrip?
    /// The latest saved-trip tap that failed, kept so Retry sends the same trip id.
    private(set) var failedLog: FailedLog?
    /// The saved trips whose tap is still logging. Another tap on one of them is ignored, so it can't log a second
    /// trip.
    private(set) var loggingIds: Set<UUID> = []

    /// The year's trips, newest first. Cleared when a different year is requested.
    var trips: [Trip] { rows.items }
    /// The year's business miles: every listed trip's, which is the whole year.
    var businessMiles: Miles { Miles(trips.reduce(0) { $0 + $1.miles.value }) }
    /// The latest load failure. It sits beside `trips`: a failed reload keeps the rows on screen.
    var error: APIError? { loader.error }
    /// Whether a load of the year has finished, with rows or an error.
    var hasLoaded: Bool { loader.hasLoaded }
    /// Whether `trips` are the year's: a load succeeded. A failed refresh keeps it true.
    var hasData: Bool { loader.hasData }
    /// The latest failed edit or undo, already rolled back. Cleared when the next one starts.
    var editError: EditFailure? { undoFailure ?? rows.editError }

    private let loader = KeyedLoader<Int>()
    private let rows = EditableRows<Trip>()
    /// An undo whose trip wasn't listed (another year was showing) and whose delete failed.
    private var undoFailure: EditFailure?

    /// A trip a saved-trip tap logged, and the saved trip's name for the undo banner.
    struct LoggedTrip: Identifiable {
        let trip: Trip
        let name: String

        var id: UUID { trip.id }
    }

    /// A saved-trip tap that failed: the saved trip, the trip id it sent, and why.
    struct FailedLog {
        let saved: SavedTrip
        let tripId: UUID
        let error: APIError
    }

    func load(year: Int, api: APIClient) async {
        let trips = await loader.load(
            key: year,
            reset: { rows.replace(with: []) },
            fetch: { try await api.trips(year: year) }
        )
        guard let trips else { return }
        rows.replace(with: trips)
    }

    /// Creates `draft` and, when it falls in the loaded year, inserts the server's row in date order. Returns false
    /// on failure, with `createError` set unless it was cancelled; the draft keeps its id, so sending it again
    /// can't duplicate the row. A year load sent before the create is dropped so it can't take the new row off
    /// the list, then sent again.
    func create(_ draft: TripDraft, api: APIClient) async -> Bool {
        createError = nil
        return await loader.land(
            { try await api.createTrip(draft) },
            apply: insert,
            reload: { await reloadYear(api: api) },
            failed: { createError = $0 }
        )
    }

    /// Applies `patch` to the row at once, then confirms it with the server. A date moved out of the year takes
    /// the row off the list. On failure the row goes back and `editError` says why. Once the server answers, a
    /// year load sent before the edit is dropped so it can't put the old row back, and sent again.
    func update(_ trip: Trip, patch: TripPatch, api: APIClient) async {
        let year = loader.key
        await settle(api: api) {
            await rows.edit(
                id: trip.id,
                RowEdit(rowName: trip.purpose, patch: patch, leaves: { $0.applying(patch).date.year != year }),
                changed: { _ in sortRows() },
                send: { try await api.updateTrip(id: trip.id, patch: patch) }
            )
        }
    }

    /// Takes `trip` off the list at once, then deletes it on the server. On failure it goes back where it stood and
    /// `editError` says why.
    func delete(_ trip: Trip, api: APIClient) async {
        await settle(api: api) {
            await rows.delete(id: trip.id, rowName: trip.purpose, send: { try await api.deleteTrip(id: trip.id) })
        }
    }

    /// Logs `saved` as a trip dated today, under a new trip id. On success the trip is listed when the loaded
    /// year is this one, and `lastLogged` offers its undo; on failure `failedLog` keeps the id for `retryLog`. Does
    /// nothing while a log of `saved` is in flight.
    func log(_ saved: SavedTrip, api: APIClient) async {
        await log(saved, tripId: UUID(), api: api)
    }

    /// Sends the failed tap again with the same trip id, so a tap the server did get can't log a second trip.
    func retryLog(api: APIClient) async {
        guard let failedLog else { return }
        await log(failedLog.saved, tripId: failedLog.tripId, api: api)
    }

    func dismissFailedLog() {
        failedLog = nil
    }

    /// Deletes the trip `lastLogged` names, by `delete`'s rules when it is listed.
    func undoLog(api: APIClient) async {
        guard let logged = lastLogged else { return }
        lastLogged = nil
        undoFailure = nil
        guard !trips.contains(where: { $0.id == logged.id }) else {
            await delete(logged.trip, api: api)
            return
        }
        do {
            try await api.deleteTrip(id: logged.id)
        } catch is CancellationError {
        } catch {
            undoFailure = EditFailure(error: APIError(wrapping: error), rowName: logged.trip.purpose)
        }
    }

    /// Stops offering the undo for trip `id`, once its banner has shown long enough.
    func expireUndo(id: UUID) {
        if lastLogged?.id == id { lastLogged = nil }
    }

    func clearEditError() {
        undoFailure = nil
        rows.clearEditError()
    }

    private func log(_ saved: SavedTrip, tripId: UUID, api: APIClient) async {
        guard loggingIds.insert(saved.id).inserted else { return }
        defer { loggingIds.remove(saved.id) }
        failedLog = nil
        _ = await loader.land(
            { try await api.logSavedTrip(id: saved.id, tripId: tripId) },
            apply: { trip in
                insert(trip)
                lastLogged = LoggedTrip(trip: trip, name: saved.name)
            },
            reload: { await reloadYear(api: api) },
            failed: { failedLog = FailedLog(saved: saved, tripId: tripId, error: $0) }
        )
    }

    /// Runs an optimistic `change`, then drops the year loads sent before it and loads again when it dropped one.
    private func settle(api: APIClient, _ change: () async -> Bool) async {
        _ = await loader.land(change, apply: { _ in }, reload: { await reloadYear(api: api) }, failed: { _ in })
    }

    /// Lists `trip` in date order when it falls in the loaded year.
    private func insert(_ trip: Trip) {
        guard trip.date.year == loader.key else { return }
        rows.upsert(trip)
        sortRows()
    }

    /// Loads the year of the latest `load` again.
    private func reloadYear(api: APIClient) async {
        if let year = loader.key { await load(year: year, api: api) }
    }

    /// The server's order: date, then created time, then id, all descending.
    private func sortRows() {
        rows.sort { lhs, rhs in
            if lhs.date != rhs.date { return lhs.date > rhs.date }
            if lhs.createdAt != rhs.createdAt { return lhs.createdAt > rhs.createdAt }
            return lhs.id.uuidString > rhs.id.uuidString
        }
    }
}

extension Trip: @MainActor EditableRow {
    /// This trip with the fields `patch` sets, as the server will return it.
    func applying(_ patch: TripPatch) -> Trip {
        Trip(
            id: id,
            date: patch.date ?? date,
            miles: patch.details.miles ?? miles,
            purpose: patch.details.purpose ?? purpose,
            startLocation: patch.details.startLocation ?? startLocation,
            endLocation: patch.details.endLocation ?? endLocation,
            createdAt: createdAt,
            updatedAt: updatedAt
        )
    }

    func replacing(_ saved: Trip) -> Trip { saved }
}
