import Foundation

/// The load rules for a screen that shows one month at a time: `KeyedLoader`'s, keyed by month.
typealias MonthLoader = KeyedLoader<MonthRef>

extension KeyedLoader where Key == MonthRef {
    /// The month of the latest `load`.
    var month: MonthRef? { key }

    /// Fetches `month`, by `load(key:reset:fetch:)`'s rules.
    func load<Value>(month: MonthRef, reset: () -> Void, fetch: () async throws -> Value) async -> Value? {
        await load(key: month, reset: reset, fetch: fetch)
    }
}
