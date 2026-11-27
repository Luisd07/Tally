import Foundation
import Observation

/// The load rules for a screen that shows one key (a month, a year) at a time: `LatestLoad`'s, plus a key switch.
/// Switching keys calls `reset` before the request, so the old key's data never shows under the new title.
/// Only the latest `load` lands, whatever its key.
@Observable
final class KeyedLoader<Key: Equatable> {
    /// The key of the latest `load`.
    private(set) var key: Key?
    /// The latest load failure for `key`. Cleared by the next successful load or a key switch.
    var error: APIError? { latest.error }
    /// Whether a load of `key` has finished, with data or an error. A cancelled or superseded load doesn't count.
    var hasLoaded: Bool { latest.hasLoaded }
    /// Whether a load of `key` succeeded, so the caller's data is this key's. A failed refresh keeps it true.
    var hasData: Bool { latest.hasData }

    private let latest = LatestLoad()

    // An explicit deinit works around a Swift 6.x optimizer crash (EarlyPerfInliner) on generic @Observable
    // classes in Release.
    deinit {}

    /// Fetches `key`. Returns the value for the caller to show, or nil when it failed (with `error` set),
    /// was cancelled or was superseded by a newer `load`.
    func load<Value>(key: Key, reset: () -> Void, fetch: () async throws -> Value) async -> Value? {
        if key != self.key {
            self.key = key
            latest.reset()
            reset()
        }
        return await latest.load(fetch: fetch)
    }

    /// Sends a write and lands what the server returned, by `LatestLoad.land`'s rules. Returns whether it succeeded.
    func land<Value>(
        _ write: () async throws -> Value,
        apply: (Value) -> Void,
        reload: () async -> Void,
        failed: (APIError) -> Void
    ) async -> Bool {
        await latest.land(write, apply: apply, reload: reload, failed: failed)
    }
}
