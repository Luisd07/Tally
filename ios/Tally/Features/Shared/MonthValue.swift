import Foundation

/// One value loaded for one month, by `KeyedValue`'s rules keyed by month.
typealias MonthValue<Value> = KeyedValue<MonthRef, Value>

extension KeyedValue where Key == MonthRef {
    /// Loads `month`, by `load(key:fetch:)`'s rules.
    func load(month: MonthRef, fetch: () async throws -> Value) async {
        await load(key: month, fetch: fetch)
    }
}
