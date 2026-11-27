import Foundation
import Observation

/// One year's odometer reading for the Odometer screen. Nothing is stored. Loading follows `KeyedLoader`'s rules;
/// a save replaces the year's reading and drops the loads sent before it, so none can put the old one back.
@Observable
final class OdometerModel {
    /// The year's reading; nil until it is set.
    private(set) var reading: OdometerReading?
    /// The latest failed save. Cleared when the next one starts.
    private(set) var saveError: APIError?
    private(set) var isSaving = false

    var error: APIError? { loader.error }
    var hasLoaded: Bool { loader.hasLoaded }
    /// Whether `reading` is the year's: a load succeeded.
    var hasData: Bool { loader.hasData }

    private let loader = KeyedLoader<Int>()

    func load(year: Int, api: APIClient) async {
        let loaded = await loader.load(key: year, reset: { reading = nil }, fetch: { try await api.odometer(year: year) })
        if let loaded { reading = loaded }
    }

    /// Replaces `year`'s reading with `input`, unless a save is already running. Returns false on failure, with
    /// `saveError` set unless it was cancelled.
    func save(_ input: OdometerInput, year: Int, api: APIClient) async -> Bool {
        guard !isSaving else { return false }
        isSaving = true
        saveError = nil
        defer { isSaving = false }
        return await loader.land(
            { try await api.saveOdometer(year: year, input) },
            apply: { saved in if loader.key == year { reading = saved } },
            reload: { if let key = loader.key { await load(year: key, api: api) } },
            failed: { saveError = $0 }
        )
    }
}
