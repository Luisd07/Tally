import Foundation
import Observation

/// One value loaded for one key (a month, a year), by `KeyedLoader`'s rules: reloading the same key keeps its
/// value until the response arrives, requesting a different key drops the old key's value, and a superseded
/// response is ignored.
@Observable
final class KeyedValue<Key: Equatable, Value> {
    /// The value for the most recently requested key; nil until a load succeeds and again when a different key
    /// is requested.
    private(set) var value: Value?
    /// The latest load failure. It sits beside `value`: a failed refresh keeps the value on screen.
    var error: APIError? { loader.error }
    /// Whether a load of the key has finished, with a value or an error.
    var hasLoaded: Bool { loader.hasLoaded }
    /// Whether `value` is the key's: a load succeeded. A failed refresh keeps it true.
    var hasData: Bool { loader.hasData }

    private let loader = KeyedLoader<Key>()

    // An explicit deinit works around a Swift 6.x optimizer crash (EarlyPerfInliner) on generic @Observable
    // classes in Release.
    deinit {}

    func load(key: Key, fetch: () async throws -> Value) async {
        let loaded = await loader.load(key: key, reset: { value = nil }, fetch: fetch)
        if let loaded { value = loaded }
    }
}
