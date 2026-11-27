import Foundation
import Observation

/// The load rules for a screen's data; the screen's model keeps the data itself.
/// - Only the latest `load` lands: a response that a newer `load` (or `supersede`) overtook is dropped.
/// - A cancelled load changes nothing and reports nothing.
/// - A failed load sets `error` and leaves the data alone, so a failed refresh keeps the data on screen.
@Observable
final class LatestLoad {
    /// The latest load failure. Cleared by the next successful load or `reset`.
    private(set) var error: APIError?
    /// Whether a load has finished, with data or an error. A cancelled or superseded load doesn't count.
    private(set) var hasLoaded = false
    /// Whether a load succeeded, so the caller's data is the server's. A failed refresh keeps it true.
    private(set) var hasData = false
    /// Whether a load is in flight, a cancelled one included until it returns.
    var isLoading: Bool { inFlight > 0 }

    /// Bumped by every `load`, and by a `supersede` with loads in flight; responses from an earlier one are dropped.
    private var generation = 0
    /// The loads sent and not yet finished, cancelled ones included until they return.
    private var inFlight = 0

    /// Fetches the data. Returns the value for the caller to show, or nil when it failed (with `error` set),
    /// was cancelled or was superseded.
    func load<Value>(fetch: () async throws -> Value) async -> Value? {
        generation += 1
        inFlight += 1
        defer { inFlight -= 1 }
        return await fetchLatest(fetch) { succeeded in
            hasLoaded = true
            if succeeded { hasData = true }
        }
    }

    /// Fetches more of the data the latest load landed (a next page), without dropping the loads in flight.
    /// Returns the value for the caller to add, or nil when it failed (with `error` set), was cancelled, or a
    /// `load`, or a `supersede` that dropped one, came after it was sent.
    func loadMore<Value>(fetch: () async throws -> Value) async -> Value? {
        await fetchLatest(fetch) { _ in }
    }

    /// Drops the loads in flight, so none replaces data the caller changed after they were sent (a saved create
    /// or edit). Returns whether one was in flight: the caller should load again, so the data it asked for lands.
    @discardableResult
    func supersede() -> Bool {
        guard inFlight > 0 else { return false }
        generation += 1
        return true
    }

    /// Sends a write (a create, a recorded value) and lands what the server returned: `apply` shows it, after
    /// the loads in flight are dropped so none takes it off screen, then `reload` sends one again so the data it
    /// asked for lands; with `reloadsAlways`, `reload` runs after every write that succeeded. Returns whether the
    /// write succeeded; a failure calls `failed` unless it was cancelled.
    func land<Value>(
        _ write: () async throws -> Value,
        apply: (Value) -> Void,
        reload: () async -> Void,
        reloadsAlways: Bool = false,
        failed: (APIError) -> Void
    ) async -> Bool {
        do {
            let value = try await write()
            let dropped = supersede()
            apply(value)
            if dropped || reloadsAlways { await reload() }
            return true
        } catch {
            if !(error is CancellationError) { failed(APIError(wrapping: error)) }
            return false
        }
    }

    /// Forgets the load state, as for a different query. Loads in flight still land.
    func reset() {
        error = nil
        hasLoaded = false
        hasData = false
    }

    /// `fetch`'s value, or nil when it failed (with `error` set), was cancelled or was overtaken by a newer `load`
    /// or `supersede`. `finished` gets whether it succeeded, unless it was cancelled or overtaken.
    private func fetchLatest<Value>(
        _ fetch: () async throws -> Value,
        finished: (_ succeeded: Bool) -> Void
    ) async -> Value? {
        let current = generation
        do {
            let value = try await fetch()
            guard generation == current else { return nil }
            error = nil
            finished(true)
            return value
        } catch is CancellationError {
            return nil
        } catch {
            guard generation == current else { return nil }
            self.error = APIError(wrapping: error)
            finished(false)
            return nil
        }
    }
}
