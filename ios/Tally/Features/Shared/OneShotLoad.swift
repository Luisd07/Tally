import Foundation
import Observation

/// A value a view or model loads on its own, such as a sheet's choices or a detail's rows, by `LatestLoad`'s
/// rules: only the latest `load` lands, a cancelled one changes nothing, and a failed one sets `error` and keeps
/// `value`. Start it from `.task`, so leaving the view cancels it.
@Observable
final class OneShotLoad<Value> {
    /// The latest loaded value; nil until a load succeeds.
    private(set) var value: Value?
    /// The latest load failure. Cleared by the next successful load.
    var error: APIError? { loader.error }
    /// Whether a load is in flight.
    var isLoading: Bool { loader.isLoading }

    private let loader = LatestLoad()

    /// Without an explicit deinit, Swift 6.3's optimizer crashes in the synthesized one for a generic class in Release.
    deinit {}

    func load(_ fetch: () async throws -> Value) async {
        guard let loaded = await loader.load(fetch: fetch) else { return }
        value = loaded
    }

    /// Changes the loaded value with `transform`, a change the server already has. Does nothing before a load.
    func amend(_ transform: (Value) -> Value) {
        value = value.map(transform)
    }
}
