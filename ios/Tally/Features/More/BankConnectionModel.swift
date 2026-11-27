import Foundation
import Observation

/// Connecting SimpleFIN with a setup token, running a sync, and the recent runs.
@Observable
final class BankConnectionModel {
    private(set) var runs: [SyncRun] = []
    private(set) var hasLoaded = false
    private(set) var isWorking = false
    /// The SimpleFIN host after a connect in this visit.
    private(set) var connectedHost: String?
    private(set) var message: String?

    func load(api: APIClient) async {
        do {
            runs = try await api.syncRuns().items
            message = nil
        } catch is CancellationError {
            return
        } catch {
            message = error.localizedDescription
        }
        hasLoaded = true
    }

    /// Claims the setup token, then runs the first sync, which backfills the year.
    func connect(setupToken: String, api: APIClient) async -> Bool {
        let token = setupToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return false }
        var connected = false
        await run {
            let claim = try await api.claimSimplefin(setupToken: token)
            connectedHost = claim.host
            connected = true
            try await syncSteps(api: api)
        }
        return connected
    }

    func syncNow(api: APIClient) async {
        await run { try await syncSteps(api: api) }
    }

    private func syncSteps(api: APIClient) async throws {
        let latest = try await api.syncNow()
        runs.removeAll { $0.id == latest.id }
        runs.insert(latest, at: 0)
    }

    private func run(_ steps: () async throws -> Void) async {
        guard !isWorking else { return }
        isWorking = true
        message = nil
        defer { isWorking = false }
        do {
            try await steps()
        } catch is CancellationError {
            return
        } catch {
            message = error.localizedDescription
        }
    }
}
